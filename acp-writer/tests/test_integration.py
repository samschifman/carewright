"""Integration tests for the acp-writer API."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acp_writer.api import app, _dynamic_models, _vector_store, _guidelines_store

PROJECT_ROOT = Path(__file__).parent.parent.parent

client = TestClient(app)


def load_bundle(name: str) -> dict:
    path = PROJECT_ROOT / "mock-EHR" / "data" / name
    return json.loads(path.read_text())


def load_dmn(name: str) -> str:
    path = Path(__file__).parent / "fixtures" / "dmn" / name
    return path.read_text()


def deploy_models():
    """Deploy the standard hypertension DMN models for tests that need them."""
    for dmn_file in ["treatment-recommendation.dmn", "monitoring-plan.dmn"]:
        dmn_xml = load_dmn(dmn_file)
        r = client.post(
            "/api/v1/decisions/models",
            content=dmn_xml,
            headers={"Content-Type": "application/xml"},
        )
        assert r.status_code == 201


def find_resources(bundle: dict, resource_type: str) -> list[dict]:
    return [
        e["resource"]
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == resource_type
    ]


@pytest.fixture(autouse=True)
def clear_models(monkeypatch):
    """Clear dynamic models between tests."""
    # Deployment validation is covered by test_dmn_validation.py. Keep the
    # broad integration suite independent of a running decision-service.
    monkeypatch.setattr("acp_writer.api._validate_dmn_with_engine", lambda _: None)
    _dynamic_models.clear()
    yield
    _dynamic_models.clear()


class TestHealth:
    def test_liveness(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "UP"

    def test_status(self):
        r = client.get("/api/v1/status")
        assert r.status_code == 200
        data = r.json()
        assert "decision_engine" in data
        assert "knowledge_base" in data


class TestDecisionModels:
    def test_list_empty(self):
        r = client.get("/api/v1/decisions/models")
        assert r.status_code == 200
        assert r.json() == []

    def test_deploy_model(self):
        dmn_xml = load_dmn("treatment-recommendation.dmn")
        r = client.post(
            "/api/v1/decisions/models",
            content=dmn_xml,
            headers={"Content-Type": "application/xml"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "Treatment Recommendation"
        assert data["id"] == "treatment-recommendation"
        assert len(data["inputs"]) == 3
        assert len(data["outputs"]) == 4

    def test_list_after_deploy(self):
        deploy_models()
        r = client.get("/api/v1/decisions/models")
        assert r.status_code == 200
        models = r.json()
        assert len(models) == 2

    def test_get_model(self):
        deploy_models()
        r = client.get("/api/v1/decisions/models/treatment-recommendation")
        assert r.status_code == 200
        data = r.json()
        assert "dmn_xml" in data

    def test_delete_model(self):
        deploy_models()
        r = client.delete("/api/v1/decisions/models/treatment-recommendation")
        assert r.status_code == 204
        r = client.get("/api/v1/decisions/models")
        assert len(r.json()) == 1

    def test_deploy_invalid_xml(self):
        r = client.post(
            "/api/v1/decisions/models",
            content="not xml",
            headers={"Content-Type": "application/xml"},
        )
        assert r.status_code == 400

    def test_different_source_collision_requires_replace(self):
        dmn_xml = load_dmn("treatment-recommendation.dmn")
        first = client.post("/api/v1/decisions/models?source_cpg=CPG-A", content=dmn_xml,
                            headers={"Content-Type": "application/xml"})
        assert first.status_code == 201
        collision = client.post("/api/v1/decisions/models?source_cpg=CPG-B", content=dmn_xml,
                                headers={"Content-Type": "application/xml"})
        assert collision.status_code == 409
        replaced = client.post("/api/v1/decisions/models?source_cpg=CPG-B&replace=true", content=dmn_xml,
                               headers={"Content-Type": "application/xml"})
        assert replaced.status_code == 201
        assert replaced.json()["source_cpg"] == "CPG-B"


class TestCarePlanEndpoint:
    def test_invalid_bundle(self):
        r = client.post("/api/v1/careplans", json={"not": "a bundle"})
        assert r.status_code == 400

    @patch("acp_writer.checkpointer.get_checkpointer", return_value=None)
    @patch("acp_writer.pipeline.build_pipeline")
    def test_empty_brief_after_budget_returns_422_feedback(self, mock_build_pipeline, _mock_checkpointer):
        feedback = "Planning Brief parse/normalization error: invalid activity item"
        compiled = MagicMock()
        compiled.invoke.return_value = {
            "planning_brief": {"goals": [], "activities": []},
            "brief_review_count": 4,
            "brief_review_feedback": feedback,
        }
        graph = MagicMock()
        graph.compile.return_value = compiled
        mock_build_pipeline.return_value = graph

        response = client.post(
            "/api/v1/careplans",
            json=load_bundle("patient-bundle-medication.json"),
        )
        assert response.status_code == 422
        assert response.json()["detail"] == feedback

    @patch("acp_writer.nodes.plan_composer.get_llm")
    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_generates_fhir_bundle(self, mock_fhir, mock_brief, mock_compose):
        brief_json = json.dumps({
            "patient_reference": "Patient/patient-1",
            "applicable_cpgs": [],
            "dmn_audit_trail": [],
            "goals": [{"description": "Lower BP", "source_cpg": "test"}],
            "activities": [{"type": "lifestyle", "description": "DASH diet", "source_cpg": "test"}],
            "conflicts": [],
            "review_status": "pending",
        })
        for mock_llm, content in [
            (mock_compose, brief_json),
            (mock_brief, '{"verdict": "APPROVE", "issues": []}'),
            (mock_fhir, '{"verdict": "APPROVE", "issues": []}'),
        ]:
            resp = MagicMock()
            resp.content = content
            m = MagicMock()
            m.invoke.return_value = resp
            mock_llm.return_value = m

        bundle = load_bundle("patient-bundle-medication.json")
        with patch(
            "acp_writer.pipeline.guideline_resolver",
            return_value={"applicable_cpgs": [{"cpg_id": "test"}], "applicable_dmn_models": []},
        ), patch(
            "acp_writer.pipeline.recommendation_retriever",
            return_value={"recommendations": [{"id": "rec-test", "title": "Test", "content": "Test", "source_cpg": "test"}]},
        ), patch(
            "acp_writer.pipeline.fhir_server_writer",
            return_value={"delivery_status": "skipped"},
        ):
            r = client.post("/api/v1/careplans", json=bundle)
        assert r.status_code == 201
        data = r.json()
        assert data["resourceType"] == "Bundle"
        assert data["type"] == "transaction"


class TestCarePlanEndpoints:
    def test_list_careplans_empty(self):
        r = client.get("/api/v1/careplans")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_careplan_not_found(self):
        r = client.get("/api/v1/careplans/nonexistent")
        assert r.status_code == 404

    def test_update_status_not_found(self):
        r = client.put("/api/v1/careplans/nonexistent/status", json={"status": "active"})
        assert r.status_code == 404


class TestKnowledge:
    def test_search_empty(self):
        r = client.post("/api/v1/knowledge/search", json={"query": "hypertension treatment"})
        assert r.status_code == 200
        assert r.json()["results"] == []

    def test_list_recommendations_empty(self):
        r = client.get("/api/v1/knowledge/recommendations")
        assert r.status_code == 200
        assert r.json() == []
