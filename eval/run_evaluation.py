"""LangSmith evaluation runner: scores the agent against test_questions.json.

Per ``docs/File_Folder_Structure.docx``'s ``eval/`` layout and
``docs/Technical_Design_Document.docx`` section 13.6's "realistic
evaluation workflow" — run this after any meaningful architecture change
(chunking strategy, embedding model, prompt wording, etc.) to get a
numbers-based answer to "did this actually help," not a subjective one.

Uploads ``eval/test_questions.json`` as a LangSmith Dataset (idempotent —
reuses one already there under ``_DATASET_NAME`` rather than duplicating
it every run) and runs each question through the real
``agent/graph.py::run()``, using the ``langsmith`` SDK's ``evaluate()`` so
results land as a real, browsable experiment in the configured LangSmith
project — not just local console output.

Makes real calls: one real agent run per question (embeddings + whichever
LLM tasks the router selects, per the configured ``provider_mode``), plus
two real ``"eval"``-task judge calls per question (faithfulness,
relevance) — in ``mixed``/``fully_cloud`` mode, the judge calls are real,
billed OpenRouter requests. Run deliberately, not automatically.

Run via ``uv run python -m eval.run_evaluation``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langsmith import Client
from langsmith.evaluation import evaluate

from agent.graph import run as run_agent
from agent.tracing import enable_tracing
from config.settings import get_settings
from eval.evaluators import evaluate_faithfulness, evaluate_relevance, recall_at_k
from providers.base import ProviderInterface, get_provider
from storage.chroma_store import get_collection
from storage.neo4j_store import get_driver
from storage.sqlite_store import connect, get_chunks_for_item

logger = logging.getLogger(__name__)

_TEST_QUESTIONS_PATH = Path(__file__).resolve().parent / "test_questions.json"
_DATASET_NAME = "pkg-agent-eval-questions"


def load_test_questions(
    path: Path = _TEST_QUESTIONS_PATH,
) -> list[dict[str, Any]]:
    """Load the hand-written evaluation question set.

    Args:
        path: Defaults to ``eval/test_questions.json``.

    Returns:
        Each question as a dict with ``id``, ``question``, ``category``,
        ``expected_item_ids``, and ``expected_answer_summary``.
    """
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _ensure_dataset(client: Client, questions: list[dict[str, Any]]) -> str:
    """Create the LangSmith dataset if it doesn't exist yet; return its id.

    Reuses an existing dataset under ``_DATASET_NAME`` rather than
    duplicating it on every run — the question set is meant to stay
    stable across evaluation runs so results are comparable over time
    (see the module docstring's "realistic evaluation workflow").
    """
    existing = list(client.list_datasets(dataset_name=_DATASET_NAME))
    if existing:
        return str(existing[0].id)

    dataset = client.create_dataset(
        _DATASET_NAME,
        description=(
            "Personal Knowledge Graph Agent — hand-written evaluation "
            "questions against the real ingested corpus. See "
            "eval/test_questions.json."
        ),
    )
    client.create_examples(
        dataset_id=dataset.id,
        examples=[
            {
                "inputs": {"question": q["question"]},
                "outputs": {
                    "expected_item_ids": q["expected_item_ids"],
                    "expected_answer_summary": q["expected_answer_summary"],
                },
                "metadata": {"id": q["id"], "category": q["category"]},
            }
            for q in questions
        ],
    )
    return str(dataset.id)


def _make_target(conn, collection, driver):
    """Build the function ``evaluate()`` runs against each dataset example."""

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        result = run_agent(conn, collection, driver, inputs["question"])
        context = [
            "\n\n".join(
                chunk.text for chunk in get_chunks_for_item(conn, source.item_id)
            )
            for source in result.sources
        ]
        return {
            "answer": result.answer,
            "retrieved_item_ids": [s.item_id for s in result.sources],
            "context": context,
        }

    return target


def _make_evaluators(judge_provider: ProviderInterface):
    """Build the three LangSmith-shaped evaluator functions."""

    def recall_evaluator(run, example) -> dict[str, Any]:
        expected = example.outputs.get("expected_item_ids", [])
        retrieved = run.outputs.get("retrieved_item_ids", [])
        result = recall_at_k(expected, retrieved)
        return {"key": "recall_at_k", "score": result.recall_at_k}

    def faithfulness_evaluator(run, example) -> dict[str, Any]:
        question = example.inputs["question"]
        answer = run.outputs.get("answer", "")
        context = run.outputs.get("context", [])
        judgment = evaluate_faithfulness(judge_provider, question, answer, context)
        return {
            "key": "faithfulness",
            "score": judgment.score,
            "comment": judgment.reasoning,
        }

    def relevance_evaluator(run, example) -> dict[str, Any]:
        question = example.inputs["question"]
        answer = run.outputs.get("answer", "")
        judgment = evaluate_relevance(judge_provider, question, answer)
        return {
            "key": "relevance",
            "score": judgment.score,
            "comment": judgment.reasoning,
        }

    return [recall_evaluator, faithfulness_evaluator, relevance_evaluator]


def main() -> None:
    """Run the full evaluation and print a summary."""
    logging.basicConfig(level=get_settings().env.log_level)
    if not enable_tracing():
        logger.warning(
            "LANGSMITH_API_KEY is not configured — evaluation will still "
            "run, but without tracing, and dataset upload will fail."
        )

    conn = connect()
    collection = get_collection()
    driver = get_driver()
    try:
        questions = load_test_questions()
        client = Client()
        dataset_id = _ensure_dataset(client, questions)
        judge_provider = get_provider("eval")

        results = evaluate(
            _make_target(conn, collection, driver),
            data=dataset_id,
            evaluators=_make_evaluators(judge_provider),
            experiment_prefix="pkg-agent-eval",
            client=client,
        )
        logger.info("Evaluation complete: %s", results)
    finally:
        driver.close()
        conn.close()


if __name__ == "__main__":
    main()
