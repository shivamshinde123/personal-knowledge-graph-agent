"""Tests for eval/evaluators.py.

recall_at_k is pure — tested directly. The LLM-judge scorers are tested
against a fake ProviderInterface, same pattern as tests/test_providers/.
"""

from eval.evaluators import evaluate_faithfulness, evaluate_relevance, recall_at_k
from providers.base import EvalJudgment


class FakeEvalProvider:
    """Records calls and returns a fixed judgment."""

    def __init__(self, judgment=None):
        """Use ``judgment`` for every call, or a default if omitted."""
        self._judgment = judgment or EvalJudgment(score=0.75, reasoning="looks fine")
        self.calls: list[tuple[str, str, str, tuple]] = []

    def generate_eval_judgment(self, criterion, question, answer, context):
        self.calls.append((criterion, question, answer, tuple(context)))
        return self._judgment


class TestRecallAtK:
    def test_all_expected_items_retrieved_scores_1(self):
        result = recall_at_k(["a", "b"], ["a", "b", "c"])

        assert result.recall_at_k == 1.0
        assert result.matched_item_ids == {"a", "b"}

    def test_none_retrieved_scores_0(self):
        result = recall_at_k(["a", "b"], ["x", "y"])

        assert result.recall_at_k == 0.0
        assert result.matched_item_ids == set()

    def test_partial_match_scores_the_fraction_found(self):
        result = recall_at_k(["a", "b", "c", "d"], ["a", "c", "z"])

        assert result.recall_at_k == 0.5
        assert result.matched_item_ids == {"a", "c"}

    def test_no_expected_items_scores_1_since_nothing_could_be_missed(self):
        result = recall_at_k([], ["a", "b"])

        assert result.recall_at_k == 1.0

    def test_exposes_expected_and_retrieved_as_sets(self):
        result = recall_at_k(["a", "a", "b"], ["b", "b"])

        assert result.expected_item_ids == {"a", "b"}
        assert result.retrieved_item_ids == {"b"}


class TestEvaluateFaithfulness:
    def test_delegates_to_the_provider_with_the_faithfulness_criterion(self):
        provider = FakeEvalProvider()

        judgment = evaluate_faithfulness(provider, "q", "a", ["ctx1", "ctx2"])

        assert judgment.score == 0.75
        assert provider.calls == [("faithfulness", "q", "a", ("ctx1", "ctx2"))]


class TestEvaluateRelevance:
    def test_delegates_to_the_provider_with_the_relevance_criterion(self):
        provider = FakeEvalProvider()

        judgment = evaluate_relevance(provider, "q", "a")

        assert judgment.score == 0.75
        assert provider.calls == [("relevance", "q", "a", ())]

    def test_accepts_optional_context_for_a_uniform_call_shape(self):
        provider = FakeEvalProvider()

        evaluate_relevance(provider, "q", "a", ["ctx"])

        assert provider.calls == [("relevance", "q", "a", ("ctx",))]
