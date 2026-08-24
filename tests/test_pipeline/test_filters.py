"""Tests for cross-source content-quality filtering."""

from types import SimpleNamespace

import pytest

from extractors.base import ExtractedItem
from pipeline.filters import apply_noise_filter


def make_item(**overrides) -> ExtractedItem:
    defaults = dict(
        source_type="local_file",
        source_ref_id="C:/notes/a.txt",
        title="a.txt",
        url_or_path="C:/notes/a.txt",
        raw_text="This is a reasonably long piece of extracted content.",
    )
    defaults.update(overrides)
    return ExtractedItem(**defaults)


@pytest.fixture(autouse=True)
def min_content_length(monkeypatch):
    monkeypatch.setattr(
        "pipeline.filters.get_settings",
        lambda: SimpleNamespace(
            config=SimpleNamespace(filters=SimpleNamespace(min_content_length=10))
        ),
    )


class TestApplyNoiseFilter:
    def test_keeps_items_at_or_above_the_minimum_length(self):
        assert apply_noise_filter(make_item(raw_text="exactly ten")) is True

    def test_drops_items_below_the_minimum_length(self):
        assert apply_noise_filter(make_item(raw_text="short")) is False

    def test_drops_empty_text(self):
        assert apply_noise_filter(make_item(raw_text="")) is False

    def test_drops_whitespace_only_text(self):
        assert apply_noise_filter(make_item(raw_text="   \n\t  ")) is False

    def test_length_is_measured_after_stripping_whitespace(self):
        assert apply_noise_filter(make_item(raw_text="   short   ")) is False
