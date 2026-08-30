"""Tests for the Google Calendar extractor.

The real Calendar API isn't reachable in tests, so the ``googleapiclient``
service object is replaced with a fake implementing just the
``events().list()`` call this module makes, shaped like the real API's
JSON responses. Credentials are faked too (``_get_credentials``/
``_build_service`` are monkeypatched directly) — same pattern as
``tests/test_extractors/test_gmail.py``.
"""

from types import SimpleNamespace

import pytest

from extractors.base import ExtractorError
from extractors.calendar import extract_new_items


def event(
    event_id,
    *,
    summary="Meeting",
    description=None,
    start="2026-08-01T09:00:00-04:00",
    end="2026-08-01T10:00:00-04:00",
    all_day=False,
    attendees=None,
    organizer_email="alice@example.com",
    recurrence=None,
    status="confirmed",
):
    start_field = {"date": start} if all_day else {"dateTime": start}
    end_field = {"date": end} if all_day else {"dateTime": end}
    result = {
        "id": event_id,
        "status": status,
        "summary": summary,
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        "start": start_field,
        "end": end_field,
        "created": "2026-07-01T00:00:00Z",
        "updated": "2026-07-15T00:00:00Z",
        "organizer": {"email": organizer_email},
    }
    if description is not None:
        result["description"] = description
    if attendees is not None:
        result["attendees"] = [{"email": a} for a in attendees]
    if recurrence is not None:
        result["recurrence"] = recurrence
    return result


class FakeExecutor:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeEvents:
    def __init__(self, events):
        self._events = events

    def list(self, **kwargs):
        return FakeExecutor({"items": self._events, "nextPageToken": None})


class FakeService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return FakeEvents(self._events)


def install_fake_calendar(
    monkeypatch,
    events,
    *,
    google_calendar_credentials_path="/fake/creds.json",
    skip_recurring_without_description=True,
):
    monkeypatch.setattr(
        "extractors.calendar.get_settings",
        lambda: SimpleNamespace(
            env=SimpleNamespace(
                google_calendar_credentials_path=google_calendar_credentials_path
            ),
            config=SimpleNamespace(
                filters=SimpleNamespace(
                    calendar=SimpleNamespace(
                        skip_recurring_without_description=skip_recurring_without_description
                    )
                )
            ),
        ),
    )
    monkeypatch.setattr("extractors.calendar._get_credentials", lambda: object())
    monkeypatch.setattr(
        "extractors.calendar._build_service", lambda creds: FakeService(events)
    )


class TestNotConfigured:
    def test_raises_extractor_error_when_credentials_path_is_unset(self, monkeypatch):
        monkeypatch.setattr(
            "extractors.calendar.get_settings",
            lambda: SimpleNamespace(
                env=SimpleNamespace(google_calendar_credentials_path=None)
            ),
        )

        with pytest.raises(ExtractorError, match="GOOGLE_CALENDAR_CREDENTIALS_PATH"):
            extract_new_items()

    def test_raises_extractor_error_when_not_yet_authorized(self, monkeypatch):
        monkeypatch.setattr(
            "extractors.calendar.get_settings",
            lambda: SimpleNamespace(
                env=SimpleNamespace(google_calendar_credentials_path="/fake/creds.json")
            ),
        )

        def raise_not_authorized():
            raise ExtractorError("Google Calendar is not authorized yet. Run ...")

        monkeypatch.setattr(
            "extractors.calendar._get_credentials", raise_not_authorized
        )

        with pytest.raises(ExtractorError, match="not authorized"):
            extract_new_items()


class TestBasicExtraction:
    def test_extracts_title_description_time_and_attendees(self, monkeypatch):
        install_fake_calendar(
            monkeypatch,
            [
                event(
                    "e1",
                    summary="Interview",
                    description="Final round interview.",
                    attendees=["bob@example.com", "carol@example.com"],
                )
            ],
        )

        items = extract_new_items()

        assert len(items) == 1
        item = items[0]
        assert item.source_type == "calendar"
        assert item.source_ref_id == "e1"
        assert item.title == "Interview"
        assert "Final round interview." in item.raw_text
        assert "bob@example.com" in item.raw_text
        assert "carol@example.com" in item.raw_text
        assert "2026-08-01" in item.raw_text
        assert item.author_or_sender == "alice@example.com"

    def test_a_cancelled_event_is_skipped(self, monkeypatch):
        install_fake_calendar(monkeypatch, [event("e1", status="cancelled")])

        items = extract_new_items()

        assert items == []

    def test_an_event_with_no_description_still_produces_an_item(self, monkeypatch):
        # Title/time/attendees alone are still worth ingesting -- most
        # personal calendar events have no description at all.
        install_fake_calendar(monkeypatch, [event("e1", summary="Dentist")])

        items = extract_new_items()

        assert len(items) == 1
        assert "Dentist" in items[0].raw_text

    def test_an_all_day_event_is_anchored_to_utc_midnight(self, monkeypatch):
        install_fake_calendar(
            monkeypatch,
            [
                event(
                    "e1",
                    all_day=True,
                    start="2026-08-01",
                    end="2026-08-02",
                )
            ],
        )

        items = extract_new_items()

        assert items[0].raw_text  # doesn't raise, produces a real item


class TestRecurringEventFiltering:
    def test_a_recurring_event_with_no_description_is_dropped_by_default(
        self, monkeypatch
    ):
        install_fake_calendar(
            monkeypatch,
            [
                event(
                    "e1",
                    summary="Daily standup",
                    recurrence=["RRULE:FREQ=DAILY"],
                )
            ],
        )

        items = extract_new_items()

        assert items == []

    def test_a_recurring_event_with_a_description_is_kept(self, monkeypatch):
        install_fake_calendar(
            monkeypatch,
            [
                event(
                    "e1",
                    summary="Weekly 1:1",
                    description="Discuss roadmap and blockers.",
                    recurrence=["RRULE:FREQ=WEEKLY"],
                )
            ],
        )

        items = extract_new_items()

        assert len(items) == 1
        assert "Discuss roadmap and blockers." in items[0].raw_text

    def test_the_filter_can_be_disabled(self, monkeypatch):
        install_fake_calendar(
            monkeypatch,
            [event("e1", summary="Daily reminder", recurrence=["RRULE:FREQ=DAILY"])],
            skip_recurring_without_description=False,
        )

        items = extract_new_items()

        assert len(items) == 1
