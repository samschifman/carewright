"""Environment-backed LLM client factory for acp-writer."""

import os
from collections.abc import Mapping
from typing import Any

from langchain_openai import ChatOpenAI


def _api_key_from_environment() -> str:
    """Resolve the credential at construction, preferring project configuration.

    Helm provides LLM_API_KEY and compose provides LITELLM_API_KEY. OPENAI_API_KEY
    is a fallback because developer machines may set it for unrelated tools.
    """
    for name in ("LLM_API_KEY", "LITELLM_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    return "sk-change-me"


def get_llm(
    state: Mapping[str, Any] | None = None,
    *,
    base_url: str | None = None,
    model: str | None = None,
    max_retries: int | None = None,
    request_timeout: int | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI client without accepting credentials through state."""
    state = state or {}
    origin = (
        base_url
        or state.get("litellm_url")
        or os.environ.get("LITELLM_URL", "http://localhost:4000")
    )
    if base_url is None:
        origin = f"{origin.rstrip('/')}/v1"

    return ChatOpenAI(
        base_url=origin,
        api_key=_api_key_from_environment(),
        model=model or state.get("llm_model") or os.environ.get("LLM_MODEL", "default"),
        use_responses_api=True,
        max_retries=max_retries if max_retries is not None else int(os.environ.get("LLM_MAX_RETRIES", "5")),
        request_timeout=request_timeout if request_timeout is not None else int(os.environ.get("LLM_REQUEST_TIMEOUT", "600")),
    )
