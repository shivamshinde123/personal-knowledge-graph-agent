"""Tests for the query router's rule-based heuristic."""

from agent.router import route


class TestKeywordSearchAlwaysRuns:
    def test_keyword_search_is_always_true(self):
        assert route("What did I work on related to RAG pipelines?").keyword_search
        assert route('Emails from "Jane Doe"').keyword_search
        assert route(
            "How does the storage layer connect to the pipeline?"
        ).keyword_search


class TestVectorSearchSkippedForSpecificLookups:
    def test_quoted_phrase_skips_vector_search(self):
        decision = route('Find notes mentioning "RAG pipeline design"')

        assert decision.vector_search is False

    def test_from_sender_lookup_skips_vector_search(self):
        decision = route("Show me emails from this recruiter")

        assert decision.vector_search is False

    def test_by_author_lookup_skips_vector_search(self):
        decision = route("Find commits by shivamshinde123")

        assert decision.vector_search is False

    def test_open_ended_question_runs_vector_search(self):
        decision = route(
            "What did I work on related to RAG pipelines in the last two months?"
        )

        assert decision.vector_search is True

    def test_thematic_question_runs_vector_search(self):
        decision = route("Summarize my notes on knowledge graphs")

        assert decision.vector_search is True


class TestGraphTraversalOnlyForRelationshipQuestions:
    def test_relationship_phrase_triggers_graph_traversal(self):
        decision = route("How does the storage layer relate to the pipeline design?")

        assert decision.graph_traversal is True

    def test_connection_phrase_triggers_graph_traversal(self):
        decision = route(
            "What's the connection between the PRD and the tech stack doc?"
        )

        assert decision.graph_traversal is True

    def test_causal_phrase_triggers_graph_traversal(self):
        decision = route("What led to the decision to use Neo4j?")

        assert decision.graph_traversal is True

    def test_plain_lookup_does_not_trigger_graph_traversal(self):
        decision = route("What is the embedding model configured?")

        assert decision.graph_traversal is False

    def test_specific_lookup_does_not_trigger_graph_traversal(self):
        decision = route("Show me emails from this recruiter")

        assert decision.graph_traversal is False


class TestCombinations:
    def test_relationship_question_can_still_be_a_specific_lookup(self):
        decision = route('How does "SWAP RAG notes" relate to the tech stack doc?')

        assert decision.vector_search is False
        assert decision.keyword_search is True
        assert decision.graph_traversal is True

    def test_broad_relationship_question_runs_everything(self):
        decision = route("How does the storage layer connect to the daily batch?")

        assert decision.vector_search is True
        assert decision.keyword_search is True
        assert decision.graph_traversal is True
