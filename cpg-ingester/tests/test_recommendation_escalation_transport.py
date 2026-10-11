"""Tests for recommendation escalation transport through the pods services."""

from fastapi.testclient import TestClient

from cpg_ingester import generation
from cpg_ingester.services import assembly_svc, llm_analysis
from cpg_ingester.services.artifact_resolver import enrich_run_detail


ESCALATION = {
    "type": "recommendation",
    "id": "section-4",
    "name": "Section: Pharmacological Treatment",
    "section": "Pharmacological Treatment",
    "escalation_reason": "no-source-text",
    "escalation_errors": ["No CPG source text was available."],
}


def _generate_result() -> dict:
    return {
        "cpg_metadata": {"cpg_id": "CPG-1"},
        "item_manifest": [],
        "dmn_results": [],
        "recommendation_results": [
            {"id": "rec-1", "title": "Example", "source_cpg": "CPG-1"},
        ],
        "recommendation_escalations": [ESCALATION],
    }


def test_generate_service_includes_recommendation_escalations(monkeypatch):
    monkeypatch.setattr(llm_analysis, "_store", None)
    passed_state = {}

    def _generate(state):
        passed_state.update(state)
        return {
            "dmn_results": [],
            "recommendation_results": [],
            "recommendation_escalations": [ESCALATION],
            "bpmn_results": [{"item": {"name": "Home BP"}, "bpmn_xml": "<definitions/>"}],
        }

    monkeypatch.setattr(
        generation,
        "generate_all",
        _generate,
    )

    result = llm_analysis._do_generate({
        "analysis_result": {
            "item_manifest": [],
            "figures": [{"id": "fig-1", "interpretation": {"mermaid": "flowchart LR"}}],
        },
        "review_feedback": "Preserve the clinician's source-grounded follow-up interval.",
        "review_comment": "Do not infer a new interval.",
        "review_iteration": 1,
    })

    assert result["recommendation_escalations"] == [ESCALATION]
    assert result["bpmn_results"][0]["bpmn_xml"] == "<definitions/>"
    assert passed_state["figures"][0]["id"] == "fig-1"
    assert passed_state["review_feedback"] == "Preserve the clinician's source-grounded follow-up interval."
    assert passed_state["review_comment"] == "Do not infer a new interval."


def test_assembly_service_carries_section_escalation_from_generate_result(monkeypatch):
    class GenerateResultStore:
        def get(self, ref: str) -> dict:
            assert ref == "cpg-uploads:run-1/generate_result.json"
            return _generate_result()

    monkeypatch.setattr(assembly_svc, "_store", GenerateResultStore())
    monkeypatch.setattr(
        assembly_svc,
        "store_artifact",
        lambda store, key, value: (value, None),
    )

    with TestClient(assembly_svc.app) as client:
        response = client.post(
            "/api/v1/assemble",
            json={"generate_result_ref": "cpg-uploads:run-1/generate_result.json"},
        )

    assert response.status_code == 200
    assert response.json()["escalated_items"] == [ESCALATION]


def test_assembly_service_carries_section_escalation_from_legacy_payload(monkeypatch):
    monkeypatch.setattr(assembly_svc, "_store", None)
    monkeypatch.setattr(
        assembly_svc,
        "store_artifact",
        lambda store, key, value: (value, None),
    )

    with TestClient(assembly_svc.app) as client:
        response = client.post(
            "/api/v1/assemble",
            json=_generate_result(),
        )

    assert response.status_code == 200
    assert response.json()["escalated_items"] == [ESCALATION]


def test_artifact_resolver_exposes_recommendation_escalations_from_generate_ref():
    class GenerateResultStore:
        def get(self, ref: str) -> dict:
            assert ref == "cpg-uploads:run-1/generate_result.json"
            return {"recommendation_escalations": [ESCALATION]}

    detail = {
        "workflowData": {
            "generateResult": {
                "generate_result_ref": "cpg-uploads:run-1/generate_result.json",
            },
        },
    }

    result = enrich_run_detail(detail, GenerateResultStore())

    assert result["recommendationEscalations"] == [ESCALATION]
    assert "workflowData" not in result
