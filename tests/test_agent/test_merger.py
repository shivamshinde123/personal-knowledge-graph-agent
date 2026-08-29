"""Tests for the Reciprocal Rank Fusion result merger."""

from agent.merger import merge
from agent.search_nodes import SearchHit


class TestSingleList:
    def test_preserves_the_input_list_order(self):
        hits = [
            SearchHit(item_id="a", rank=1),
            SearchHit(item_id="b", rank=2),
            SearchHit(item_id="c", rank=3),
        ]

        result = merge(hits)

        assert [r.item_id for r in result] == ["a", "b", "c"]

    def test_a_higher_rank_scores_higher(self):
        hits = [SearchHit(item_id="a", rank=1), SearchHit(item_id="b", rank=5)]

        result = merge(hits)

        assert result[0].item_id == "a"
        assert result[0].score > result[1].score


class TestMultipleLists:
    def test_an_item_in_multiple_lists_outranks_one_in_a_single_list(self):
        vector_hits = [SearchHit(item_id="a", rank=3), SearchHit(item_id="b", rank=1)]
        keyword_hits = [SearchHit(item_id="a", rank=3)]

        result = merge(vector_hits, keyword_hits)

        assert result[0].item_id == "a"

    def test_scores_for_a_shared_item_sum_across_lists(self):
        vector_hits = [SearchHit(item_id="a", rank=1)]
        keyword_hits = [SearchHit(item_id="a", rank=1)]

        [only_vector] = merge(vector_hits)
        [combined] = merge(vector_hits, keyword_hits)

        assert combined.score == only_vector.score * 2

    def test_an_empty_list_contributes_nothing(self):
        vector_hits = [SearchHit(item_id="a", rank=1)]

        result = merge(vector_hits, [])

        assert [r.item_id for r in result] == ["a"]

    def test_three_lists_combine_correctly(self):
        vector_hits = [SearchHit(item_id="a", rank=1), SearchHit(item_id="b", rank=2)]
        keyword_hits = [SearchHit(item_id="b", rank=1)]
        graph_hits = [SearchHit(item_id="c", rank=1), SearchHit(item_id="b", rank=2)]

        result = merge(vector_hits, keyword_hits, graph_hits)

        assert result[0].item_id == "b"
        assert {r.item_id for r in result} == {"a", "b", "c"}


class TestEmptyInput:
    def test_no_lists_returns_empty(self):
        assert merge() == []

    def test_only_empty_lists_returns_empty(self):
        assert merge([], []) == []
