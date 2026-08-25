"""Tests for the browser history extractor.

Runs against a real, minimal SQLite file shaped like Chrome's ``urls``
table, in a temp directory — not mocked, since the whole point of this
extractor is reading that real on-disk format (including working around
the browser's exclusive lock by copying the file first).
"""

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from extractors.base import ExtractorError
from extractors.browser_history import extract_new_items

_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


def to_webkit_timestamp(dt: datetime) -> int:
    return int((dt - _WEBKIT_EPOCH).total_seconds() * 1_000_000)


def make_history_db(path, rows):
    """Create a minimal Chrome-shaped History SQLite file at ``path``.

    ``rows`` is ``(url, title, visit_count, last_visit_datetime)`` tuples.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE urls (url TEXT, title TEXT, visit_count INTEGER, "
        "last_visit_time INTEGER)"
    )
    conn.executemany(
        "INSERT INTO urls VALUES (?, ?, ?, ?)",
        [
            (url, title, visit_count, to_webkit_timestamp(visited_at))
            for url, title, visit_count, visited_at in rows
        ],
    )
    conn.commit()
    conn.close()


def default_filters(min_visit_count=1, domain_blocklist=None):
    return SimpleNamespace(
        min_visit_count=min_visit_count,
        domain_blocklist=domain_blocklist or [],
    )


def install_settings(monkeypatch, history_path, filters=None):
    monkeypatch.setattr(
        "extractors.browser_history.get_settings",
        lambda: SimpleNamespace(
            env=SimpleNamespace(browser_history_path=history_path),
            config=SimpleNamespace(
                filters=SimpleNamespace(browser_history=filters or default_filters())
            ),
        ),
    )


class TestExtraction:
    def test_extracts_title_as_raw_text_and_url_as_source_ref(
        self, tmp_path, monkeypatch
    ):
        history_path = tmp_path / "History"
        visited_at = datetime(2026, 8, 1, tzinfo=UTC)
        make_history_db(
            history_path,
            [("https://example.com/docs", "Example Docs", 3, visited_at)],
        )
        install_settings(monkeypatch, history_path)

        items = extract_new_items()

        assert len(items) == 1
        item = items[0]
        assert item.source_type == "browser_history"
        assert item.source_ref_id == "https://example.com/docs"
        assert item.url_or_path == "https://example.com/docs"
        assert item.title == "Example Docs"
        assert item.raw_text == "Example Docs"
        assert item.author_or_sender is None
        assert item.created_at is None
        assert item.last_edited_at == visited_at

    def test_multiple_urls_all_extracted(self, tmp_path, monkeypatch):
        history_path = tmp_path / "History"
        visited_at = datetime(2026, 8, 1, tzinfo=UTC)
        make_history_db(
            history_path,
            [
                ("https://a.example.com", "Page A", 2, visited_at),
                ("https://b.example.com", "Page B", 2, visited_at),
            ],
        )
        install_settings(monkeypatch, history_path)

        items = extract_new_items()

        assert {item.source_ref_id for item in items} == {
            "https://a.example.com",
            "https://b.example.com",
        }

    def test_entries_with_no_title_are_skipped(self, tmp_path, monkeypatch):
        history_path = tmp_path / "History"
        visited_at = datetime(2026, 8, 1, tzinfo=UTC)
        make_history_db(history_path, [("https://example.com", None, 5, visited_at)])
        install_settings(monkeypatch, history_path)

        assert extract_new_items() == []


class TestNoiseFiltering:
    def test_visits_below_the_minimum_count_are_excluded(self, tmp_path, monkeypatch):
        history_path = tmp_path / "History"
        visited_at = datetime(2026, 8, 1, tzinfo=UTC)
        make_history_db(
            history_path,
            [
                ("https://incidental.example.com", "Incidental", 1, visited_at),
                ("https://frequent.example.com", "Frequent", 5, visited_at),
            ],
        )
        install_settings(monkeypatch, history_path, default_filters(min_visit_count=2))

        items = extract_new_items()

        assert [item.source_ref_id for item in items] == [
            "https://frequent.example.com"
        ]

    def test_blocklisted_domains_are_excluded(self, tmp_path, monkeypatch):
        history_path = tmp_path / "History"
        visited_at = datetime(2026, 8, 1, tzinfo=UTC)
        make_history_db(
            history_path,
            [
                (
                    "https://www.google.com/search?q=pkg+agent",
                    "pkg agent - Google Search",
                    5,
                    visited_at,
                ),
                (
                    "https://www.google.com/maps/place/x",
                    "Google Maps",
                    5,
                    visited_at,
                ),
            ],
        )
        install_settings(
            monkeypatch,
            history_path,
            default_filters(domain_blocklist=["google.com/search"]),
        )

        items = extract_new_items()

        assert [item.source_ref_id for item in items] == [
            "https://www.google.com/maps/place/x"
        ]

    def test_bare_domain_blocklist_entry_blocks_every_path(self, tmp_path, monkeypatch):
        history_path = tmp_path / "History"
        visited_at = datetime(2026, 8, 1, tzinfo=UTC)
        make_history_db(
            history_path,
            [("https://www.facebook.com/profile", "Profile", 5, visited_at)],
        )
        install_settings(
            monkeypatch,
            history_path,
            default_filters(domain_blocklist=["facebook.com"]),
        )

        assert extract_new_items() == []


class TestIncrementalFiltering:
    def test_entries_last_visited_before_since_are_excluded(
        self, tmp_path, monkeypatch
    ):
        history_path = tmp_path / "History"
        old_visit = datetime(2026, 8, 1, tzinfo=UTC)
        new_visit = datetime(2026, 8, 10, tzinfo=UTC)
        make_history_db(
            history_path,
            [
                ("https://old.example.com", "Old", 3, old_visit),
                ("https://new.example.com", "New", 3, new_visit),
            ],
        )
        install_settings(monkeypatch, history_path)

        items = extract_new_items(since=datetime(2026, 8, 5, tzinfo=UTC))

        assert [item.source_ref_id for item in items] == ["https://new.example.com"]

    def test_since_none_includes_every_surviving_entry(self, tmp_path, monkeypatch):
        history_path = tmp_path / "History"
        make_history_db(
            history_path,
            [("https://example.com", "Any", 3, datetime(2026, 8, 1, tzinfo=UTC))],
        )
        install_settings(monkeypatch, history_path)

        assert len(extract_new_items(since=None)) == 1


class TestErrorHandling:
    def test_missing_path_config_raises_extractor_error(self, monkeypatch):
        install_settings(monkeypatch, None)

        with pytest.raises(ExtractorError):
            extract_new_items()

    def test_nonexistent_file_raises_extractor_error(self, tmp_path, monkeypatch):
        install_settings(monkeypatch, tmp_path / "does-not-exist")

        with pytest.raises(ExtractorError):
            extract_new_items()

    def test_unreadable_file_raises_extractor_error(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "History"
        bad_file.write_text("not a sqlite database", encoding="utf-8")
        install_settings(monkeypatch, bad_file)

        with pytest.raises(ExtractorError):
            extract_new_items()


class TestLiveFileLocking:
    def test_reads_from_a_copy_so_the_live_file_stays_untouched(
        self, tmp_path, monkeypatch
    ):
        history_path = tmp_path / "History"
        make_history_db(
            history_path,
            [("https://example.com", "Example", 3, datetime(2026, 8, 1, tzinfo=UTC))],
        )
        original_bytes = history_path.read_bytes()
        install_settings(monkeypatch, history_path)

        extract_new_items()

        assert history_path.read_bytes() == original_bytes
