"""Security tests for the shared environment-backed LLM client factory."""

import json

from cpg_contracts import get_llm
from cpg_ingester.state import CPGIngesterState


def test_llm_key_stays_out_of_state_and_client_serialization(monkeypatch):
    project_value = "sk-test-project"
    generic_value = "sk-test-generic"

    state: CPGIngesterState = {
        "litellm_url": "http://localhost:4000",
        "llm_model": "default",
    }

    def assert_used_without_serializing(value):
        client = get_llm(state)
        client_key = client.openai_api_key
        if (
            not hasattr(client_key, "get_secret_value")
            or client_key.get_secret_value() != value
        ):
            raise AssertionError(
                "LLM client factory did not use the expected environment credential"
            )

        serialized = [
            json.dumps(state),
            json.dumps(client.model_dump(), default=str),
            json.dumps(client.dict(), default=str),
        ]
        if any(value in dumped for dumped in serialized):
            raise AssertionError("LLM credential appeared in serialized state or client data")

    monkeypatch.setenv("LLM_API_KEY", project_value)
    monkeypatch.setenv("OPENAI_API_KEY", generic_value)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    assert_used_without_serializing(project_value)

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert_used_without_serializing(generic_value)
