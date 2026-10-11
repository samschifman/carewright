"""The development router can expose artifacts from a completed CLI run."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cpg_ingester.mocks.data import load_run_directory
from cpg_ingester.mocks.router import router


def _write(directory, name, value):
    (directory / name).write_text(json.dumps(value), encoding="utf-8")


def test_run_directory_populates_completed_detail_and_automation(tmp_path):
    _write(tmp_path, "run-summary.json", {"run_id": "slice-run", "created_at": "2026-10-09T12:00:00Z"})
    _write(tmp_path, "analysis-result.json", {
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002", "title": "Synthetic Hypertension v2"},
        "section_map": [{"heading": "Monitoring", "classification": "process"}],
        "item_manifest": [{"id": "process-1", "type": "process", "name": "Home BP monitoring", "tier": "A"}],
    })
    _write(tmp_path, "generation-result.json", {
        "dmn_results": [],
        "recommendation_results": [{"id": "rec-1", "title": "Monitor readings", "source_cpg": "SYN-HTN-2026-002"}],
        "bpmn_results": [{
            "artifact_id": "artifact-bpmn-1",
            "bpmn_xml": "<bpmn:definitions/>",
            "process_ir": {"process": {"id": "home_bp"}},
            "item": {"id": "process-1", "type": "process", "name": "Home BP monitoring"},
            "template_summary": {
                "id": "home-bp-monitoring",
                "version": "0123456789ab",
                "name": "Home BP monitoring",
                "triggers": [],
                "linked_recommendation_ids": ["rec-1"],
                "linked_decision_model_ids": [],
                "capabilities_used": ["request_readings"],
                "pattern_family": "monitoring",
                "validation": {"status": "valid"},
            },
        }],
    })
    _write(tmp_path, "assembly-report.json", {"cpg_id": "SYN-HTN-2026-002", "recommendations_count": 1})
    _write(tmp_path, "delivery-status.json", {"published": True, "cpg_id": "SYN-HTN-2026-002", "artifacts": [], "errors": []})

    run, detail = load_run_directory(tmp_path)
    assert run["id"] == "slice-run"
    assert detail["status"] == "completed"
    assert "awaitingReview" not in detail
    assert detail["manifest"][0]["id"] == "process-1"
    assert detail["automations"][0]["template_summary"]["id"] == "home-bp-monitoring"
    assert detail["recommendations"][0]["title"] == "Monitor readings"
    assert all(step["status"] == "completed" for step in detail["steps"])


def test_mock_router_serves_the_configured_real_run_directory(tmp_path, monkeypatch):
    _write(tmp_path, "run-summary.json", {"run_id": "slice-run"})
    _write(tmp_path, "analysis-result.json", {"cpg_metadata": {"title": "Real CPG"}, "item_manifest": []})
    _write(tmp_path, "generation-result.json", {})
    _write(tmp_path, "assembly-report.json", {})
    _write(tmp_path, "delivery-status.json", {})
    monkeypatch.setenv("CPG_INGESTER_MOCK_RUN_DIR", str(tmp_path))

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/v1/runs").json()[0]["id"] == "slice-run"
    detail = client.get("/api/v1/runs/slice-run").json()
    assert detail["cpgName"] == "Real CPG"
    assert "awaitingReview" not in detail
