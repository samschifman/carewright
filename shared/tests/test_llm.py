from unittest.mock import patch

from cpg_contracts.llm import get_llm


def test_get_llm_prefers_environment_key_over_pipeline_state(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("LITELLM_API_KEY", "proxy-environment-key")

    with patch("cpg_contracts.llm.ChatOpenAI") as chat_openai:
        get_llm({"llm_api_key": "state-key", "litellm_url": "https://api.openai.com"})

    assert chat_openai.call_args.kwargs["api_key"] == "environment-key"


def test_get_llm_uses_litellm_environment_key_before_state(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "proxy-environment-key")

    with patch("cpg_contracts.llm.ChatOpenAI") as chat_openai:
        get_llm({"llm_api_key": "state-key"})

    assert chat_openai.call_args.kwargs["api_key"] == "proxy-environment-key"


def test_get_llm_never_reads_credentials_from_pipeline_state(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with patch("cpg_contracts.llm.ChatOpenAI") as chat_openai:
        get_llm({"llm_api_key": "state-key"})

    assert chat_openai.call_args.kwargs["api_key"] == "sk-change-me"
