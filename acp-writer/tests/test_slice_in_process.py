"""In-process twin of the from-golden care-plan approval and automation slice."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from acp_writer import api
from acp_writer.nodes import fhir_server_writer
from acp_writer.nodes.automation_synthesizer import synthesize_for_activity
from acp_writer.nodes.fhir_bundle_generator import fhir_bundle_generator
from acp_writer.planning_brief import PlanActivity, PlanningBrief
from acp_writer.services import automation_publisher, bff, decision_engine, fhir_server
from acp_writer.services.automation_publisher import AutomationPublisher
from acp_writer.services.job_store import JobStore
ROOT = Path(__file__).resolve().parents[2]
AUTOMATION_SRC = str(ROOT / "automation" / "src")
if AUTOMATION_SRC not in sys.path:
    sys.path.insert(0, AUTOMATION_SRC)

import automation_service.app as automation_service_module
from automation_service.store import AutomationStore


FIXTURES = Path(__file__).parent / "fixtures"
REPLAY = FIXTURES / "replay"
CPG_ID = "SYN-HTN-2026-002"
CAREPLAN_ID = "slice-careplan-in-process"
AUTOMATION_EXTENSION = (
    "https://github.com/samschifman/cpg-to-acp/fhir/StructureDefinition/activity-automation"
)


class GoldenArtifactStore:
    def __init__(self, templates: dict[str, Path], dmns: dict[str, Path]):
        self.templates = templates
        self.dmns = dmns

    def get(self, ref: str):
        template = json.loads(self.templates[ref].read_text())
        template_id = template["summary"]["id"]
        # The source goldens use one placeholder recommendation id; the replay
        # loader normally replaces these with ids from the extracted bundle.
        template["summary"]["linked_recommendation_ids"] = [f"synthetic-rec-{template_id}"]
        return template

    def get_raw(self, ref: str):
        return self.dmns[ref].read_bytes()


class SavedResponseLLM:
    def __init__(self, response: dict):
        self.response = response

    def invoke(self, _messages):
        return type("Response", (), {"content": json.dumps(self.response)})()


def _rewrite(value, references):
    if isinstance(value, dict):
        return {key: _rewrite(child, references) for key, child in value.items()}
    if isinstance(value, list):
        return [_rewrite(child, references) for child in value]
    if isinstance(value, str):
        return references.get(value, value)
    return value


def _fake_fhir_app(resources: dict[str, dict]) -> FastAPI:
    app = FastAPI()

    @app.post("/fhir/R4")
    async def transaction(request: Request):
        bundle = await request.json()
        entries = bundle.get("entry", [])
        references = {}
        for index, entry in enumerate(entries, start=1):
            resource = entry.get("resource", {})
            resource_type = resource.get("resourceType")
            if not resource_type:
                raise HTTPException(status_code=400, detail="transaction entry has no resourceType")
            references[entry.get("fullUrl", "")] = f"{resource_type}/test-{index}"
        response_entries = []
        for entry in entries:
            source = entry["resource"]
            resource_type = source["resourceType"]
            server_ref = references[entry["fullUrl"]]
            resource_id = server_ref.split("/", 1)[1]
            stored = _rewrite(deepcopy(source), references)
            stored["id"] = resource_id
            resources[server_ref] = stored
            response_entries.append({
                "response": {
                    "status": "201 Created",
                    "location": f"{server_ref}/_history/1",
                },
            })
        return {"resourceType": "Bundle", "type": "transaction-response", "entry": response_entries}

    @app.get("/fhir/R4/{resource_type}/{resource_id}")
    def read_resource(resource_type: str, resource_id: str):
        resource = resources.get(f"{resource_type}/{resource_id}")
        if resource is None:
            raise HTTPException(status_code=404, detail="resource not found")
        return resource

    return app


def test_saved_replay_generates_and_publishes_approved_careplan(monkeypatch, tmp_path):
    """Exercise the real registration, FHIR generation, approval, and publication apps."""
    monkeypatch.setattr(
        "acp_writer.nodes.automation_binding.find_terminology_concept",
        lambda system, term: SimpleNamespace(
            found=(term == "blood pressure"),
            system=system,
            code="lookup-code" if term == "blood pressure" else None,
        ),
    )
    template_paths = {
        path.name.removesuffix(".template.json"): path
        for path in sorted((REPLAY / "templates").glob("*.template.json"))
    }
    assert len(template_paths) == 4
    template_by_ref = {
        f"templates/{template_id}": path
        for template_id, path in template_paths.items()
    }
    dmn_paths = {
        path.name: path
        for path in (
            FIXTURES / "dmn/home-bp-blood-pressure-classification.dmn",
            FIXTURES / "dmn/home-bp-treatment-recommendation.dmn",
            FIXTURES / "dmn/home-bp-monitoring-plan.dmn",
        )
    }
    dmn_by_ref = {f"dmn/{name}": path for name, path in dmn_paths.items()}
    golden_store = GoldenArtifactStore(template_by_ref, dmn_by_ref)

    registry = api.AutomationRegistry()
    models: dict = {}
    monkeypatch.setattr(api, "_dynamic_models", models)
    monkeypatch.setattr(decision_engine, "_dynamic_models", models)
    monkeypatch.setattr(api, "_automation_registry", registry)
    monkeypatch.setattr(decision_engine.app.state, "automation_registry", registry)
    monkeypatch.setattr(decision_engine, "_validate_dmn_with_engine", lambda _xml: None)
    monkeypatch.setattr(api, "validate_bpmn", lambda _xml: {"valid": True, "messages": []})
    monkeypatch.setattr(
        decision_engine.app.state,
        "bpmn_validator",
        lambda _xml: {"valid": True, "messages": []},
    )
    monkeypatch.setattr(bff, "_artifacts_store", golden_store)
    monkeypatch.setattr(bff, "_reload_cpg_artifacts", lambda: None)

    fhir_resources: dict[str, dict] = {}
    fake_fhir = _fake_fhir_app(fhir_resources)
    automation_store = AutomationStore(directory=tmp_path / "automation")
    automation_store.artifact_store = None
    monkeypatch.setattr(automation_service_module, "_store", automation_store)
    job_store = JobStore(directory=tmp_path / "jobs")
    job_store.artifact_store = None
    publisher = AutomationPublisher(
        job_store,
        fhir_url="http://fhir.test/fhir/R4",
        automation_url="http://automation.test",
    )
    monkeypatch.setattr(automation_publisher, "_publisher", publisher)
    monkeypatch.setattr(fhir_server_writer, "_care_plans", {})
    monkeypatch.setattr(fhir_server_writer, "FHIR_SERVER_URL", "http://fhir.test/fhir/R4")

    with (
        TestClient(decision_engine.app) as decision_client,
        TestClient(bff.app) as bff_client,
        TestClient(fake_fhir) as fhir_backend_client,
        TestClient(automation_service_module.app) as automation_client,
        TestClient(fhir_server.app) as fhir_writer_client,
    ):
        def dispatch_post(url, *, data=None, json=None, params=None, headers=None, timeout=None):
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            query.update(params or {})
            request_params = {key: value[-1] if isinstance(value, list) else value for key, value in query.items()}
            common = {"headers": headers or {}}
            if parsed.path.endswith("/api/v1/decisions/models"):
                return decision_client.post(parsed.path, content=data, params=request_params, **common)
            if parsed.path.endswith("/api/v1/automation/templates"):
                return decision_client.post(parsed.path, json=json, params=request_params, **common)
            if parsed.path == "/fhir/R4":
                return fhir_backend_client.post(parsed.path, json=json, **common)
            if parsed.path == "/api/v1/automations":
                return automation_client.post(parsed.path, json=json, **common)
            raise AssertionError("unexpected outbound POST in in-process slice test")

        monkeypatch.setattr(bff.http_requests, "post", dispatch_post)
        monkeypatch.setattr(fhir_server_writer.requests, "post", dispatch_post)

        registration = bff_client.post("/api/v1/notifications/artifacts-available", json={
            "cpg_id": CPG_ID,
            "artifacts": [
                *[
                    {"type": "dmn", "ref": ref, "name": Path(ref).stem}
                    for ref in sorted(dmn_by_ref)
                ],
                *[
                    {"type": "bpmn", "ref": ref}
                    for ref in sorted(template_by_ref)
                ],
            ],
        })
        assert registration.status_code == 200
        assert registration.json()["errors"] == []
        assert len(decision_client.get("/api/v1/decisions/models").json()) == 3
        assert len(decision_client.get("/api/v1/automation/templates").json()) == 4

        home_bp = registry.get("home-bp-monitoring")
        assert home_bp is not None
        activity = PlanActivity(
            id="activity-home-bp",
            type="monitoring",
            description="Monitor home blood pressure readings for synthetic test subject.",
            source_recommendation_id=home_bp.summary.linked_recommendation_ids[0],
            source_cpg=CPG_ID,
        )
        brief = PlanningBrief(
            patient_reference="Patient/synthetic-001",
            applicable_cpgs=[CPG_ID],
            goals=[],
            activities=[activity],
        )
        # This is the PHI-safe projection of saved attempt-3: retain its
        # max_duration proposal, drop the model-authored IR and defaulted text,
        # and replace the free-text reason with a non-patient-specific summary.
        saved_bindings = json.loads((REPLAY / "automation-bindings.json").read_text())
        llm = SavedResponseLLM(saved_bindings)

        def validate_through_registry(payload):
            response = decision_client.post("/api/v1/automation/validate", json=payload)
            assert response.status_code == 200
            return response.json()

        activity.automations = synthesize_for_activity(
            activity,
            registry,
            [],
            brief={"dmn_audit_trail": []},
            state={"patient_demographics": {"name": "Synthetic Patient"}},
            llm=llm,
            validator=validate_through_registry,
            reviewer=lambda **_kwargs: (True, ""),
        )
        assert len(activity.automations) == 1
        assert activity.automations[0].validation.status == "valid"

        generated = fhir_bundle_generator({
            "planning_brief": brief.model_dump(mode="json", exclude_none=True),
            "patient_demographics": {"name": "Synthetic Patient", "gender": "unknown"},
            "careplan_id": CAREPLAN_ID,
            "llm_model": "test-model",
        })["fhir_bundle"]
        careplan = next(
            entry["resource"] for entry in generated["entry"]
            if entry.get("resource", {}).get("resourceType") == "CarePlan"
        )
        assert any(
            extension.get("url") == AUTOMATION_EXTENSION
            for item in careplan.get("activity", [])
            for extension in item.get("extension", [])
        )
        assert any(entry.get("resource", {}).get("resourceType") == "Task" for entry in generated["entry"])
        assert any(
            entry.get("resource", {}).get("resourceType") == "DocumentReference"
            for entry in generated["entry"]
        )

        draft = fhir_writer_client.post("/api/v1/write", json={
            "fhir_bundle": generated,
            "careplan_id": CAREPLAN_ID,
            "patient_reference": "Patient/synthetic-001",
            "approved": False,
        })
        assert draft.status_code == 200
        assert draft.json()["delivery_status"] == "draft"
        approved = fhir_writer_client.put(
            f"/api/v1/careplans/{CAREPLAN_ID}/status",
            json={"status": "active", "reviewer": {"display": "Synthetic clinician"}},
        )
        assert approved.status_code == 200
        assert approved.json()["delivery_status"] == "delivered"
        assert approved.json()["automation_publication"]["status"] == "published"

        stored_records = automation_client.get(
            "/api/v1/automations", params={"careplan_id": CAREPLAN_ID}
        )
        assert stored_records.status_code == 200
        assert len(stored_records.json()) == 1
        record = stored_records.json()[0]
        assert record["status"] == "active"
        retrieved = automation_client.get(
            f"/api/v1/automations/{CAREPLAN_ID}/{record['automation_id']}",
            params={"revision": record["revision"]},
        )
        assert retrieved.status_code == 200
        assert retrieved.json()["revision"] == record["revision"]
        assert any(key.startswith("Task/") for key in fhir_resources)
        assert any(key.startswith("DocumentReference/") for key in fhir_resources)
