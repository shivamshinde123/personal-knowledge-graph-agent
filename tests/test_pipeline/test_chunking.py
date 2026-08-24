"""Tests for text chunking."""

from types import SimpleNamespace

import pytest

from pipeline.chunking import chunk_text


def fake_settings(*, target=10, overlap=3):
    return SimpleNamespace(
        config=SimpleNamespace(
            chunking=SimpleNamespace(
                target_chunk_size_tokens=target, chunk_overlap_tokens=overlap
            )
        )
    )


@pytest.fixture(autouse=True)
def default_config(monkeypatch):
    monkeypatch.setattr("pipeline.chunking.get_settings", lambda: fake_settings())


def words(n: int, prefix: str = "word") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


class TestNaturalBoundaries:
    def test_empty_text_returns_no_chunks(self):
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_single_short_paragraph_is_one_chunk(self):
        text = words(5)

        assert chunk_text(text) == [text]

    def test_short_paragraphs_are_packed_together_under_the_target(self):
        text = f"{words(4)}\n\n{words(4)}"

        chunks = chunk_text(text)

        assert chunks == [f"{words(4)}\n\n{words(4)}"]

    def test_a_new_chunk_starts_once_the_target_would_be_exceeded(self):
        # 6 + 6 = 12 words > target of 10, so they split into two chunks.
        text = f"{words(6, 'a')}\n\n{words(6, 'b')}"

        chunks = chunk_text(text)

        assert chunks == [words(6, "a"), words(6, "b")]

    def test_many_short_paragraphs_each_start_a_new_chunk_once_full(self):
        text = "\n\n".join(words(4, f"p{i}") for i in range(3))

        chunks = chunk_text(text)

        # Each paragraph is 4 words; two fit under target=10, the third
        # doesn't, so it starts a fresh chunk.
        assert chunks == [
            f"{words(4, 'p0')}\n\n{words(4, 'p1')}",
            words(4, "p2"),
        ]


class TestSlidingWindowFallback:
    def test_a_single_paragraph_over_target_is_split_with_overlap(self):
        # No blank-line boundary at all: one long paragraph, 25 words.
        text = words(25)

        chunks = chunk_text(text)

        assert len(chunks) > 1
        # step = target(10) - overlap(3) = 7 words advanced per window.
        assert chunks[0] == words(10)
        assert chunks[1] == " ".join(f"word{i}" for i in range(7, 17))

    def test_sliding_windows_cover_the_full_text_without_gaps(self):
        text = words(23)

        chunks = chunk_text(text)
        covered_words = {w for chunk in chunks for w in chunk.split()}

        assert covered_words == set(words(23).split())

    def test_oversized_paragraph_flushes_the_pending_chunk_first(self):
        small = words(3, "s")
        huge = words(25, "h")
        text = f"{small}\n\n{huge}"

        chunks = chunk_text(text)

        assert chunks[0] == small
        assert chunks[1] == words(10, "h")
