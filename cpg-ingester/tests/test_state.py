"""Tests for cpg-ingester state definitions."""

from cpg_ingester.state import CPGIngesterState, DMNPipelineState, RecPipelineState


def test_ingester_state_empty():
    state: CPGIngesterState = {}
    assert isinstance(state, dict)


def test_ingester_state_with_run_id():
    state: CPGIngesterState = {"run_id": "abc123", "pdf_path": "/tmp/test.pdf"}
    assert state["run_id"] == "abc123"


def test_ingester_state_with_llm_config():
    state: CPGIngesterState = {
        "litellm_url": "http://localhost:4000",
        "llm_model": "default",
    }
    assert state["llm_model"] == "default"
    assert "llm_api_key" not in CPGIngesterState.__annotations__


def test_dmn_pipeline_state():
    state: DMNPipelineState = {
        "item": {"id": "test"},
        "syntax_retry_count": 0,
        "semantic_retry_count": 0,
    }
    assert state["syntax_retry_count"] == 0
    assert state["semantic_retry_count"] == 0


def test_dmn_pipeline_state_with_llm_config():
    state: DMNPipelineState = {
        "item": {"id": "test"},
        "litellm_url": "http://localhost:4000",
        "llm_model": "default",
        "syntax_retry_count": 0,
    }
    assert state["litellm_url"] == "http://localhost:4000"
    assert "llm_api_key" not in DMNPipelineState.__annotations__


def test_rec_pipeline_state():
    state: RecPipelineState = {"items": [{"id": "r1"}], "review_count": 0}
    assert len(state["items"]) == 1
