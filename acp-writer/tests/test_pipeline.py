"""Tests for the LangGraph care plan pipeline."""

import logging
from unittest.mock import patch, MagicMock

from langgraph.checkpoint.memory import MemorySaver

from acp_writer.pipeline import _route_after_plan_composer

from .pipeline_helpers import build_pipeline


def _mock_llm_invoke(messages):
    """Return a minimal valid response for any LLM call."""
    mock_response = MagicMock()
    mock_response.content = '{"verdict": "APPROVE", "issues": []}'
    return mock_response


@patch("acp_writer.nodes.plan_composer.get_llm")
@patch("acp_writer.nodes.brief_reviewer.get_llm")
@patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
def _run_pipeline(mock_fhir_llm, mock_brief_llm, mock_compose_llm, ips_bundle=None, checkpointer=None, config=None):
    """Run the pipeline with mocked LLM nodes for determinism."""
    import json

    compose_response = MagicMock()
    compose_response.content = json.dumps({
        "patient_reference": "Patient/unknown",
        "applicable_cpgs": [],
        "dmn_audit_trail": [],
        "goals": [],
        "activities": [],
        "conflicts": [],
        "review_status": "pending",
    })
    mock_compose = MagicMock()
    mock_compose.invoke.return_value = compose_response
    mock_compose_llm.return_value = mock_compose

    for mock_llm in [mock_brief_llm, mock_fhir_llm]:
        approve_response = MagicMock()
        approve_response.content = '{"verdict": "APPROVE", "issues": []}'
        m = MagicMock()
        m.invoke.return_value = approve_response
        mock_llm.return_value = m

    graph = build_pipeline()
    compile_kwargs = {}
    if checkpointer:
        compile_kwargs["checkpointer"] = checkpointer
    compiled = graph.compile(**compile_kwargs)

    invoke_kwargs = {}
    if config:
        invoke_kwargs["config"] = config
    return compiled, compiled.invoke({"ips_bundle": ips_bundle or {}}, **invoke_kwargs)


class TestPipelineStructure:
    def test_graph_compiles(self):
        graph = build_pipeline()
        compiled = graph.compile()
        assert compiled is not None

    def test_expected_nodes(self):
        graph = build_pipeline()
        node_names = set(graph.nodes.keys())
        expected = {
            "condition_scanner",
            "guideline_resolver",
            "dmn_executor",
            "recommendation_retriever",
            "plan_composer",
            "brief_reviewer",
            "automation_synthesizer",
            "conflict_analyst",
            "fhir_bundle_generator",
            "terminology_validator",
            "fhir_syntax_validator",
            "fhir_semantic_reviewer",
            "fhir_server_writer",
        }
        assert expected == node_names

    def test_thirteen_nodes(self):
        graph = build_pipeline()
        assert len(graph.nodes) == 13

    def test_conflict_analyst_wiring(self):
        """brief_reviewer → automation_synthesizer → conflict_analyst → fhir_bundle_generator; the FHIR
        loop must not pass back through the analyst."""
        graph = build_pipeline()
        edges = set(graph.edges)
        assert ("automation_synthesizer", "conflict_analyst") in edges
        assert ("conflict_analyst", "fhir_bundle_generator") in edges
        # The FHIR review loop returns to the generator, never the analyst.
        assert ("fhir_semantic_reviewer", "conflict_analyst") not in edges

    def test_malformed_brief_routes_to_composer_without_reviewer(self):
        assert _route_after_plan_composer({
            "brief_parse_failed": True,
            "brief_review_count": 1,
        }) == "plan_composer"
        assert _route_after_plan_composer({
            "brief_parse_failed": True,
            "brief_review_count": 4,
        }) == "__end__"
        assert _route_after_plan_composer({"brief_parse_failed": False}) == "brief_reviewer"


class TestPipelineExecution:
    def test_pipeline_completes(self):
        _, result = _run_pipeline()
        assert "delivery_status" in result
        assert result["fhir_bundle"]["resourceType"] == "Bundle"
        assert result["fhir_bundle"]["type"] == "transaction"

    def test_all_nodes_execute(self, caplog):
        with caplog.at_level(logging.INFO):
            _run_pipeline()

        logged = caplog.text
        for node in [
            "condition_scanner",
            "guideline_resolver",
            "dmn_executor",
            "recommendation_retriever",
            "plan_composer",
            "brief_reviewer",
            "automation_synthesizer",
            "fhir_bundle_generator",
            "terminology_validator",
            "fhir_syntax_validator",
            "fhir_semantic_reviewer",
            "fhir_server_writer",
        ]:
            assert node in logged, f"Node {node} did not execute"

    def test_memorysaver_checkpoint(self):
        config = {"configurable": {"thread_id": "test-run-001"}}
        checkpointer = MemorySaver()
        compiled, result = _run_pipeline(checkpointer=checkpointer, config=config)

        assert "delivery_status" in result

        state = compiled.get_state(config)
        assert state.values["delivery_status"] == result["delivery_status"]
