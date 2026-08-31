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


def child_page_block(page_id, title):
    """A nested-subpage block — no ``rich_text``, unlike other block types."""
    return {
        "type": "child_page",
        "id": page_id,
        "has_children": True,
        "child_page": {"title": title},
    }


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


class FakePages:
    def __init__(self, pages_by_id):
        """Hold a fake ``retrieve`` endpoint, mirroring ``Client.pages``."""
        self._pages_by_id = pages_by_id
        self.calls: list[str] = []

    def retrieve(self, page_id):
        self.calls.append(page_id)
        if page_id not in self._pages_by_id:
            raise ValueError(f"no such page: {page_id}")
        return self._pages_by_id[page_id]


class FakeClient:
    def __init__(self, pages, blocks_by_parent, auth=None):
        """A fake ``notion_client.Client`` exposing ``search``/``blocks``/``pages``."""
        self._pages = pages
        self.blocks = FakeBlocks(blocks_by_parent)
        self.pages = FakePages({page["id"]: page for page in pages})

    def search(self, filter=None, start_cursor=None):
        return {"results": self._pages, "has_more": False, "next_cursor": None}


def install_fake_client(
    monkeypatch, pages, blocks_by_parent, api_key="secret", notion_page_ids=()
):
    monkeypatch.setattr(
        "extractors.notion.get_settings",
        lambda: SimpleNamespace(
            env=SimpleNamespace(
                notion_api_key=api_key, notion_page_ids_list=list(notion_page_ids)
            )
        ),
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


class TestSubpages:
    """A Notion subpage nested under a scoped/root page — see DECISIONS.md."""

    def test_a_subpage_becomes_its_own_item_not_merged_into_the_parent(
        self, monkeypatch
    ):
        parent = make_page("parent", "Parent")
        subpage = make_page("sub-1", "Subpage")
        blocks_by_parent = {
            "parent": [
                block("paragraph", "Parent text"),
                child_page_block("sub-1", "Subpage"),
            ],
            "sub-1": [block("paragraph", "Subpage text")],
        }
        # Scoped mode (matching real usage — a workspace-search result
        # already includes subpages as their own root pages, but scoped
        # ingestion only ever calls pages.retrieve(), so `subpage` must be
        # registered there without also being an independent root — see
        # TestSubpages.test_a_page_reachable_as_both_a_root_and_a_subpage_
        # is_not_duplicated for the unscoped/workspace-mode case).
        install_fake_client(
            monkeypatch,
            [parent, subpage],
            blocks_by_parent,
            notion_page_ids=["parent"],
        )

        items = extract_new_items()

        by_id = {item.source_ref_id: item for item in items}
        assert by_id["parent"].raw_text == "Parent text"
        assert by_id["sub-1"].raw_text == "Subpage text"
        assert by_id["sub-1"].title == "Subpage"
        assert by_id["sub-1"].url_or_path == "https://notion.so/sub-1"

    def test_subpages_nest_arbitrarily_deep(self, monkeypatch):
        parent = make_page("parent", "Parent")
        sub_1 = make_page("sub-1", "Sub 1")
        sub_2 = make_page("sub-2", "Sub 2")
        blocks_by_parent = {
            "parent": [child_page_block("sub-1", "Sub 1")],
            "sub-1": [
                block("paragraph", "Sub 1 text"),
                child_page_block("sub-2", "Sub 2"),
            ],
            "sub-2": [block("paragraph", "Sub 2 text")],
        }
        install_fake_client(
            monkeypatch,
            [parent, sub_1, sub_2],
            blocks_by_parent,
            notion_page_ids=["parent"],
        )

        items = extract_new_items()

        assert {item.source_ref_id for item in items} == {"sub-1", "sub-2"}

    def test_a_subpage_is_discovered_even_when_the_parent_is_unchanged(
        self, monkeypatch
    ):
        parent = make_page(
            "parent", "Parent", last_edited_time="2026-08-01T00:00:00.000Z"
        )
        subpage = make_page(
            "sub-1", "Subpage", last_edited_time="2026-08-10T00:00:00.000Z"
        )
        blocks_by_parent = {
            "parent": [child_page_block("sub-1", "Subpage")],
            "sub-1": [block("paragraph", "Subpage text")],
        }
        install_fake_client(
            monkeypatch,
            [parent, subpage],
            blocks_by_parent,
            notion_page_ids=["parent"],
        )

        items = extract_new_items(since=datetime(2026, 8, 5, tzinfo=UTC))

        # The parent is unchanged since `since` and is correctly excluded,
        # but its subpage — independently newer — must still surface.
        assert [item.source_ref_id for item in items] == ["sub-1"]

    def test_an_unfetchable_subpage_is_skipped_not_raised(self, monkeypatch):
        parent = make_page("parent", "Parent")
        blocks_by_parent = {
            "parent": [
                block("paragraph", "Parent text"),
                child_page_block("does-not-exist", "Ghost"),
            ]
        }
        install_fake_client(monkeypatch, [parent], blocks_by_parent)

        items = extract_new_items()

        assert [item.source_ref_id for item in items] == ["parent"]

    def test_a_page_reachable_as_both_a_root_and_a_subpage_is_not_duplicated(
        self, monkeypatch
    ):
        parent = make_page("parent", "Parent")
        subpage = make_page("sub-1", "Also root")
        blocks_by_parent = {
            "parent": [child_page_block("sub-1", "Also root")],
            "sub-1": [block("paragraph", "text")],
        }
        install_fake_client(monkeypatch, [parent, subpage], blocks_by_parent)

        items = extract_new_items()

        ids = [item.source_ref_id for item in items]
        assert ids.count("sub-1") == 1


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
            lambda: SimpleNamespace(
                env=SimpleNamespace(notion_api_key="secret", notion_page_ids_list=[])
            ),
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
            lambda: SimpleNamespace(
                env=SimpleNamespace(notion_api_key="secret", notion_page_ids_list=[])
            ),
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


class TestScopedToConfiguredPages:
    """``notion_page_ids_list`` restricts ingestion to specific pages.

    Fetched directly by id, not via the workspace-wide search — see
    DECISIONS.md, 2026-08-30.
    """

    def test_only_the_configured_pages_are_fetched_not_the_whole_workspace(
        self, monkeypatch
    ):
        wanted = make_page("page-wanted", "Wanted")
        unwanted = make_page("page-unwanted", "Unwanted")
        blocks_by_parent = {
            "page-wanted": [block("paragraph", "keep me")],
            "page-unwanted": [block("paragraph", "should never be fetched")],
        }
        install_fake_client(
            monkeypatch,
            [wanted, unwanted],
            blocks_by_parent,
            notion_page_ids=["page-wanted"],
        )

        items = extract_new_items()

        assert [item.source_ref_id for item in items] == ["page-wanted"]

    def test_search_is_never_called_when_pages_are_scoped(self, monkeypatch):
        page = make_page("page-1", "Scoped")
        blocks_by_parent = {"page-1": [block("paragraph", "text")]}

        class NoSearchFakeClient(FakeClient):
            def search(self, filter=None, start_cursor=None):
                raise AssertionError("search() should not be called when scoped")

        monkeypatch.setattr(
            "extractors.notion.get_settings",
            lambda: SimpleNamespace(
                env=SimpleNamespace(
                    notion_api_key="secret", notion_page_ids_list=["page-1"]
                )
            ),
        )
        monkeypatch.setattr(
            "extractors.notion.Client",
            lambda auth: NoSearchFakeClient([page], blocks_by_parent),
        )

        items = extract_new_items()

        assert len(items) == 1

    def test_an_unfetchable_configured_page_is_skipped_not_raised(self, monkeypatch):
        good_page = make_page("page-good", "Good")
        install_fake_client(
            monkeypatch,
            [good_page],
            {"page-good": [block("paragraph", "fine")]},
            notion_page_ids=["page-good", "page-does-not-exist"],
        )

        items = extract_new_items()

        assert [item.source_ref_id for item in items] == ["page-good"]

    def test_an_empty_scope_falls_back_to_the_whole_workspace(self, monkeypatch):
        page = make_page("page-1", "Unscoped")
        install_fake_client(
            monkeypatch,
            [page],
            {"page-1": [block("paragraph", "text")]},
            notion_page_ids=[],
        )

        items = extract_new_items()

        assert [item.source_ref_id for item in items] == ["page-1"]


class TestOnProgress:
    def test_scoped_mode_reports_a_real_total(self, monkeypatch):
        page_a = make_page("page-a", "Alpha")
        page_b = make_page("page-b", "Beta")
        blocks_by_parent = {
            "page-a": [block("paragraph", "a")],
            "page-b": [block("paragraph", "b")],
        }
        install_fake_client(
            monkeypatch,
            [page_a, page_b],
            blocks_by_parent,
            notion_page_ids=["page-a", "page-b"],
        )
        calls = []

        extract_new_items(
            on_progress=lambda current, total, label: (
                calls.append((current, total, label)) or True
            )
        )

        assert calls == [(1, 2, "Alpha"), (2, 2, "Beta")]

    def test_unscoped_mode_reports_no_total(self, monkeypatch):
        page = make_page("page-1", "Any")
        install_fake_client(
            monkeypatch, [page], {"page-1": [block("paragraph", "text")]}
        )
        calls = []

        extract_new_items(
            on_progress=lambda current, total, label: (
                calls.append((current, total, label)) or True
            )
        )

        assert calls == [(1, None, "Any")]

    def test_returning_false_stops_after_the_current_root_page(self, monkeypatch):
        page_a = make_page("page-a", "Alpha")
        page_b = make_page("page-b", "Beta")
        blocks_by_parent = {
            "page-a": [block("paragraph", "a")],
            "page-b": [block("paragraph", "b")],
        }
        install_fake_client(
            monkeypatch,
            [page_a, page_b],
            blocks_by_parent,
            notion_page_ids=["page-a", "page-b"],
        )

        items = extract_new_items(on_progress=lambda current, total, label: False)

        assert [item.source_ref_id for item in items] == ["page-a"]

    def test_no_callback_is_the_default(self, monkeypatch):
        page = make_page("page-1", "Any")
        install_fake_client(
            monkeypatch, [page], {"page-1": [block("paragraph", "text")]}
        )

        items = extract_new_items()  # must not raise

        assert len(items) == 1
