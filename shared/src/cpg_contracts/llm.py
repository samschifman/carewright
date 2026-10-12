"""Shared LLM client factory with retry and timeout configuration."""

import os

try:
    from langchain_openai import ChatOpenAI
except ImportError:  # The LLM integration is an optional extra for shared contracts.
    ChatOpenAI = None  # type: ignore[assignment,misc]

LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "5"))
LLM_REQUEST_TIMEOUT = int(os.environ.get("LLM_REQUEST_TIMEOUT", "600"))


def get_llm(state: dict) -> ChatOpenAI:
    """Build an LLM client from non-secret pipeline settings.

    state['litellm_url'] must be the bare origin (e.g. 'http://localhost:4000')
    — this function appends '/v1'. Do not include '/v1' in the config value.
    Credentials come from the environment only: prefer LLM_API_KEY (Helm), then
    LITELLM_API_KEY (compose), then OPENAI_API_KEY, which developers may set for
    unrelated tools. Never pass credentials through pipeline state.
    """
    if ChatOpenAI is None:
        raise RuntimeError("LLM support requires the cpg-contracts 'llm' extra")
    # Credentials come only from the process environment. Pipeline state is
    # captured by tracing and checkpointing, so secrets must never enter it.
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("LITELLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "sk-change-me"
    )
    return ChatOpenAI(
        base_url=f"{state.get('litellm_url', 'http://localhost:4000')}/v1",
        api_key=api_key,
        model=state.get("llm_model", "default"),
        use_responses_api=True,
        max_retries=LLM_MAX_RETRIES,
        request_timeout=LLM_REQUEST_TIMEOUT,
    )


def content_to_text(content) -> str:
    """Normalize an AIMessage.content value to plain text.

    LangChain's message content is `str | list` by contract. With
    use_responses_api=True (required for gpt-5.6 tool calling), reasoning
    models return a list of content blocks — e.g. a reasoning block followed
    by a text block — instead of a bare string. Code that assumes str (e.g.
    `response.content.strip()`) crashes with
    "'list' object has no attribute 'strip'".

    Joins the text of all text-bearing blocks; skips reasoning/tool blocks.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") in ("text", "output_text") or (
                "type" not in block and "text" in block
            ):
                parts.append(block.get("text", ""))
    return "".join(parts)
