"""Tests for agent/embedding_info.py.

A thin pass-through to the frozen embedding model constants — no network,
no mocking needed.
"""

from agent.embedding_info import get_embedding_model_names
from providers.local_provider import EMBEDDING_MODEL as LOCAL_EMBEDDING_MODEL
from providers.openrouter_provider import EMBEDDING_MODEL as CLOUD_EMBEDDING_MODEL


class TestGetEmbeddingModelNames:
    def test_returns_the_frozen_local_and_cloud_model_names(self):
        local, cloud = get_embedding_model_names()

        assert local == LOCAL_EMBEDDING_MODEL
        assert cloud == CLOUD_EMBEDDING_MODEL
