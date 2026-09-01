"""Tests for the shared provider adapter and factory.

No real LLM calls: LangChainProvider is exercised against a fake chat model
double, since it only needs an object with an ``.invoke()`` method — the
same reason live Ollama/OpenRouter calls aren't needed to test prompt
construction, response parsing, or retry behavior.
"""

from types import SimpleNamespace

import pytest

from providers.base import (
    ContextChunk,
    ConversationTurn,
    LangChainProvider,
    ProviderError,
    get_provider,
)


class TestGenerateEvalJudgment:
    def test_parses_score_and_reasoning(self):
        chat_model = FakeChatModel(
            ['{"score": 0.9, "reasoning": "Well grounded in the context."}']
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        judgment = provider.generate_eval_judgment(
            "faithfulness", "q", "a", ["context chunk"]
        )

        assert judgment.score == 0.9
        assert judgment.reasoning == "Well grounded in the context."

    def test_a_reasoning_preamble_before_the_json_is_still_parsed(self):
        chat_model = FakeChatModel(
            [
                "Looking at the answer against the context, every claim "
                'checks out.\n\n{"score": 1.0, "reasoning": "Fully supported."}'
            ]
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        judgment = provider.generate_eval_judgment(
            "faithfulness", "q", "a", ["context"]
        )

        assert judgment.score == 1.0

    def test_a_score_outside_0_to_1_raises_provider_error(self):
        chat_model = FakeChatModel(['{"score": 1.5, "reasoning": "x"}'] * 4)
        provider = LangChainProvider(chat_model, provider_name="test")

        with pytest.raises(ProviderError):
            provider.generate_eval_judgment("relevance", "q", "a", [])

    def test_missing_score_raises_provider_error(self):
        chat_model = FakeChatModel(['{"reasoning": "no score given"}'] * 4)
        provider = LangChainProvider(chat_model, provider_name="test")

        with pytest.raises(ProviderError):
            provider.generate_eval_judgment("relevance", "q", "a", [])

    def test_missing_reasoning_defaults_to_empty_string(self):
        chat_model = FakeChatModel(['{"score": 0.5}'])
        provider = LangChainProvider(chat_model, provider_name="test")

        judgment = provider.generate_eval_judgment("relevance", "q", "a", [])

        assert judgment.reasoning == ""

    def test_faithfulness_prompt_includes_context_not_a_missing_placeholder(self):
        chat_model = FakeChatModel(['{"score": 0.8, "reasoning": "ok"}'])
        provider = LangChainProvider(chat_model, provider_name="test")

        provider.generate_eval_judgment(
            "faithfulness", "What is X?", "X is Y.", ["X is defined as Y in doc Z."]
        )

        prompt = chat_model.prompts[0]
        assert "What is X?" in prompt
        assert "X is Y." in prompt
        assert "X is defined as Y in doc Z." in prompt

    def test_empty_context_is_shown_as_such_not_left_blank(self):
        chat_model = FakeChatModel(['{"score": 0.0, "reasoning": "no context"}'])
        provider = LangChainProvider(chat_model, provider_name="test")

        provider.generate_eval_judgment("relevance", "q", "a", [])

        assert "no context retrieved" in chat_model.prompts[0]


class FakeChatModel:
    """Queues canned responses (or exceptions) for successive .invoke() calls."""

    def __init__(self, responses):
        """Queue ``responses`` (strings or exceptions) for successive calls."""
        self._responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(content=value)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("providers.base.time.sleep", lambda _seconds: None)


class TestGenerateMetadata:
    def test_parses_a_batch_of_results_in_order(self):
        chat_model = FakeChatModel(
            [
                '[{"project_name": "pkg-agent", "topic": "storage"}, '
                '{"project_name": null, "topic": null}]'
            ]
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        results = provider.generate_metadata(["text one", "text two"])

        assert results[0].project_name == "pkg-agent"
        assert results[0].topic == "storage"
        assert results[1].project_name is None
        assert results[1].topic is None

    def test_empty_input_returns_empty_output_without_calling_the_model(self):
        chat_model = FakeChatModel([])
        provider = LangChainProvider(chat_model, provider_name="test")

        assert provider.generate_metadata([]) == []
        assert chat_model.prompts == []

    def test_mismatched_result_count_raises_provider_error(self):
        chat_model = FakeChatModel(
            ['[{"project_name": null, "topic": null}]'] * 4  # always 1, never 2
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        with pytest.raises(ProviderError):
            provider.generate_metadata(["text one", "text two"])


class TestGenerateRelationship:
    def test_returns_none_when_not_related(self):
        chat_model = FakeChatModel(['{"related": false}'])
        provider = LangChainProvider(chat_model, provider_name="test")

        assert provider.generate_relationship("a", "b") is None

    def test_returns_judgment_when_related(self):
        chat_model = FakeChatModel(
            ['{"related": true, "label": "implements", "confidence": 0.8}']
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        judgment = provider.generate_relationship("a", "b")

        assert judgment.label == "implements"
        assert judgment.confidence == 0.8

    def test_related_without_confidence_leaves_it_none(self):
        chat_model = FakeChatModel(['{"related": true, "label": "discussed_in"}'])
        provider = LangChainProvider(chat_model, provider_name="test")

        judgment = provider.generate_relationship("a", "b")

        assert judgment.confidence is None

    def test_related_without_a_label_raises_provider_error(self):
        chat_model = FakeChatModel(['{"related": true}'] * 4)
        provider = LangChainProvider(chat_model, provider_name="test")

        with pytest.raises(ProviderError):
            provider.generate_relationship("a", "b")

    def test_a_label_outside_the_fixed_vocabulary_raises_provider_error(self):
        chat_model = FakeChatModel(
            ['{"related": true, "label": "loosely_reminds_me_of"}'] * 4
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        with pytest.raises(ProviderError):
            provider.generate_relationship("a", "b")

    def test_prompt_lists_the_fixed_label_vocabulary(self):
        chat_model = FakeChatModel(['{"related": false}'])
        provider = LangChainProvider(chat_model, provider_name="test")

        provider.generate_relationship("a", "b")

        prompt = chat_model.prompts[0]
        for label in ("implements", "discussed_in", "planned_in", "companion_to"):
            assert label in prompt

    def test_a_reasoning_preamble_before_the_json_is_still_parsed(self):
        """Regression coverage for a real false positive on claude-sonnet-4.

        Verified against the real model: asking it to first name any
        shared boilerplate before judging (see DECISIONS.md, 2026-08-29)
        reliably makes it explain its reasoning ahead of the JSON, despite
        the prompt's "ONLY JSON" instruction — so parsing must tolerate a
        preamble, not just a bare JSON object.
        """
        chat_model = FakeChatModel(
            [
                "Looking at both texts, I can identify shared boilerplate: "
                '"Companion document to: X". Setting that aside, the '
                "content is unrelated.\n\n"
                '{"related": false}'
            ]
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        assert provider.generate_relationship("a", "b") is None

    def test_a_reasoning_preamble_before_a_confirmed_judgment_is_parsed(self):
        chat_model = FakeChatModel(
            [
                "The shared boilerplate line names Text 2 specifically, so "
                "this is a genuine companion pairing.\n\n"
                '{"related": true, "label": "companion_to", "confidence": 0.9}'
            ]
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        judgment = provider.generate_relationship("a", "b")

        assert judgment.label == "companion_to"
        assert judgment.confidence == 0.9

    def test_prompt_instructs_setting_aside_shared_boilerplate(self):
        chat_model = FakeChatModel(['{"related": false}'])
        provider = LangChainProvider(chat_model, provider_name="test")

        provider.generate_relationship("a", "b")

        assert "boilerplate" in chat_model.prompts[0]


class TestGenerateAnswer:
    def test_returns_the_raw_response_text(self):
        chat_model = FakeChatModel(["Vector search runs first [1]."])
        provider = LangChainProvider(chat_model, provider_name="test")
        context = [ContextChunk(text="...", source_type="notion", title="Design")]

        answer = provider.generate_answer("How does search work?", context)

        assert answer == "Vector search runs first [1]."

    def test_prompt_includes_question_and_numbered_context(self):
        chat_model = FakeChatModel(["answer"])
        provider = LangChainProvider(chat_model, provider_name="test")
        context = [ContextChunk(text="chunk text", source_type="gmail")]

        provider.generate_answer("What happened?", context)

        prompt = chat_model.prompts[0]
        assert "What happened?" in prompt
        assert "[1]" in prompt
        assert "chunk text" in prompt

    def test_no_history_omits_the_conversation_section(self):
        chat_model = FakeChatModel(["answer"])
        provider = LangChainProvider(chat_model, provider_name="test")

        provider.generate_answer("q", [])

        assert "Prior conversation" not in chat_model.prompts[0]

    def test_history_is_included_in_the_prompt(self):
        chat_model = FakeChatModel(["answer"])
        provider = LangChainProvider(chat_model, provider_name="test")
        history = [
            ConversationTurn(role="user", text="What did I work on?"),
            ConversationTurn(role="agent", text="You worked on X and Y [1]."),
        ]

        provider.generate_answer("Tell me more about the second one", [], history)

        prompt = chat_model.prompts[0]
        assert "Prior conversation" in prompt
        assert "What did I work on?" in prompt
        assert "You worked on X and Y [1]." in prompt
        assert "Tell me more about the second one" in prompt


class TestGenerateSearchQuery:
    def test_returns_the_stripped_response_text(self):
        chat_model = FakeChatModel(["  What database did the storage layer choose?  "])
        provider = LangChainProvider(chat_model, provider_name="test")
        history = [
            ConversationTurn(role="user", text="What database options were weighed?"),
            ConversationTurn(role="agent", text="SQLite, Chroma, and Neo4j [1]."),
        ]

        query = provider.generate_search_query(
            "Why was the second one chosen?", history
        )

        assert query == "What database did the storage layer choose?"

    def test_prompt_includes_history_and_the_follow_up(self):
        chat_model = FakeChatModel(["rewritten query"])
        provider = LangChainProvider(chat_model, provider_name="test")
        history = [
            ConversationTurn(role="user", text="What did I work on yesterday?"),
            ConversationTurn(role="agent", text="You worked on X and Y [1]."),
        ]

        provider.generate_search_query("Tell me more about the second one", history)

        prompt = chat_model.prompts[0]
        assert "What did I work on yesterday?" in prompt
        assert "You worked on X and Y [1]." in prompt
        assert "Tell me more about the second one" in prompt

    def test_an_empty_response_raises_provider_error(self):
        chat_model = FakeChatModel(["   "] * 4)
        provider = LangChainProvider(chat_model, provider_name="test")

        with pytest.raises(ProviderError):
            provider.generate_search_query(
                "q", [ConversationTurn(role="user", text="x")]
            )


class TestRetryBehavior:
    def test_retries_transient_failures_and_succeeds(self):
        chat_model = FakeChatModel(
            [ConnectionError("boom"), ConnectionError("boom"), "final answer"]
        )
        provider = LangChainProvider(chat_model, provider_name="test")

        assert provider.generate_answer("q", []) == "final answer"
        assert len(chat_model.prompts) == 3

    def test_raises_provider_error_after_exhausting_retries(self):
        chat_model = FakeChatModel([ConnectionError("boom")] * 10)
        provider = LangChainProvider(chat_model, provider_name="my-provider")

        with pytest.raises(ProviderError, match="my-provider"):
            provider.generate_answer("q", [])


class TestGetProvider:
    def test_fully_local_always_returns_local_provider(self, monkeypatch):
        monkeypatch.setattr(
            "providers.base.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(llm=SimpleNamespace(provider_mode="fully_local"))
            ),
        )
        monkeypatch.setattr(
            "providers.local_provider.create_local_provider", lambda: "LOCAL"
        )
        monkeypatch.setattr(
            "providers.openrouter_provider.create_openrouter_provider",
            lambda: "CLOUD",
        )

        assert get_provider("answer") == "LOCAL"
        assert get_provider("metadata") == "LOCAL"

    def test_fully_cloud_always_returns_cloud_provider(self, monkeypatch):
        monkeypatch.setattr(
            "providers.base.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(llm=SimpleNamespace(provider_mode="fully_cloud"))
            ),
        )
        monkeypatch.setattr(
            "providers.local_provider.create_local_provider", lambda: "LOCAL"
        )
        monkeypatch.setattr(
            "providers.openrouter_provider.create_openrouter_provider",
            lambda: "CLOUD",
        )

        assert get_provider("answer") == "CLOUD"
        assert get_provider("relationship") == "CLOUD"

    def test_embedding_routes_to_local_under_fully_local(self, monkeypatch):
        """Embedding follows provider_mode like every other task.

        Local embedding was brought back via Ollama — see
        providers/base.py::get_provider()'s own docstring, DECISIONS.md.
        """
        monkeypatch.setattr(
            "providers.base.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(llm=SimpleNamespace(provider_mode="fully_local"))
            ),
        )
        monkeypatch.setattr(
            "providers.local_provider.create_local_provider", lambda: "LOCAL"
        )
        monkeypatch.setattr(
            "providers.openrouter_provider.create_openrouter_provider",
            lambda: "CLOUD",
        )

        assert get_provider("embedding") == "LOCAL"

    def test_embedding_routes_to_cloud_under_fully_cloud(self, monkeypatch):
        monkeypatch.setattr(
            "providers.base.get_settings",
            lambda: SimpleNamespace(
                config=SimpleNamespace(llm=SimpleNamespace(provider_mode="fully_cloud"))
            ),
        )
        monkeypatch.setattr(
            "providers.local_provider.create_local_provider", lambda: "LOCAL"
        )
        monkeypatch.setattr(
            "providers.openrouter_provider.create_openrouter_provider",
            lambda: "CLOUD",
        )

        assert get_provider("embedding") == "CLOUD"
