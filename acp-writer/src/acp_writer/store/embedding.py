"""Pluggable embedding provider interface.

Organizations can substitute any model due to domain preferences,
legal restrictions, or compliance requirements. Default uses
NeuML/pubmedbert-base-embeddings (local, clinical-domain).
"""

import logging
import os
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"


class EmbeddingProvider(ABC):
    """Interface for producing vector embeddings from text."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts into vectors."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Dimensionality of the embedding vectors."""


class SentenceTransformerProvider(EmbeddingProvider):
    """Embedding provider using sentence-transformers models.

    Default model: NeuML/pubmedbert-base-embeddings (768 dims, clinical-domain).
    Override via EMBEDDING_MODEL env var or constructor argument.
    """

    def __init__(self, model_name: str | None = None):
        self._model_name = (
            model_name
            or os.environ.get("EMBEDDING_MODEL", "NeuML/pubmedbert-base-embeddings")
        )
        self._model = None
        self._dims: int | None = None

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "The local sentence-transformers provider requires "
                    "the 'local-embeddings' extra: pip install 'acp-writer[local-embeddings]'"
                ) from exc
            logger.info("Loading embedding model: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
            self._dims = self._model.get_sentence_embedding_dimension()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._load_model()
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return [e.tolist() for e in embeddings]

    @property
    def dimensions(self) -> int:
        self._load_model()
        return self._dims


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """Embedding provider backed by an OpenAI-compatible ``/embeddings`` endpoint.

    Works against OpenAI directly or any OpenAI-compatible gateway (LiteLLM,
    MaaS). Reuses the same base URL and API key the service already uses for
    chat completions (``LITELLM_URL`` / ``LLM_API_KEY``), so no extra
    credentials or heavyweight model downloads are required.

    Config (constructor args override env):
    - ``EMBEDDING_MODEL`` (default ``text-embedding-3-small``)
    - ``EMBEDDING_BASE_URL`` else ``LITELLM_URL``
    - ``EMBEDDING_API_KEY`` else ``LLM_API_KEY``
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        self._model = (
            model
            or os.environ.get("EMBEDDING_MODEL")
            or DEFAULT_OPENAI_EMBEDDING_MODEL
        )
        base = (
            base_url
            or os.environ.get("EMBEDDING_BASE_URL")
            or os.environ.get("LITELLM_URL")
            or "http://localhost:4000"
        )
        self.embeddings_url = self._build_embeddings_url(base)
        self._api_key = (
            api_key
            or os.environ.get("EMBEDDING_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or ""
        )
        self._timeout = timeout
        self._dims: int | None = None

    @staticmethod
    def _build_embeddings_url(base: str) -> str:
        base = base.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        if base.endswith("/v1"):
            return f"{base}/embeddings"
        return f"{base}/v1/embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = requests.post(
            self.embeddings_url,
            json={"model": self._model, "input": texts},
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        # The API may return items out of order; honor the `index` field.
        data = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [d["embedding"] for d in data]
        if vectors:
            self._dims = len(vectors[0])
        return vectors

    @property
    def dimensions(self) -> int:
        if self._dims is None:
            # Probe with a trivial input to learn the vector width.
            self.embed([" "])
        return self._dims


class FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic fake provider for testing. Produces fixed-dimension vectors."""

    def __init__(self, dimensions: int = 8):
        self._dims = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            h = hash(text) & 0xFFFFFFFF
            vec = []
            for i in range(self._dims):
                val = ((h * (i + 1) * 2654435761) & 0xFFFFFFFF) / 0xFFFFFFFF
                vec.append(val * 2 - 1)
            norm = sum(v * v for v in vec) ** 0.5
            if norm > 0:
                vec = [v / norm for v in vec]
            result.append(vec)
        return result

    @property
    def dimensions(self) -> int:
        return self._dims


def make_embedding_provider() -> EmbeddingProvider:
    """Select an embedding provider from the ``EMBEDDING_PROVIDER`` env switch.

    - ``openai`` — OpenAI-compatible endpoint (LiteLLM/MaaS/OpenAI). No heavy
      deps; reuses ``LITELLM_URL`` + ``LLM_API_KEY``.
    - ``sentence-transformers`` — local model (heavy; downloads torch + weights).
    - unset / ``fake`` / anything else — ``FakeEmbeddingProvider`` (test-safe
      default: no network or downloads on import).
    """
    provider = os.environ.get("EMBEDDING_PROVIDER", "").strip().lower()
    if provider == "openai":
        logger.info("Using OpenAI-compatible embedding provider")
        return OpenAICompatibleEmbeddingProvider()
    if provider in ("sentence-transformers", "sentence_transformers", "st"):
        logger.info("Using SentenceTransformer embedding provider")
        return SentenceTransformerProvider()
    if provider and provider != "fake":
        logger.warning(
            "Unknown EMBEDDING_PROVIDER=%r — falling back to FakeEmbeddingProvider",
            provider,
        )
    return FakeEmbeddingProvider(dimensions=8)
