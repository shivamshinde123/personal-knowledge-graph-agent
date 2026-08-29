"""Tests for the answer synthesizer.

The LLM provider is faked (deterministic, no real call needed to test
context assembly/ordering); SQLite is real (:memory:), matching the rest
of the pipeline/agent test suites' convention.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.merger import MergedResult
from agent.synthesizer import synthesize
from storage.sqlite_store import Chunk, Item, connect, insert_chunk, insert_item


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def set_top_k(monkeypatch):
    monkeypatch.setattr(
        "agent.synthesizer.get_settings",
        lambda: SimpleNamespace(
            config=SimpleNamespace(retrieval=SimpleNamespace(top_k_vector=2))
        ),
    )


class FakeProvider:
    """A fake ProviderInterface recording each generate_answer() call."""

    def __init__(self, answer="Here is the answer [1]."):
        """Return ``answer`` for every call."""
        self._answer = answer
        self.calls: list[tuple[str, list]] = []

    def generate_answer(self, question, context):
        self.calls.append((question, list(context)))
        return self._answer


def make_item(**overrides) -> Item:
    defaults = dict(
        id="item-1",
        source_type="notion",
        source_ref_id="page-1",
        title="Design notes",
        url_or_path="https://notion.so/page-1",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        ingested_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Item(**defaults)


def ingest(conn, *, item_id, texts, **item_overrides):
    insert_item(conn, make_item(id=item_id, source_ref_id=item_id, **item_overrides))
    for i, text in enumerate(texts):
        insert_chunk(
            conn,
            Chunk(
                id=f"{item_id}-c{i}",
                item_id=item_id,
                chunk_index=i,
                text=text,
                embedding_id=f"{item_id}-e{i}",
            ),
        )


class TestSynthesize:
    def test_passes_question_and_context_to_the_provider(self, conn, monkeypatch):
        ingest(conn, item_id="item-1", texts=["The storage layer uses SQLite."])
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = synthesize(
            conn, "What does the storage layer use?", [MergedResult("item-1", 1.0)]
        )

        assert result.answer == "Here is the answer [1]."
        question, context = provider.calls[0]
        assert question == "What does the storage layer use?"
        assert context[0].text == "The storage layer uses SQLite."
        assert context[0].source_type == "notion"
        assert context[0].title == "Design notes"
        assert context[0].url == "https://notion.so/page-1"

    def test_sources_match_context_order(self, conn, monkeypatch):
        ingest(conn, item_id="item-a", texts=["text a"], title="A")
        ingest(conn, item_id="item-b", texts=["text b"], title="B")
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = synthesize(
            conn,
            "question",
            [MergedResult("item-a", 2.0), MergedResult("item-b", 1.0)],
        )

        assert [s.item_id for s in result.sources] == ["item-a", "item-b"]
        assert [s.title for s in result.sources] == ["A", "B"]

    def test_multiple_chunks_of_one_item_are_joined_into_one_context_entry(
        self, conn, monkeypatch
    ):
        ingest(conn, item_id="item-1", texts=["first chunk", "second chunk"])
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        synthesize(conn, "question", [MergedResult("item-1", 1.0)])

        _, context = provider.calls[0]
        assert len(context) == 1
        assert "first chunk" in context[0].text
        assert "second chunk" in context[0].text

    def test_result_capped_at_top_k(self, conn, monkeypatch):
        ingest(conn, item_id="item-1", texts=["a"])
        ingest(conn, item_id="item-2", texts=["b"])
        ingest(conn, item_id="item-3", texts=["c"])
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        # top_k patched to 2 via the set_top_k fixture
        synthesize(
            conn,
            "question",
            [
                MergedResult("item-1", 3.0),
                MergedResult("item-2", 2.0),
                MergedResult("item-3", 1.0),
            ],
        )

        _, context = provider.calls[0]
        assert len(context) == 2

    def test_a_deleted_item_is_skipped_not_fatal(self, conn, monkeypatch):
        ingest(conn, item_id="item-1", texts=["still here"])
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = synthesize(
            conn,
            "question",
            [MergedResult("item-gone", 2.0), MergedResult("item-1", 1.0)],
        )

        assert [s.item_id for s in result.sources] == ["item-1"]

    def test_an_item_with_no_chunks_is_skipped(self, conn, monkeypatch):
        insert_item(conn, make_item(id="item-empty", source_ref_id="item-empty"))
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = synthesize(conn, "question", [MergedResult("item-empty", 1.0)])

        assert result.sources == []
        assert provider.calls == []

    def test_no_usable_context_returns_fixed_answer_without_calling_provider(
        self, conn, monkeypatch
    ):
        provider = FakeProvider()
        monkeypatch.setattr("agent.synthesizer.get_provider", lambda task: provider)

        result = synthesize(conn, "question", [])

        assert result.sources == []
        assert provider.calls == []
        assert result.answer
