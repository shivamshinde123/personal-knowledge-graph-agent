"""Tests for agent/embedding_info.py.

A thin pass-through to the frozen embedding model constant — no network,
no mocking needed.
"""

from agent.embedding_info import get_embedding_model_name
from providers.openrouter_provider import EMBEDDING_MODEL


class TestGetEmbeddingModelName:
    def test_returns_the_frozen_model_name(self):
        assert get_embedding_model_name() == EMBEDDING_MODEL
