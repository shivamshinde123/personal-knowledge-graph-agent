"""Tests for eval/run_evaluation.py.

No real LangSmith or agent calls anywhere here — the langsmith Client,
agent.graph.run, and the judge provider are all faked.
"""

from types import SimpleNamespace

from eval.run_evaluation import (
    _ensure_dataset,
    _make_evaluators,
    _make_target,
    load_test_questions,
)
from providers.base import EvalJudgment


class TestLoadTestQuestions:
    def test_loads_the_real_question_set(self):
        questions = load_test_questions()

        assert len(questions) >= 20
        for q in questions:
            assert set(q.keys()) == {
                "id",
                "question",
                "category",
                "expected_item_ids",
                "expected_answer_summary",
            }

    def test_loads_from_a_given_path(self, tmp_path):
        path = tmp_path / "questions.json"
        path.write_text(
            '[{"id": "q1", "question": "x", "category": "factual", '
            '"expected_item_ids": [], "expected_answer_summary": ""}]',
            encoding="utf-8",
        )

        questions = load_test_questions(path)

        assert questions[0]["id"] == "q1"


class FakeDataset:
    def __init__(self, id_):
        """Wrap a dataset id, matching langsmith's Dataset.id attribute."""
        self.id = id_


class FakeClient:
    def __init__(self, existing=None):
        """Start with ``existing`` datasets (default none) already present."""
        self._existing = existing or []
        self.created_dataset = None
        self.created_examples = None

    def list_datasets(self, dataset_name):
        return iter(self._existing)

    def create_dataset(self, name, description=None):
        self.created_dataset = FakeDataset("new-dataset-id")
        return self.created_dataset

    def create_examples(self, dataset_id, examples):
        self.created_examples = (dataset_id, examples)


class TestEnsureDataset:
    def test_reuses_an_existing_dataset(self):
        client = FakeClient(existing=[FakeDataset("existing-id")])

        dataset_id = _ensure_dataset(client, [])

        assert dataset_id == "existing-id"
        assert client.created_dataset is None

    def test_creates_and_populates_a_new_dataset(self):
        client = FakeClient()
        questions = [
            {
                "id": "q1",
                "question": "What is X?",
                "category": "factual",
                "expected_item_ids": ["item-1"],
                "expected_answer_summary": "X is Y.",
            }
        ]

        dataset_id = _ensure_dataset(client, questions)

        assert dataset_id == "new-dataset-id"
        created_id, examples = client.created_examples
        assert created_id == "new-dataset-id"
        assert examples[0]["inputs"] == {"question": "What is X?"}
        assert examples[0]["outputs"]["expected_item_ids"] == ["item-1"]
        assert examples[0]["metadata"] == {"id": "q1", "category": "factual"}


class FakeSource:
    def __init__(self, item_id):
        """Wrap an item id, matching agent.synthesizer.Source.item_id."""
        self.item_id = item_id


class FakeChunk:
    def __init__(self, text):
        """Wrap chunk text, matching storage.sqlite_store.Chunk.text."""
        self.text = text


class TestMakeTarget:
    def test_returns_answer_retrieved_ids_and_joined_context(self, monkeypatch):
        fake_result = SimpleNamespace(
            answer="The answer.",
            sources=[FakeSource("item-1"), FakeSource("item-2")],
        )
        monkeypatch.setattr(
            "eval.run_evaluation.run_agent",
            lambda conn, collection, driver, question: fake_result,
        )
        chunks_by_item = {
            "item-1": [FakeChunk("chunk 1a"), FakeChunk("chunk 1b")],
            "item-2": [FakeChunk("chunk 2a")],
        }
        monkeypatch.setattr(
            "eval.run_evaluation.get_chunks_for_item",
            lambda conn, item_id: chunks_by_item[item_id],
        )

        target = _make_target(conn=object(), collection=object(), driver=object())
        result = target({"question": "What is X?"})

        assert result["answer"] == "The answer."
        assert result["retrieved_item_ids"] == ["item-1", "item-2"]
        assert result["context"] == ["chunk 1a\n\nchunk 1b", "chunk 2a"]


class FakeEvalProvider:
    def __init__(self, judgment):
        """Return ``judgment`` for every generate_eval_judgment() call."""
        self._judgment = judgment

    def generate_eval_judgment(self, criterion, question, answer, context):
        return self._judgment


class TestMakeEvaluators:
    def test_recall_evaluator_scores_against_expected_item_ids(self):
        recall_eval, _, _ = _make_evaluators(FakeEvalProvider(None))
        run = SimpleNamespace(outputs={"retrieved_item_ids": ["a", "b"]})
        example = SimpleNamespace(outputs={"expected_item_ids": ["a"]}, inputs={})

        result = recall_eval(run, example)

        assert result == {"key": "recall_at_k", "score": 1.0}

    def test_faithfulness_evaluator_uses_the_judge_provider(self):
        provider = FakeEvalProvider(EvalJudgment(score=0.6, reasoning="partial"))
        _, faithfulness_eval, _ = _make_evaluators(provider)
        run = SimpleNamespace(outputs={"answer": "a", "context": ["ctx"]})
        example = SimpleNamespace(inputs={"question": "q"}, outputs={})

        result = faithfulness_eval(run, example)

        assert result == {"key": "faithfulness", "score": 0.6, "comment": "partial"}

    def test_relevance_evaluator_uses_the_judge_provider(self):
        provider = FakeEvalProvider(EvalJudgment(score=0.9, reasoning="on topic"))
        _, _, relevance_eval = _make_evaluators(provider)
        run = SimpleNamespace(outputs={"answer": "a"})
        example = SimpleNamespace(inputs={"question": "q"}, outputs={})

        result = relevance_eval(run, example)

        assert result == {"key": "relevance", "score": 0.9, "comment": "on topic"}
