"""Tests for the Gmail extractor.

The real Gmail API isn't reachable in tests, so the ``googleapiclient``
service object is replaced with a fake implementing just the
``users().messages()``/``users().threads()`` chain this module calls,
shaped like the real API's JSON responses. Credentials are faked too
(``_get_credentials``/``_build_service`` are monkeypatched directly) since
the OAuth flow itself is exercised by :func:`extractors.gmail.setup_auth`,
never by ``extract_new_items()``.
"""

import base64
from types import SimpleNamespace

import pytest

from extractors.base import ExtractorError
from extractors.gmail import extract_new_items


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def plain_payload(body_text: str) -> dict:
    return {
        "mimeType": "text/plain",
        "body": {"data": b64(body_text)},
    }


def message(
    message_id,
    thread_id,
    *,
    subject="Hello",
    sender="alice@example.com",
    date="Mon, 1 Aug 2026 12:00:00 +0000",
    body="Message body.",
    label_ids=None,
    parts=None,
):
    payload = (
        {"mimeType": "multipart/mixed", "parts": parts}
        if parts is not None
        else plain_payload(body)
    )
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": label_ids or ["INBOX"],
        "payload": {
            **payload,
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": date},
            ],
        },
    }


class FakeExecutor:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeAttachments:
    def __init__(self, attachments_by_id):
        self._attachments_by_id = attachments_by_id

    def get(self, userId, messageId, id):
        return FakeExecutor({"data": self._attachments_by_id[id]})


class FakeMessages:
    def __init__(self, world):
        self._world = world

    def list(self, userId, q, pageToken=None, maxResults=500):
        self._world["last_query"] = q
        entries = [{"id": m["id"]} for m in self._world["messages"].values()]
        return FakeExecutor({"messages": entries, "nextPageToken": None})

    def get(self, userId, id, format=None, metadataHeaders=None):
        return FakeExecutor(self._world["messages"][id])

    def attachments(self):
        return FakeAttachments(self._world.get("attachments", {}))


class FakeThreads:
    def __init__(self, world):
        self._world = world

    def get(self, userId, id, format=None):
        thread_messages = [
            m for m in self._world["messages"].values() if m["threadId"] == id
        ]
        return FakeExecutor({"id": id, "messages": thread_messages})


class FakeUsers:
    def __init__(self, world):
        self._world = world

    def messages(self):
        return FakeMessages(self._world)

    def threads(self):
        return FakeThreads(self._world)

    def getProfile(self, userId):
        return FakeExecutor({"emailAddress": "me@example.com"})


class FakeService:
    def __init__(self, world):
        self._world = world

    def users(self):
        return FakeUsers(self._world)


def install_fake_gmail(
    monkeypatch,
    messages,
    *,
    excluded_labels=(),
    gmail_credentials_path="/fake/creds.json",
    attachments=None,
    date_range_start=None,
    date_range_end=None,
):
    world = {
        "messages": {m["id"]: m for m in messages},
        "attachments": attachments or {},
    }
    monkeypatch.setattr(
        "extractors.gmail.get_settings",
        lambda: SimpleNamespace(
            env=SimpleNamespace(
                gmail_credentials_path=gmail_credentials_path,
                # Named for what extract_new_items() actually reads
                # (EnvSettings.effective_gmail_date_range_start/_end,
                # which default to a rolling window when unset — see
                # config/settings.py, DECISIONS.md). This helper still
                # accepts a plain None here to exercise
                # _effective_window()'s own "no floor/ceiling" merge
                # behavior in isolation; the defaulting itself is tested
                # separately in tests/test_config/test_settings.py.
                effective_gmail_date_range_start=date_range_start,
                effective_gmail_date_range_end=date_range_end,
            ),
            config=SimpleNamespace(
                filters=SimpleNamespace(
                    gmail=SimpleNamespace(excluded_labels=list(excluded_labels))
                )
            ),
        ),
    )
    monkeypatch.setattr("extractors.gmail._get_credentials", lambda: object())
    monkeypatch.setattr(
        "extractors.gmail._build_service", lambda creds: FakeService(world)
    )
    return world


class TestNotConfigured:
    def test_raises_extractor_error_when_credentials_path_is_unset(self, monkeypatch):
        monkeypatch.setattr(
            "extractors.gmail.get_settings",
            lambda: SimpleNamespace(env=SimpleNamespace(gmail_credentials_path=None)),
        )

        with pytest.raises(ExtractorError, match="GMAIL_CREDENTIALS_PATH"):
            extract_new_items()

    def test_raises_extractor_error_when_not_yet_authorized(self, monkeypatch):
        monkeypatch.setattr(
            "extractors.gmail.get_settings",
            lambda: SimpleNamespace(
                env=SimpleNamespace(gmail_credentials_path="/fake/creds.json")
            ),
        )

        def raise_not_authorized():
            raise ExtractorError("Gmail is not authorized yet. Run ...")

        monkeypatch.setattr("extractors.gmail._get_credentials", raise_not_authorized)

        with pytest.raises(ExtractorError, match="not authorized"):
            extract_new_items()


class TestThreadGrouping:
    def test_two_messages_in_one_thread_become_one_item(self, monkeypatch):
        messages = [
            message("m1", "t1", subject="Original", body="First message."),
            message("m2", "t1", subject="Re: Original", body="A reply."),
        ]
        install_fake_gmail(monkeypatch, messages)

        items = extract_new_items()

        assert len(items) == 1
        item = items[0]
        assert item.source_ref_id == "t1"
        assert item.source_type == "gmail"
        assert "First message." in item.raw_text
        assert "A reply." in item.raw_text

    def test_two_separate_threads_become_two_items(self, monkeypatch):
        messages = [
            message("m1", "t1", subject="Thread one"),
            message("m2", "t2", subject="Thread two"),
        ]
        install_fake_gmail(monkeypatch, messages)

        items = extract_new_items()

        assert {i.source_ref_id for i in items} == {"t1", "t2"}

    def test_title_is_the_subject_and_sender_is_the_most_recent(self, monkeypatch):
        messages = [
            message("m1", "t1", subject="Project update", sender="bob@example.com"),
            message(
                "m2",
                "t1",
                subject="Re: Project update",
                sender="alice@example.com",
            ),
        ]
        install_fake_gmail(monkeypatch, messages)

        items = extract_new_items()

        assert items[0].title == "Project update"
        assert items[0].author_or_sender == "alice@example.com"


class TestLabelFiltering:
    def test_a_message_with_an_excluded_label_is_dropped(self, monkeypatch):
        messages = [
            message("m1", "t1", body="A real message.", label_ids=["INBOX"]),
            message(
                "m2",
                "t1",
                body="Promo content.",
                label_ids=["CATEGORY_PROMOTIONS"],
            ),
        ]
        install_fake_gmail(
            monkeypatch, messages, excluded_labels=["CATEGORY_PROMOTIONS"]
        )

        items = extract_new_items()

        assert len(items) == 1
        assert "A real message." in items[0].raw_text
        assert "Promo content." not in items[0].raw_text

    def test_a_thread_entirely_excluded_produces_no_item(self, monkeypatch):
        messages = [
            message("m1", "t1", label_ids=["CATEGORY_PROMOTIONS"]),
        ]
        install_fake_gmail(
            monkeypatch, messages, excluded_labels=["CATEGORY_PROMOTIONS"]
        )

        items = extract_new_items()

        assert items == []


class TestHtmlFallback:
    def test_html_only_message_is_stripped_to_plain_text(self, monkeypatch):
        html_message = message("m1", "t1", body="unused")
        html_message["payload"] = {
            "mimeType": "text/html",
            "body": {"data": b64("<p>Hello <b>world</b></p>")},
            "headers": html_message["payload"]["headers"],
        }
        install_fake_gmail(monkeypatch, [html_message])

        items = extract_new_items()

        assert "Hello" in items[0].raw_text
        assert "world" in items[0].raw_text
        assert "<p>" not in items[0].raw_text


class TestDateRangeScoping:
    """Exercises _effective_window()'s own before/after merge logic.

    Independent of whether a real, unset GMAIL_DATE_RANGE_START/_END
    actually reaches it as None in practice — since 2026-09-01 it never
    does; EnvSettings.effective_gmail_date_range_start/_end default to a
    rolling 15-day window instead (see
    tests/test_config/test_settings.py, DECISIONS.md).
    """

    def test_no_range_configured_has_no_before_or_after(self, monkeypatch):
        world = install_fake_gmail(monkeypatch, [message("m1", "t1")])

        extract_new_items()

        assert world["last_query"] == ""

    def test_start_date_adds_after_when_no_since_cursor(self, monkeypatch):
        from datetime import date

        world = install_fake_gmail(
            monkeypatch, [message("m1", "t1")], date_range_start=date(2026, 1, 1)
        )

        extract_new_items()

        assert "after:" in world["last_query"]
        assert "before:" not in world["last_query"]

    def test_start_date_does_not_move_a_later_since_cursor_backward(self, monkeypatch):
        from datetime import UTC, date, datetime

        world = install_fake_gmail(
            monkeypatch, [message("m1", "t1")], date_range_start=date(2020, 1, 1)
        )
        later_since = datetime(2026, 1, 1, tzinfo=UTC)

        extract_new_items(later_since)

        assert f"after:{int(later_since.timestamp())}" in world["last_query"]

    def test_start_date_floors_an_earlier_since_cursor(self, monkeypatch):
        from datetime import UTC, date, datetime

        range_start = date(2026, 6, 1)
        world = install_fake_gmail(
            monkeypatch, [message("m1", "t1")], date_range_start=range_start
        )
        earlier_since = datetime(2020, 1, 1, tzinfo=UTC)

        extract_new_items(earlier_since)

        expected_floor = datetime(2026, 6, 1, tzinfo=UTC)
        assert f"after:{int(expected_floor.timestamp())}" in world["last_query"]

    def test_end_date_adds_an_inclusive_before(self, monkeypatch):
        from datetime import UTC, date, datetime

        world = install_fake_gmail(
            monkeypatch, [message("m1", "t1")], date_range_end=date(2026, 6, 30)
        )

        extract_new_items()

        # The end date is inclusive, so `before:` targets the day after it.
        expected_until = datetime(2026, 7, 1, tzinfo=UTC)
        assert f"before:{int(expected_until.timestamp())}" in world["last_query"]


class TestConnectionCheckHelpers:
    def test_build_service_and_get_credentials_are_importable(self):
        # Smoke test: agent/connection_check.py imports these two names
        # directly from this module — a rename here would silently break
        # that import at runtime (only caught by an actual connection
        # check), not at collection time.
        from extractors.gmail import _build_service, _get_credentials

        assert callable(_build_service)
        assert callable(_get_credentials)
