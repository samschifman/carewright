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
from lxml import etree
import pytest

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR, SourceRef, ir_version
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
SCRIPTS_SRC = str(ROOT / "scripts")
if SCRIPTS_SRC not in sys.path:
    sys.path.insert(0, SCRIPTS_SRC)

import automation_service.app as automation_service_module
from automation_service.store import AutomationStore
from slice_automation_guard import write_automation_guard, write_automation_guard_or_raise


FIXTURES = Path(__file__).parent / "fixtures"
REPLAY = FIXTURES / "replay"
LIVE = FIXTURES / "live"
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


class LiveArtifactStore:
    """Read-only artifact loader over the checked-in live replay fixture set."""

    def __init__(self):
        self.template_overrides = {}
        self.templates = {
            f"live/templates/{path.name}": path
            for path in (LIVE / "templates").glob("*.template.json")
        }
        self.dmns = {
            f"live/dmn/{path.name}": path
            for path in (LIVE / "dmn").glob("*.dmn")
        }

    def get(self, ref: str):
        if ref == "live/recommendations.json":
            return json.loads((LIVE / "recommendations.json").read_text())
        if ref in self.template_overrides:
            return deepcopy(self.template_overrides[ref])
        template_path = self.templates.get(ref)
        if template_path is not None:
            return json.loads(template_path.read_text())
        raise KeyError(ref)

    def get_raw(self, ref: str):
        return self.dmns[ref].read_bytes()


def _json_prompt_section(prompt: str, heading: str) -> dict:
    start = prompt.index(heading) + len(heading)
    end = prompt.find("\n## ", start)
    value = prompt[start:] if end < 0 else prompt[start:end]
    return json.loads(value.strip())


class LiveComposerLLM:
    def __init__(self):
        self.responses = []
        for round_number in (1, 2):
            payload = json.loads(
                (LIVE / f"responses/plan-composer/round-{round_number}.response.json").read_text()
            )
            brief = json.loads(payload["content"])
            brief["patient_reference"] = "Patient/synthetic-live-001"
            self.responses.append(json.dumps(brief))

    def invoke(self, _messages):
        content = self.responses.pop(0)
        return SimpleNamespace(content=content)


class LiveSynthesisLLM:
    def __init__(self, *, remove_bindings: dict[str, str] | None = None):
        remove_bindings = remove_bindings or {}
        self.responses = {}
        for request_path in sorted((LIVE / "responses/automation").glob("*/attempt-1.request.json")):
            request = json.loads(request_path.read_text())
            prompt = request["messages"][-1]["content"]
            template = _json_prompt_section(prompt, "## Selected template (pinned)")
            activity = _json_prompt_section(prompt, "## Care-plan activity summary")
            key = (
                template["process"]["id"],
                activity.get("type"),
                activity.get("source_recommendation_id"),
            )
            response = json.loads((request_path.parent / "attempt-1.response.json").read_text())
            parsed_response = json.loads(response["content"])
            parameter_name = remove_bindings.get(template["process"]["id"])
            if parameter_name:
                parsed_response["bindings"] = [
                    binding for binding in parsed_response.get("bindings", [])
                    if binding.get("name") != parameter_name
                ]
            self.responses[key] = parsed_response
        self.used = []

    def invoke(self, messages):
        prompt = messages[-1]["content"]
        template = _json_prompt_section(prompt, "## Selected template (pinned)")
        activity = _json_prompt_section(prompt, "## Care-plan activity summary")
        key = (
            template["process"]["id"],
            activity.get("type"),
            activity.get("source_recommendation_id"),
        )
        if key not in self.responses:
            raise AssertionError("no saved synthesis response matches this template and activity")
        self.used.append(key)
        return SimpleNamespace(content=json.dumps(self.responses[key]))


def _saved_binding_value(template_id: str, parameter_name: str):
    for request_path in sorted((LIVE / "responses/automation").glob("*/attempt-1.request.json")):
        request = json.loads(request_path.read_text())
        template = _json_prompt_section(
            request["messages"][-1]["content"], "## Selected template (pinned)"
        )
        if template["process"]["id"] != template_id:
            continue
        response = json.loads(
            (request_path.parent / "attempt-1.response.json").read_text()
        )
        payload = json.loads(response["content"])
        binding = next(
            (
                item for item in payload.get("bindings", [])
                if item.get("name") == parameter_name
            ),
            None,
        )
        if binding is not None:
            return binding["value"]
    raise AssertionError("saved replay has no matching parameter binding")


def _review_edited_live_template(template_path: Path) -> tuple[dict, str, str]:
    payload = json.loads(template_path.read_text())
    ir = ProcessIR.model_validate(payload["ir"])
    parameter = next(
        item for item in ir.process.acp.parameters
        if item.type == "boolean"
        and item.required
        and item.default is None
        and item.source is not None
        and item.source.kind == "cpg"
    )
    previous = parameter.source.model_copy(deep=True)
    parameter.default = _saved_binding_value(
        ir.process.id,
        parameter.name,
    )
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Offline reviewer",
        note="Selected during pre-delivery review.",
        previous=previous,
    )
    payload["ir"] = ir.model_dump(mode="json", exclude_none=True)
    payload["summary"]["version"] = ir_version(ir)
    payload["summary"]["parameters"] = [
        item.model_dump(mode="json", exclude_none=True)
        for item in ir.process.acp.parameters
    ]
    all_namespaces = {}
    for dmn_path in (LIVE / "dmn").glob("*.dmn"):
        root = etree.fromstring(dmn_path.read_bytes())
        model_id = root.get("id")
        namespace = root.get("namespace")
        if model_id and namespace:
            all_namespaces[model_id] = namespace
    linked_model_ids = payload["summary"].get("linked_decision_model_ids", [])
    namespaces = {
        model_id: all_namespaces[model_id]
        for model_id in linked_model_ids
        if model_id in all_namespaces
    }
    payload["dmn_namespaces"] = namespaces
    payload["bpmn_xml"] = compile(
        ir,
        catalog=load_catalog(),
        dmn_namespaces=namespaces,
    )
    return payload, ir.process.id, parameter.name


def test_automation_guard_persists_failure_artifact_before_raising(tmp_path):
    result_path = tmp_path / "automation-guard.json"

    with pytest.raises(SystemExit, match="no attached live automations"):
        write_automation_guard_or_raise(
            {"activities": []},
            {"entry": []},
            result_path,
        )

    saved = json.loads(result_path.read_text())
    assert saved["ok"] is False
    assert saved["attached_count"] == 0
    assert "no attached live automations" in saved["message"]


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
        automation_token="test-token",
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
        # This is the PHI-safe projection of saved attempt-3: drop the
        # model-authored IR, defaulted text, and max_duration proposal so the
        # packaged operational policy supplies the reserved run window.
        saved_bindings = json.loads((REPLAY / "automation-bindings.json").read_text())
        saved_bindings["bindings"] = [
            binding for binding in saved_bindings.get("bindings", [])
            if binding.get("name") != "max_duration"
        ]
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
        assert next(
            binding for binding in activity.automations[0].bindings
            if binding.name == "max_duration"
        ).source == "policy"

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


def test_live_fixture_replay_runs_pipeline_validation_approval_and_publication(monkeypatch, tmp_path):
    """Replay the saved live composer/synthesizer outputs without model or container calls."""
    import importlib

    plan_composer_node = importlib.import_module("acp_writer.nodes.plan_composer")
    brief_reviewer_node = importlib.import_module("acp_writer.nodes.brief_reviewer")
    synthesizer_node = importlib.import_module("acp_writer.nodes.automation_synthesizer")
    pipeline_module = importlib.import_module("acp_writer.pipeline")

    store = LiveArtifactStore()
    template_refs = sorted(store.templates)
    dmn_refs = sorted(store.dmns)
    assert len(template_refs) == 2
    assert len(dmn_refs) == 6
    reviewed_template_ref = next(
        ref for ref, path in store.templates.items()
        if any(
            parameter.get("type") == "boolean"
            and parameter.get("required")
            and "default" not in parameter
            and (parameter.get("source") or {}).get("kind") == "cpg"
            for parameter in json.loads(path.read_text())["ir"]["process"]["acp"]["parameters"]
        )
    )
    edited_template, reviewed_template_id, reviewed_parameter_name = (
        _review_edited_live_template(store.templates[reviewed_template_ref])
    )
    store.template_overrides[reviewed_template_ref] = edited_template
    recommendation_bundle = store.get("live/recommendations.json")
    assert recommendation_bundle["recommendations"]

    registry = api.AutomationRegistry()
    models: dict = {}
    monkeypatch.setattr(api, "_dynamic_models", models)
    monkeypatch.setattr(decision_engine, "_dynamic_models", models)
    monkeypatch.setattr(api, "_automation_registry", registry)
    monkeypatch.setattr(decision_engine.app.state, "automation_registry", registry)
    monkeypatch.setattr(decision_engine, "_validate_dmn_with_engine", lambda _xml: None)
    monkeypatch.setattr(api, "_validate_dmn_with_engine", lambda _xml: None)
    kogito_calls = []

    def validate_with_stubbed_kogito(xml):
        kogito_calls.append(xml)
        return {"valid": True, "messages": []}

    monkeypatch.setattr(api, "validate_bpmn", validate_with_stubbed_kogito)
    monkeypatch.setattr(decision_engine.app.state, "bpmn_validator", validate_with_stubbed_kogito)
    monkeypatch.setattr(bff, "_artifacts_store", store)
    monkeypatch.setattr(bff, "_reload_cpg_artifacts", lambda: None)

    fhir_resources: dict[str, dict] = {}
    fake_fhir = _fake_fhir_app(fhir_resources)
    automation_store = AutomationStore(directory=tmp_path / "automation-service")
    automation_store.artifact_store = None
    monkeypatch.setattr(automation_service_module, "_store", automation_store)
    job_store = JobStore(directory=tmp_path / "approval-jobs")
    job_store.artifact_store = None
    publisher = AutomationPublisher(
        job_store,
        fhir_url="http://fhir.test/fhir/R4",
        automation_url="http://automation.test",
        automation_token="test-token",
    )
    monkeypatch.setattr(automation_publisher, "_publisher", publisher)
    monkeypatch.setattr(fhir_server_writer, "_care_plans", {})
    monkeypatch.setattr(fhir_server_writer, "FHIR_SERVER_URL", "http://fhir.test/fhir/R4")

    class RegistrationResponse:
        def raise_for_status(self):
            return None

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
            request_params = {
                key: value[-1] if isinstance(value, list) else value
                for key, value in query.items()
            }
            common = {"headers": headers or {}}
            if parsed.path.endswith("/api/v1/decisions/models"):
                return decision_client.post(parsed.path, content=data, params=request_params, **common)
            if parsed.path.endswith("/api/v1/automation/templates"):
                return decision_client.post(parsed.path, json=json, params=request_params, **common)
            if parsed.path == "/api/v1/knowledge/recommendations/batch":
                return RegistrationResponse()
            if parsed.path == "/fhir/R4":
                return fhir_backend_client.post(parsed.path, json=json, **common)
            if parsed.path == "/api/v1/automations":
                return automation_client.post(parsed.path, json=json, **common)
            raise AssertionError("unexpected outbound POST in live replay")

        monkeypatch.setattr(bff.http_requests, "post", dispatch_post)
        monkeypatch.setattr(fhir_server_writer.requests, "post", dispatch_post)
        monkeypatch.setattr(automation_publisher.requests, "post", dispatch_post)

        registration = bff_client.post("/api/v1/notifications/artifacts-available", json={
            "cpg_id": CPG_ID,
            "artifacts": [
                *[
                    {"type": "dmn", "ref": ref, "name": Path(ref).stem}
                    for ref in dmn_refs
                ],
                {"type": "recommendations", "ref": "live/recommendations.json"},
                *[{"type": "bpmn", "ref": ref} for ref in template_refs],
            ],
        })
        assert registration.status_code == 200
        assert registration.json()["errors"] == []
        assert len(decision_client.get("/api/v1/decisions/models").json()) == 6
        assert len(decision_client.get("/api/v1/automation/templates").json()) == 2
        bmp_template = registry.get("treatment-linked-bmp-scheduling")
        assert bmp_template is not None
        assert bmp_template.dmn_summaries["monitoring-plan"].name == "Monitoring Plan"

        saved_audit = json.loads((LIVE / "dmn-audit-trail.json").read_text())
        summaries = api._automation_dmn_summaries()
        recs = recommendation_bundle["recommendations"]

        def replay_guideline_resolver(_state, **_kwargs):
            return {
                "applicable_cpgs": [{"cpg_id": CPG_ID, "title": "Synthetic replay guideline"}],
                "applicable_dmn_models": [
                    summary.model_dump(mode="json", exclude_none=True)
                    for summary in summaries.values()
                ],
                "dmn_dependency_graph": [[summary.id for summary in summaries.values()]],
            }

        def replay_dmn_executor(_state):
            return {"dmn_results": saved_audit}

        def replay_recommendations(_state, *, vector_store):
            return {"recommendations": recs}

        def replay_conflicts(state):
            brief = dict(state.get("planning_brief") or {})
            brief["conflicts"] = brief.get("conflicts") or []
            return {"planning_brief": brief, "conflict_prompt": ""}

        def replay_fhir_review(state):
            return {
                "fhir_review_count": state.get("fhir_review_count", 0) + 1,
                "fhir_review_feedback": "",
            }

        monkeypatch.setattr(pipeline_module, "guideline_resolver", replay_guideline_resolver)
        monkeypatch.setattr(pipeline_module, "dmn_executor", replay_dmn_executor)
        monkeypatch.setattr(pipeline_module, "recommendation_retriever", replay_recommendations)
        monkeypatch.setattr(pipeline_module, "conflict_analyst", replay_conflicts)
        monkeypatch.setattr(pipeline_module, "terminology_validator", lambda _state: {"terminology_issues": []})
        monkeypatch.setattr(pipeline_module, "fhir_semantic_reviewer", replay_fhir_review)

        composer_llm = LiveComposerLLM()
        monkeypatch.setattr(plan_composer_node, "get_llm", lambda _state: composer_llm)

        class QueuedReviewerLLM:
            def __init__(self):
                self.responses = [
                    {"verdict": "REVISE", "issues": [{
                        "severity": "warning",
                        "description": "Apply the saved final brief after the initial review.",
                        "fix": "Use the saved second-round composition.",
                    }]},
                    {"verdict": "APPROVE", "issues": []},
                ]

            def invoke(self, _messages):
                return SimpleNamespace(content=json.dumps(self.responses.pop(0)))

        reviewer_llm = QueuedReviewerLLM()
        monkeypatch.setattr(brief_reviewer_node, "get_llm", lambda _state: reviewer_llm)

        synthesis_llm = LiveSynthesisLLM(
            remove_bindings={reviewed_template_id: reviewed_parameter_name}
        )
        monkeypatch.setattr(synthesizer_node, "get_llm", lambda _state: synthesis_llm)
        monkeypatch.setattr(synthesizer_node, "_review", lambda *_args, **_kwargs: (True, ""))
        monkeypatch.setattr(
            "acp_writer.nodes.automation_binding.find_terminology_concept",
            lambda system, term: SimpleNamespace(
                found=False, system=system, code=None,
            ),
        )
        def validate_registered_template_summaries(payload):
            response = decision_client.post("/api/v1/automation/validate", json=payload)
            assert response.status_code == 200
            return response.json()

        graph = pipeline_module.build_pipeline(
            automation_registry=registry,
            automation_validator=validate_registered_template_summaries,
            guidelines_store=None,
            vector_store=None,
            decision_models_provider=lambda: list(summaries.values()),
        ).compile()
        careplan_output_dir = tmp_path / "careplans" / CAREPLAN_ID
        result = graph.invoke({
            "run_id": "offline-live-fixture-replay",
            "output_dir": str(careplan_output_dir),
            "ips_bundle": json.loads((LIVE / "patient-bundle.json").read_text()),
            "careplan_id": CAREPLAN_ID,
            "approved": False,
            "llm_model": "saved-response-replay",
        })

        assert result["delivery_status"] == "draft"
        assert not composer_llm.responses
        assert not reviewer_llm.responses
        # One synthesis per template per plan: the two templates are shared
        # across the five activities they serve rather than instantiated five times.
        assert len(synthesis_llm.used) == 2
        assert len(kogito_calls) >= 2

        guard_path = careplan_output_dir / "automation-guard.json"
        guard = write_automation_guard(result["planning_brief"], result["fhir_bundle"], guard_path)
        assert guard_path.is_file()
        assert guard["ok"], guard.get("errors")
        # Two shared instances serve the five activities: one valid, one incomplete.
        assert guard["attached_count"] == 2
        assert guard["valid_instances"] == 1
        assert all(item["status"] in {"valid", "incomplete"} for item in guard["attached"])
        valid_automation = next(
            item for item in guard["attached"]
            if item["status"] == "valid"
            and item["template_refs"][0]["template_id"] == reviewed_template_id
        )
        valid_payload = next(
            automation
            for activity in result["planning_brief"]["activities"]
            for automation in activity.get("automations", [])
            if automation["validation"]["status"] == "valid"
            and automation["template_refs"][0]["template_id"] == reviewed_template_id
        )
        assert valid_payload["validation"]["rungs_passed"] == [
            "L0", "L1", "L2", "L3", "L4", "L5a", "L6", "L7",
        ]
        assert next(
            binding for binding in valid_payload["bindings"]
            if binding["name"] == "max_duration"
        )["source"] == "policy"
        # Precedence (design §3.1): a plan-derived value, here the decision
        # input the plan evaluated for this patient, outranks the reviewer's
        # edited template default. The reviewer edit still travels on the
        # pinned snapshot's parameter provenance.
        reviewed_binding = next(
            binding for binding in valid_payload["bindings"]
            if binding["name"] == reviewed_parameter_name
        )
        assert reviewed_binding["source"] in {"plan-derived", "reviewer"}
        snapshot_parameter = next(
            parameter
            for parameter in valid_payload["template_snapshots"][0]["process"]["acp"]["parameters"]
            if parameter["name"] == reviewed_parameter_name
        )
        assert snapshot_parameter["source"]["kind"] == "reviewer"

        approved = fhir_writer_client.put(
            f"/api/v1/careplans/{CAREPLAN_ID}/status",
            json={"status": "active", "reviewer": {"display": "Synthetic replay clinician"}},
        )
        assert approved.status_code == 200
        assert approved.json()["delivery_status"] == "delivered"
        assert approved.json()["automation_publication"]["status"] == "published"

        careplan = next(value for key, value in fhir_resources.items() if key.startswith("CarePlan/"))
        assert careplan["status"] == "active"
        if guard["valid_instances"]:
            assert any(key.startswith("Task/") for key in fhir_resources)
            assert any(key.startswith("DocumentReference/") for key in fhir_resources)
        records = automation_client.get("/api/v1/automations", params={"careplan_id": CAREPLAN_ID})
        assert records.status_code == 200
        assert len(records.json()) == guard["valid_instances"]
        for record in records.json():
            fetched = automation_client.get(
                f"/api/v1/automations/{CAREPLAN_ID}/{record['automation_id']}",
                params={"revision": record["revision"]},
            )
            assert fetched.status_code == 200
