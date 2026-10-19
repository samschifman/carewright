"""Tests for recommendation escalation transport through the pods services."""

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from cpg_ingester import generation
from cpg_ingester.services import assembly_svc, bff, llm_analysis
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


def test_automation_counts_travel_from_generation_to_review_notification(monkeypatch):
    monkeypatch.setattr(llm_analysis, "_store", None)
    monkeypatch.setattr(generation, "generate_all", lambda _state: {
        "dmn_results": [],
        "recommendation_results": [],
        "bpmn_results": [
            {"escalated": False},
            {"escalated": True},
            {"escalated": False},
        ],
    })

    generated = llm_analysis._do_generate({
        "analysis_result": {"item_manifest": []},
    })
    assert generated["automation_counts"] == {
        "total": 3,
        "ready": 2,
        "escalated": 1,
    }

    with TestClient(bff.app) as client:
        response = client.post("/api/v1/notify-review", json={
            "review_type": "pre-delivery",
            "automation_counts": generated["automation_counts"],
        })

    assert response.status_code == 200
    assert response.json() == {
        "status": "acknowledged",
        "automation_counts": {"total": 3, "ready": 2, "escalated": 1},
    }


def test_automation_counts_survive_generate_result_storage(monkeypatch):
    class GenerateStore:
        generated_result = None

        def put(self, key, value):
            assert key.endswith("/generate_result.json")
            self.generated_result = value
            return f"cpg-artifacts:{key}"

    store = GenerateStore()
    monkeypatch.setattr(llm_analysis, "_store", store)
    monkeypatch.setattr(generation, "generate_all", lambda _state: {
        "dmn_results": [],
        "recommendation_results": [],
        "bpmn_results": [{"escalated": False}],
    })

    result = llm_analysis._do_generate({
        "analysis_result": {"item_manifest": []},
    })

    expected_counts = {"total": 1, "ready": 1, "escalated": 0}
    assert result["automation_counts"] == expected_counts
    assert store.generated_result["automation_counts"] == expected_counts


def test_workflow_carries_review_feedback_edits_and_template_counts():
    workflow_path = (
        Path(__file__).parents[1]
        / "deploy"
        / "orchestrator"
        / "cpg-ingester-workflow.yaml"
    )
    workflow = yaml.safe_load(workflow_path.read_text())
    states = workflow["spec"]["flow"]["states"]
    by_name = {state["name"]: state for state in states}

    generate_args = by_name["Generate"]["action"]["functionRef"]["arguments"]
    assert generate_args["review_feedback"] == "${ .artifactsReview.feedback }"
    assert generate_args["review_comment"] == "${ .artifactsReview.overallComment }"
    assert generate_args["prior_generate_result_ref"] == "${ .generateResult.generate_result_ref }"

    review_args = by_name["ReviewArtifacts"]["action"]["functionRef"]["arguments"]
    assert review_args["automation_counts"] == "${ .generateResult.automation_counts }"

    assemble_args = by_name["Assemble"]["actions"][0]["functionRef"]["arguments"]
    assert assemble_args["artifact_review"] == "${ .artifactsReview }"


def test_chart_leaves_bpmn_preflight_url_unset_by_default():
    values_path = (
        Path(__file__).parents[1]
        / "deploy"
        / "chart"
        / "values.yaml"
    )
    values = yaml.safe_load(values_path.read_text())
    assert values["pods"]["assembly"]["env"]["bpmnPreflightUrl"] == ""
    deployment_template = (
        values_path.parent / "templates" / "deployments.yaml"
    ).read_text()
    assert "name: BPMN_PREFLIGHT_URL" in deployment_template
