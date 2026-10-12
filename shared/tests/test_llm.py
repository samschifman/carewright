from unittest.mock import patch

from cpg_contracts.llm import get_llm


def test_get_llm_prefers_project_environment_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "project-test-value")
    monkeypatch.setenv("LITELLM_API_KEY", "compose-test-value")
    monkeypatch.setenv("OPENAI_API_KEY", "generic-test-value")

    with patch("cpg_contracts.llm.ChatOpenAI") as chat_openai:
        get_llm({"llm_api_key": "state-key", "litellm_url": "https://api.openai.com"})

    if chat_openai.call_args.kwargs["api_key"] != "project-test-value":
        raise AssertionError("factory did not prefer the project environment key")


def test_get_llm_uses_litellm_environment_key_before_state(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "compose-test-value")

    with patch("cpg_contracts.llm.ChatOpenAI") as chat_openai:
        get_llm({"llm_api_key": "state-key"})

    if chat_openai.call_args.kwargs["api_key"] != "compose-test-value":
        raise AssertionError("factory did not use the compose environment key")


def test_get_llm_uses_openai_environment_key_as_fallback(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "generic-test-value")

    with patch("cpg_contracts.llm.ChatOpenAI") as chat_openai:
        get_llm({})

    if chat_openai.call_args.kwargs["api_key"] != "generic-test-value":
        raise AssertionError("factory did not use the generic environment fallback")


def test_get_llm_never_reads_credentials_from_pipeline_state(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    with patch("cpg_contracts.llm.ChatOpenAI") as chat_openai:
        get_llm({"llm_api_key": "state-key"})

    assert chat_openai.call_args.kwargs["api_key"] == "sk-change-me"
