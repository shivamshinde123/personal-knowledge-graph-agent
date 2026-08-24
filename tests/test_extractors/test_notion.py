"""Tests for the Notion extractor.

The real Notion API isn't reachable in tests, so ``notion_client.Client``
is replaced with a fake implementing just the two endpoints this module
calls (``search`` and ``blocks.children.list``), shaped like the real
API's JSON responses.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from extractors.base import ExtractorError
from extractors.notion import extract_new_items


def rich_text(text):
    return [{"plain_text": text}]


def block(block_type, text=None, id_="block-1", has_children=False):
    entry = {"type": block_type, "id": id_, "has_children": has_children}
    if text is not None:
        entry[block_type] = {"rich_text": rich_text(text)}
    else:
        entry[block_type] = {}
    return entry


def make_page(
    page_id,
    title,
    *,
    created_time="2026-08-01T00:00:00.000Z",
    last_edited_time="2026-08-01T00:00:00.000Z",
    url=None,
):
    return {
        "id": page_id,
        "url": url or f"https://notion.so/{page_id}",
        "created_time": created_time,
        "last_edited_time": last_edited_time,
        "properties": {
            "title": {"type": "title", "title": rich_text(title)},
        },
    }


class FakeBlocksChildren:
    def __init__(self, blocks_by_parent):
        """Map parent block/page id to the list of child block dicts."""
        self._blocks_by_parent = blocks_by_parent
        self.calls: list[str] = []

    def list(self, block_id, start_cursor=None):
        self.calls.append(block_id)
        return {
            "results": self._blocks_by_parent.get(block_id, []),
            "has_more": False,
            "next_cursor": None,
        }


class FakeBlocks:
    def __init__(self, blocks_by_parent):
        """Hold a fake ``children`` endpoint, mirroring ``Client.blocks``."""
        self.children = FakeBlocksChildren(blocks_by_parent)


class FakeClient:
    def __init__(self, pages, blocks_by_parent, auth=None):
        """A fake ``notion_client.Client`` exposing just ``search``/``blocks``."""
        self._pages = pages
        self.blocks = FakeBlocks(blocks_by_parent)

    def search(self, filter=None, start_cursor=None):
        return {"results": self._pages, "has_more": False, "next_cursor": None}


def install_fake_client(monkeypatch, pages, blocks_by_parent, api_key="secret"):
    monkeypatch.setattr(
        "extractors.notion.get_settings",
        lambda: SimpleNamespace(env=SimpleNamespace(notion_api_key=api_key)),
    )
    monkeypatch.setattr(
        "extractors.notion.Client",
        lambda auth: FakeClient(pages, blocks_by_parent, auth=auth),
    )


class TestExtraction:
    def test_extracts_page_title_and_blocks_as_plain_text(self, monkeypatch):
        page = make_page("page-1", "Design Notes")
        blocks_by_parent = {
            "page-1": [
                block("heading_1", "Overview"),
                block("paragraph", "The storage layer uses SQLite."),
            ]
        }
        install_fake_client(monkeypatch, [page], blocks_by_parent)

        items = extract_new_items()

        assert len(items) == 1
        item = items[0]
        assert item.source_type == "notion"
        assert item.source_ref_id == "page-1"
        assert item.title == "Design Notes"
        assert item.url_or_path == "https://notion.so/page-1"
        assert item.raw_text == "Overview\n\nThe storage layer uses SQLite."
        assert item.created_at == datetime(2026, 8, 1, tzinfo=UTC)
        assert item.last_edited_at == datetime(2026, 8, 1, tzinfo=UTC)

    def test_recurses_into_nested_children(self, monkeypatch):
        page = make_page("page-1", "Nested")
        blocks_by_parent = {
            "page-1": [
                block("bulleted_list_item", "Top item", id_="li-1", has_children=True)
            ],
            "li-1": [block("paragraph", "Nested detail")],
        }
        install_fake_client(monkeypatch, [page], blocks_by_parent)

        items = extract_new_items()

        assert items[0].raw_text == "Top item\n\nNested detail"

    def test_blocks_without_rich_text_are_skipped(self, monkeypatch):
        page = make_page("page-1", "Mixed")
        blocks_by_parent = {
            "page-1": [
                block("paragraph", "Has text"),
                block("divider"),
                block("paragraph", "More text"),
            ]
        }
        install_fake_client(monkeypatch, [page], blocks_by_parent)

        items = extract_new_items()

        assert items[0].raw_text == "Has text\n\nMore text"

    def test_page_with_no_text_content_is_skipped(self, monkeypatch):
        page = make_page("page-1", "Empty")
        install_fake_client(monkeypatch, [page], {"page-1": []})

        assert extract_new_items() == []

    def test_page_missing_title_property_falls_back_to_untitled(self, monkeypatch):
        page = make_page("page-1", "irrelevant")
        page["properties"] = {}
        install_fake_client(
            monkeypatch, [page], {"page-1": [block("paragraph", "Body")]}
        )

        items = extract_new_items()

        assert items[0].title == "Untitled"


class TestIncrementalFiltering:
    def test_pages_edited_before_since_are_excluded(self, monkeypatch):
        old_page = make_page(
            "page-old", "Old", last_edited_time="2026-08-01T00:00:00.000Z"
        )
        new_page = make_page(
            "page-new", "New", last_edited_time="2026-08-10T00:00:00.000Z"
        )
        blocks_by_parent = {
            "page-old": [block("paragraph", "old text")],
            "page-new": [block("paragraph", "new text")],
        }
        install_fake_client(monkeypatch, [old_page, new_page], blocks_by_parent)

        items = extract_new_items(since=datetime(2026, 8, 5, tzinfo=UTC))

        assert [item.source_ref_id for item in items] == ["page-new"]

    def test_since_none_includes_every_page(self, monkeypatch):
        page = make_page("page-1", "Any")
        install_fake_client(
            monkeypatch, [page], {"page-1": [block("paragraph", "text")]}
        )

        items = extract_new_items(since=None)

        assert len(items) == 1


class TestErrorHandling:
    def test_missing_api_key_raises_extractor_error(self, monkeypatch):
        monkeypatch.setattr(
            "extractors.notion.get_settings",
            lambda: SimpleNamespace(env=SimpleNamespace(notion_api_key=None)),
        )

        with pytest.raises(ExtractorError):
            extract_new_items()

    def test_search_failure_raises_extractor_error(self, monkeypatch):
        monkeypatch.setattr(
            "extractors.notion.get_settings",
            lambda: SimpleNamespace(env=SimpleNamespace(notion_api_key="secret")),
        )

        class ExplodingClient:
            def __init__(self, auth=None):
                pass

            def search(self, filter=None, start_cursor=None):
                raise RuntimeError("boom")

        monkeypatch.setattr("extractors.notion.Client", ExplodingClient)

        with pytest.raises(ExtractorError):
            extract_new_items()

    def test_a_single_pages_fetch_failure_is_skipped_not_raised(self, monkeypatch):
        good_page = make_page("page-good", "Good")
        bad_page = make_page("page-bad", "Bad")
        blocks_by_parent = {"page-good": [block("paragraph", "fine")]}

        class ExplodingChildren(FakeBlocksChildren):
            def list(self, block_id, start_cursor=None):
                if block_id == "page-bad":
                    raise RuntimeError("boom")
                return super().list(block_id, start_cursor)

        monkeypatch.setattr(
            "extractors.notion.get_settings",
            lambda: SimpleNamespace(env=SimpleNamespace(notion_api_key="secret")),
        )
        fake = FakeClient([bad_page, good_page], blocks_by_parent)
        fake.blocks.children = ExplodingChildren(blocks_by_parent)
        monkeypatch.setattr("extractors.notion.Client", lambda auth: fake)

        items = extract_new_items()

        assert [item.source_ref_id for item in items] == ["page-good"]


class TestProgressLogging:
    def test_logs_progress_every_interval_and_a_final_summary(
        self, monkeypatch, caplog
    ):
        pages = [make_page(f"page-{i}", f"Page {i}") for i in range(30)]
        blocks_by_parent = {page["id"]: [block("paragraph", "text")] for page in pages}
        install_fake_client(monkeypatch, pages, blocks_by_parent)

        with caplog.at_level("INFO", logger="extractors.notion"):
            items = extract_new_items()

        assert len(items) == 30
        progress_logs = [r for r in caplog.records if "scanned 25" in r.message]
        assert len(progress_logs) == 1
        assert any(
            "finished" in r.message and "30" in r.message for r in caplog.records
        )
