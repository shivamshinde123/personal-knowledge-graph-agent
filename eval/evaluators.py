"""Evaluators: recall@K (pure) and faithfulness/relevance (LLM-as-judge).

Per ``docs/File_Folder_Structure.docx``'s ``eval/`` layout and
``docs/Technical_Design_Document.docx`` section 13.4/13.6. Used by
``eval/run_evaluation.py`` to score each real agent run against
``eval/test_questions.json``'s expected sources. The LLM-judge scorers go
through ``ProviderInterface`` like every other LLM call in the system —
never a raw LangChain/LangSmith call — per ``CLAUDE.md``'s "never bypass
the LLM Provider abstraction" rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from providers.base import EvalJudgment, ProviderInterface


@dataclass(frozen=True, slots=True)
class RecallResult:
    """How many of a question's expected sources were actually retrieved."""

    recall_at_k: float
    expected_item_ids: frozenset[str]
    retrieved_item_ids: frozenset[str]
    matched_item_ids: frozenset[str]


def recall_at_k(
    expected_item_ids: Sequence[str], retrieved_item_ids: Sequence[str]
) -> RecallResult:
    """Fraction of a question's expected sources that were retrieved.

    Args:
        expected_item_ids: The item ids a correct answer should have cited
            (``eval/test_questions.json``'s ``expected_item_ids``).
        retrieved_item_ids: The item ids the agent actually retrieved/cited
            for this question (``QueryResult.sources``' ``item_id``s).

    Returns:
        1.0 if ``expected_item_ids`` is empty (nothing to have missed);
        otherwise the fraction of expected ids found among the retrieved
        ones.
    """
    expected = frozenset(expected_item_ids)
    retrieved = frozenset(retrieved_item_ids)
    if not expected:
        return RecallResult(1.0, expected, retrieved, frozenset())
    matched = expected & retrieved
    return RecallResult(len(matched) / len(expected), expected, retrieved, matched)


def evaluate_faithfulness(
    provider: ProviderInterface, question: str, answer: str, context: Sequence[str]
) -> EvalJudgment:
    """Score whether ``answer`` states only things supported by ``context``.

    Args:
        provider: A provider from ``get_provider("eval")``.
        question: The question that was asked (background only).
        answer: The agent's synthesized answer.
        context: The retrieved chunk texts the answer was grounded in.

    Returns:
        A faithfulness judgment — see
        :meth:`providers.base.ProviderInterface.generate_eval_judgment`.
    """
    return provider.generate_eval_judgment("faithfulness", question, answer, context)


def evaluate_relevance(
    provider: ProviderInterface,
    question: str,
    answer: str,
    context: Sequence[str] = (),
) -> EvalJudgment:
    """Score whether ``answer`` actually addresses ``question``.

    Args:
        provider: A provider from ``get_provider("eval")``.
        question: The question that was asked.
        answer: The agent's synthesized answer.
        context: Unused by this criterion; accepted for a uniform call
            shape alongside :func:`evaluate_faithfulness`.

    Returns:
        A relevance judgment — see
        :meth:`providers.base.ProviderInterface.generate_eval_judgment`.
    """
    return provider.generate_eval_judgment("relevance", question, answer, context)
