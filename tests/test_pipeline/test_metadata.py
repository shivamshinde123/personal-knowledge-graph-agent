"""Tests for LLM-derived item metadata generation."""

from types import SimpleNamespace

import pytest

from extractors.base import ExtractedItem
from pipeline.metadata import generate_metadata
from providers.base import ItemMetadata, ProviderError


class FakeProvider:
    """A fake ProviderInterface that records calls and can fail on cue."""

    def __init__(self, error_on_call=None):
        """``error_on_call``: raise ``ProviderError`` on this call number, if set."""
        self.calls: list[list[str]] = []
        self._error_on_call = error_on_call

    def generate_metadata(self, texts):
        self.calls.append(list(texts))
        if self._error_on_call is not None and len(self.calls) == self._error_on_call:
            raise ProviderError("boom")
        return [ItemMetadata(project_name="pkg-agent", topic="storage") for _ in texts]


def make_item(**overrides) -> ExtractedItem:
    defaults = dict(
        source_type="local_file",
        source_ref_id="a.txt",
        title="a.txt",
        url_or_path="a.txt",
        raw_text="Some extracted content about the storage layer.",
    )
    defaults.update(overrides)
    return ExtractedItem(**defaults)


@pytest.fixture
def set_group_size(monkeypatch):
    def _set(size):
        monkeypatch.setattr(
            "pipeline.metadata.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(
                    ingestion=SimpleNamespace(batch_metadata_group_size=size)
                )
            ),
        )

    return _set


class TestBatching:
    def test_empty_input_returns_empty_output_without_calling_the_provider(
        self, monkeypatch, set_group_size
    ):
        set_group_size(10)
        provider = FakeProvider()
        monkeypatch.setattr("pipeline.metadata.get_provider", lambda task: provider)

        assert generate_metadata([]) == []
        assert provider.calls == []

    def test_items_are_grouped_per_batch_metadata_group_size(
        self, monkeypatch, set_group_size
    ):
        set_group_size(2)
        provider = FakeProvider()
        monkeypatch.setattr("pipeline.metadata.get_provider", lambda task: provider)
        items = [make_item(title=f"item{i}.txt") for i in range(5)]

        results = generate_metadata(items)

        assert len(results) == 5
        assert [len(call) for call in provider.calls] == [2, 2, 1]

    def test_requests_the_metadata_task_provider(self, monkeypatch, set_group_size):
        set_group_size(10)
        seen_tasks = []
        monkeypatch.setattr(
            "pipeline.metadata.get_provider",
            lambda task: seen_tasks.append(task) or FakeProvider(),
        )

        generate_metadata([make_item()])

        assert seen_tasks == ["metadata"]


class TestRepresentativeText:
    def test_long_items_are_truncated_before_being_sent(
        self, monkeypatch, set_group_size
    ):
        set_group_size(10)
        provider = FakeProvider()
        monkeypatch.setattr("pipeline.metadata.get_provider", lambda task: provider)

        generate_metadata([make_item(raw_text="x" * 5000)])

        assert len(provider.calls[0][0]) <= 2000

    def test_text_is_stripped_before_being_sent(self, monkeypatch, set_group_size):
        set_group_size(10)
        provider = FakeProvider()
        monkeypatch.setattr("pipeline.metadata.get_provider", lambda task: provider)

        generate_metadata([make_item(raw_text="  padded text  ")])

        assert provider.calls[0][0] == "padded text"


class TestGracefulDegradation:
    def test_a_failed_group_degrades_to_empty_metadata_for_that_group_only(
        self, monkeypatch, set_group_size
    ):
        set_group_size(2)
        provider = FakeProvider(error_on_call=2)  # the second group's call fails
        monkeypatch.setattr("pipeline.metadata.get_provider", lambda task: provider)
        items = [make_item(title=f"item{i}.txt") for i in range(4)]

        results = generate_metadata(items)

        assert len(results) == 4
        assert results[0] == ItemMetadata(project_name="pkg-agent", topic="storage")
        assert results[1] == ItemMetadata(project_name="pkg-agent", topic="storage")
        assert results[2] == ItemMetadata(project_name=None, topic=None)
        assert results[3] == ItemMetadata(project_name=None, topic=None)
