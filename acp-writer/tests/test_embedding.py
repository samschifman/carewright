"""Tests for embedding providers and the provider factory.

Covers the OpenAI-compatible (LiteLLM/MaaS) embedding provider and
``make_embedding_provider``, which selects a provider from the
``EMBEDDING_PROVIDER`` env switch.
"""

from unittest.mock import MagicMock, patch

import pytest

from acp_writer.store.embedding import (
    FakeEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    make_embedding_provider,
)


def _fake_response(vectors: list[list[float]]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "data": [
            {"index": i, "embedding": v} for i, v in enumerate(vectors)
        ],
        "model": "text-embedding-3-small",
    }
    return resp


class TestOpenAICompatibleEmbeddingProvider:
    def test_embed_posts_input_and_returns_vectors(self):
        provider = OpenAICompatibleEmbeddingProvider(
            model="text-embedding-3-small",
            base_url="https://api.openai.com",
            api_key="sk-test",
        )
        with patch(
            "acp_writer.store.embedding.requests.post",
            return_value=_fake_response([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]),
        ) as mock_post:
            vectors = provider.embed(["metformin", "lisinopril"])

        assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        # posted the texts as `input` with the configured model
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["input"] == ["metformin", "lisinopril"]
        assert kwargs["json"]["model"] == "text-embedding-3-small"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test"

    def test_embed_reorders_by_response_index(self):
        """The provider must honor the `index` field, not response order."""
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://api.openai.com", api_key="sk-test"
        )
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "data": [
                {"index": 1, "embedding": [9.0]},
                {"index": 0, "embedding": [1.0]},
            ]
        }
        with patch("acp_writer.store.embedding.requests.post", return_value=resp):
            vectors = provider.embed(["a", "b"])
        assert vectors == [[1.0], [9.0]]

    def test_embed_empty_list_makes_no_request(self):
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://api.openai.com", api_key="sk-test"
        )
        with patch("acp_writer.store.embedding.requests.post") as mock_post:
            assert provider.embed([]) == []
        mock_post.assert_not_called()

    def test_base_url_without_v1_gets_v1_embeddings(self):
        provider = OpenAICompatibleEmbeddingProvider(base_url="https://api.openai.com")
        assert provider.embeddings_url == "https://api.openai.com/v1/embeddings"

    def test_base_url_with_v1_gets_embeddings_only(self):
        provider = OpenAICompatibleEmbeddingProvider(base_url="http://litellm:4000/v1")
        assert provider.embeddings_url == "http://litellm:4000/v1/embeddings"

    def test_base_url_with_trailing_slash_is_normalized(self):
        provider = OpenAICompatibleEmbeddingProvider(base_url="https://api.openai.com/")
        assert provider.embeddings_url == "https://api.openai.com/v1/embeddings"

    def test_dimensions_inferred_from_embed_response(self):
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://api.openai.com", api_key="sk-test"
        )
        with patch(
            "acp_writer.store.embedding.requests.post",
            return_value=_fake_response([[0.0] * 1536]),
        ):
            dims = provider.dimensions
        assert dims == 1536


class TestMakeEmbeddingProvider:
    def test_defaults_to_fake_when_unset(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
        provider = make_embedding_provider()
        assert isinstance(provider, FakeEmbeddingProvider)

    def test_fake_explicit(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
        assert isinstance(make_embedding_provider(), FakeEmbeddingProvider)

    def test_openai_provider_selected(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("LITELLM_URL", "https://api.openai.com")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
        provider = make_embedding_provider()
        assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
        assert provider.embeddings_url == "https://api.openai.com/v1/embeddings"

    def test_selection_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "OpenAI")
        provider = make_embedding_provider()
        assert isinstance(provider, OpenAICompatibleEmbeddingProvider)

    def test_unknown_provider_falls_back_to_fake(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "bogus")
        assert isinstance(make_embedding_provider(), FakeEmbeddingProvider)

    def test_local_provider_explains_optional_dependency(self, monkeypatch):
        import sys

        from acp_writer.store.embedding import SentenceTransformerProvider

        monkeypatch.setitem(sys.modules, "sentence_transformers", None)
        with pytest.raises(RuntimeError, match="acp-writer\\[local-embeddings\\]"):
            SentenceTransformerProvider().embed(["blood pressure"])


class TestReasoningServiceStoreWiring:
    """Regression guard for the split llm_reasoning service's store binding.

    The service must ingest recommendations into the SAME vector store that the
    api module (and the RAG nodes, which read ``api._vector_store`` at call time)
    use. A module-level ``from acp_writer.api import _vector_store`` binds to the
    object present at import time, so a later ``init_stores()`` swap — e.g. the
    real-embedding-provider install — would route ingestion to a stale store
    while retrieval used the new one. This test swaps the store after import and
    asserts ingestion lands where retrieval looks.
    """

    def test_ingestion_lands_in_active_store_after_swap(self):
        import json
        from pathlib import Path

        from fastapi.testclient import TestClient

        from acp_writer import api
        from acp_writer.services.llm_reasoning import app as llm_app
        from acp_writer.store.embedding import FakeEmbeddingProvider

        fixtures = Path(__file__).parent.parent.parent / "shared" / "tests" / "fixtures"
        bundle = json.loads(
            (fixtures / "sample-recommendations.json").read_text()
        )["recommendation_bundle"]

        # Simulate the real-provider install: reassign api's stores AFTER the
        # reasoning service has already been imported.
        api.init_stores(FakeEmbeddingProvider(dimensions=8))

        client = TestClient(llm_app)
        r = client.post("/api/v1/knowledge/recommendations/batch", json=bundle)
        assert r.status_code == 201

        # The RAG nodes read api._vector_store at call time — ingestion must be
        # visible there, not stranded in a stale from-imported store.
        assert api._vector_store.count() == len(bundle["recommendations"])
