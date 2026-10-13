"""Tests for the FHIR Server Writer node and approval workflow."""

from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace

import acp_writer.nodes.fhir_server_writer as writer_module
import acp_writer.services.automation_publisher as automation_publisher_module
from acp_writer.nodes.fhir_server_writer import (
    _care_plans,
    _parse_server_ids,
    _post_bundle,
    approve_care_plan,
    fhir_server_writer,
    get_care_plan,
    list_care_plans,
    reject_care_plan,
)
from acp_writer.planning_brief import (
    ActivityType,
    ConflictCategory,
    ConflictEntry,
    ConflictSeverity,
    ConflictSource,
    ConflictStatus,
    FHIRCode,
    PlanActivity,
    PlanGoal,
    PlanningBrief,
    ReviewStatus,
    TargetValue,
)
from acp_writer.services.ai_transparency import ACP_EXT_BASE, is_conflict_provenance
from acp_writer.services.reviewer import ReviewerContext, reviewer_from_payload
from acp_writer.services.automation_publisher import AutomationPublisher
from acp_writer.services.job_store import JobStore
from acp_writer.validators.fhir_bundle_builder import build_fhir_bundle

import pytest


def _sample_bundle() -> dict:
    brief = PlanningBrief(
        patient_reference="Patient/patient-1",
        applicable_cpgs=["SYN-HTN-2026-001"],
        goals=[PlanGoal(description="Lower BP", source_cpg="SYN-HTN-2026-001")],
        activities=[
            PlanActivity(
                type=ActivityType.MEDICATION,
                description="Lisinopril",
                dose="10 mg",
                source_recommendation_id="rec-1",
                source_cpg="SYN-HTN-2026-001",
            ),
        ],
        review_status=ReviewStatus.APPROVED,
    )
    return build_fhir_bundle(brief)


def _bundle_with_conflict() -> dict:
    brief = PlanningBrief(
        patient_reference="Patient/patient-1",
        applicable_cpgs=["SYN-HTN-2026-001", "SYN-DM2-2026-001"],
        goals=[PlanGoal(description="Lower BP", source_cpg="SYN-HTN-2026-001")],
        activities=[
            PlanActivity(
                type=ActivityType.MEDICATION,
                description="Lisinopril",
                dose="10 mg",
                source_recommendation_id="rec-1",
                source_cpg="SYN-HTN-2026-001",
            ),
        ],
        conflicts=[
            ConflictEntry(
                id="conf-x",
                category=ConflictCategory.DIVERGENT_TARGET,
                severity=ConflictSeverity.WARNING,
                status=ConflictStatus.DETECTED,
                description="different targets",
                rationale="a vs b",
                confidence="high",
                goal_indices=[0],
                activity_indices=[0],
                sources=[
                    ConflictSource(cpg_id="SYN-HTN-2026-001", recommendation_id="rec-1"),
                    ConflictSource(cpg_id="SYN-DM2-2026-001", recommendation_id="rec-2"),
                ],
            )
        ],
        review_status=ReviewStatus.APPROVED,
    )
    return build_fhir_bundle(brief)


@pytest.fixture(autouse=True)
def clear_care_plans(monkeypatch, tmp_path):
    _care_plans.clear()
    calls = []

    monkeypatch.setattr(
        automation_publisher_module,
        "_publisher",
        AutomationPublisher(JobStore(directory=tmp_path), automation_url="http://automation"),
    )

    def fake_post(url, *, json=None, headers=None, timeout):
        calls.append(json)
        if json is None:
            return SimpleNamespace(status_code=200, content=b"{}", text="", json=lambda: {})
        response_entries = []
        for entry in json.get("entry", []):
            resource = entry.get("resource", {})
            response_entries.append({
                "response": {
                    "location": f"{resource.get('resourceType')}/{resource.get('id')}/_history/1",
                },
            })
        return SimpleNamespace(
            status_code=201,
            content=b"{}",
            text="",
            json=lambda: {"entry": response_entries},
        )

    monkeypatch.setattr(writer_module.requests, "post", fake_post)
    yield
    _care_plans.clear()


class TestFHIRServerWriter:
    def test_non_json_success_response_is_left_for_reconciliation(self, monkeypatch):
        def fake_post(*args, **kwargs):
            def invalid_json():
                raise ValueError("not JSON")

            return SimpleNamespace(
                status_code=201,
                content=b"created",
                text="created",
                json=invalid_json,
            )

        monkeypatch.setattr(writer_module.requests, "post", fake_post)

        result = _post_bundle(_sample_bundle())

        assert result["delivery_status"] == "stored_locally"
        assert result["server_status_code"] == 201
        assert result["server_ids"] == {}
        assert "valid JSON" in result["error"]

    def test_auth_failure_preserves_http_status_without_response_text(self, monkeypatch):
        monkeypatch.setattr(
            writer_module.requests,
            "post",
            lambda *args, **kwargs: SimpleNamespace(
                status_code=401,
                content=b'{"resourceType":"OperationOutcome"}',
                text='{"resourceType":"OperationOutcome"}',
                json=lambda: {"resourceType": "OperationOutcome"},
            ),
        )

        result = _post_bundle(_sample_bundle())

        assert result["delivery_status"] == "stored_locally"
        assert result["server_status_code"] == 401
        assert "error" not in result

    def test_stores_locally_when_server_unavailable(self):
        state = {
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
        }
        result = fhir_server_writer(state)
        assert result["delivery_status"] == "draft"
        assert result["careplan_id"] != ""

    def test_empty_bundle_skipped(self):
        result = fhir_server_writer({"fhir_bundle": {"entry": []}})
        assert result["delivery_status"] == "skipped"

    def test_care_plan_stored_in_memory(self):
        state = {
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
        }
        result = fhir_server_writer(state)
        cp = get_care_plan(result["careplan_id"])
        assert cp is not None
        assert cp["patient_reference"] == "Patient/patient-1"

    def test_care_plan_has_server_ids_field(self):
        state = {
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
        }
        result = fhir_server_writer(state)
        cp = get_care_plan(result["careplan_id"])
        assert "server_ids" in cp

    def test_business_id_is_stable_in_careplan_identifier_and_upsert_condition(self):
        stable_id = "workflow-run-123"
        result = fhir_server_writer({
            "fhir_bundle": _sample_bundle(),
            "careplan_id": stable_id,
            "patient_reference": "Patient/patient-1",
        })
        assert result["careplan_id"] == stable_id
        careplan_entry = next(
            entry for entry in get_care_plan(stable_id)["bundle"]["entry"]
            if entry["resource"].get("resourceType") == "CarePlan"
        )
        assert careplan_entry["resource"]["identifier"][0]["value"] == stable_id
        assert careplan_entry["request"]["ifNoneExist"].endswith(f"|{stable_id}")


class TestParseServerIds:
    def test_maps_fullurl_to_server_ref(self):
        bundle = {
            "entry": [
                {"fullUrl": "urn:uuid:aaa"},
                {"fullUrl": "urn:uuid:bbb"},
            ],
        }
        response = {
            "entry": [
                {"response": {"status": "201", "location": "Patient/123/_history/1"}},
                {"response": {"status": "201", "location": "CarePlan/456/_history/1"}},
            ],
        }
        id_map = _parse_server_ids(bundle, response)
        assert id_map["urn:uuid:aaa"] == "Patient/123"
        assert id_map["urn:uuid:bbb"] == "CarePlan/456"

    def test_empty_response(self):
        assert _parse_server_ids({"entry": []}, {}) == {}


class TestCarePlanCRUD:
    def _store_care_plan(self) -> str:
        state = {
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
        }
        result = fhir_server_writer(state)
        return result["careplan_id"]

    def test_list_empty(self):
        assert list_care_plans() == []

    def test_list_after_store(self):
        self._store_care_plan()
        plans = list_care_plans()
        assert len(plans) == 1

    def test_list_filter_by_patient(self):
        self._store_care_plan()
        plans = list_care_plans(patient="Patient/patient-1")
        assert len(plans) == 1
        plans = list_care_plans(patient="Patient/other")
        assert len(plans) == 0

    def test_get_care_plan(self):
        cp_id = self._store_care_plan()
        cp = get_care_plan(cp_id)
        assert cp is not None
        assert cp["id"] == cp_id
        assert "bundle" in cp

    def test_get_not_found(self):
        assert get_care_plan("nonexistent") is None


class TestApprovalWorkflow:
    def _store_care_plan(self) -> str:
        state = {
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
        }
        result = fhir_server_writer(state)
        return result["careplan_id"]

    def test_approve(self):
        cp_id = self._store_care_plan()
        result = approve_care_plan(cp_id, reviewer=ReviewerContext(display="Dr. Smith"))
        assert result["status"] == "active"
        assert result["fhir_server_status"] == 201

        cp = get_care_plan(cp_id)
        assert cp["status"] == "active"
        assert cp["delivery_status"] == "delivered"

        bundle = cp["bundle"]
        for entry in bundle["entry"]:
            resource = entry["resource"]
            security = resource.get("meta", {}).get("security", [])
            for sec in security:
                assert sec["code"] != "AIAST", f"AIAST should be replaced with CLINAST_AIRPT on {resource['resourceType']}"

            if resource["resourceType"] == "CarePlan":
                assert resource["status"] == "active"

    def test_approve_adds_verifier(self):
        cp_id = self._store_care_plan()
        approve_care_plan(cp_id, reviewer=ReviewerContext(display="Dr. Smith"))

        cp = get_care_plan(cp_id)
        bundle = cp["bundle"]
        for entry in bundle["entry"]:
            resource = entry["resource"]
            if resource["resourceType"] == "Provenance":
                profiles = resource.get("meta", {}).get("profile", [])
                if any("AI-Provenance" in p for p in profiles):
                    agents = resource.get("agent", [])
                    verifiers = [a for a in agents if
                                 a.get("type", {}).get("coding", [{}])[0].get("code") == "verifier"]
                    assert len(verifiers) == 1
                    assert verifiers[0]["who"]["display"] == "Dr. Smith"

    def test_reject(self):
        cp_id = self._store_care_plan()
        result = reject_care_plan(cp_id, reason="Dose too high")
        assert result["status"] == "entered-in-error"
        assert result["reason"] == "Dose too high"

        cp = get_care_plan(cp_id)
        assert cp["status"] == "entered-in-error"

        bundle = cp["bundle"]
        for entry in bundle["entry"]:
            resource = entry["resource"]
            if resource["resourceType"] == "CarePlan":
                assert resource["status"] == "entered-in-error"
                notes = [n["text"] for n in resource.get("note", [])]
                assert any("Dose too high" in n for n in notes)

    def test_approve_not_found(self):
        assert approve_care_plan("nonexistent") is None

    def test_draft_is_not_written_until_approval_and_approval_is_idempotent(self, monkeypatch):
        calls = []

        def fake_post(url, *, json, headers, timeout):
            calls.append((url, json))
            if url != writer_module.FHIR_SERVER_URL:
                return SimpleNamespace(status_code=201, content=b"{}", text="", json=lambda: {})
            response_entries = [
                {"response": {"location": f"{entry['resource']['resourceType']}/{entry['resource']['id']}/_history/1"}}
                for entry in json["entry"]
            ]
            return SimpleNamespace(
                status_code=201,
                content=b"{}",
                text="",
                json=lambda: {"entry": response_entries},
            )

        monkeypatch.setattr(writer_module.requests, "post", fake_post)
        cp_id = self._store_care_plan()
        assert calls == []
        assert get_care_plan(cp_id)["status"] == "draft"

        result = approve_care_plan(cp_id, reviewer=ReviewerContext(display="Dr. Smith"))
        assert result["delivery_status"] == "delivered"
        fhir_calls = [bundle for url, bundle in calls if url == writer_module.FHIR_SERVER_URL]
        assert len(fhir_calls) == 1
        careplan_entry = next(
            entry for entry in fhir_calls[0]["entry"]
            if entry["resource"].get("resourceType") == "CarePlan"
        )
        assert careplan_entry["resource"]["status"] == "active"

        approve_care_plan(cp_id, reviewer=ReviewerContext(display="Dr. Smith"))
        assert len([url for url, _ in calls if url == writer_module.FHIR_SERVER_URL]) == 1

    def test_approval_redrive_after_write_crash_searches_before_reposting(self, monkeypatch):
        fhir_writes = []
        automation_writes = []
        saved_bundle = {}

        def fake_post(url, *, json, headers=None, timeout):
            if url == writer_module.FHIR_SERVER_URL:
                fhir_writes.append(json)
                saved_bundle.update(deepcopy(json))

                def invalid_json():
                    raise ValueError("server closed after writing")

                return SimpleNamespace(status_code=201, content=b"created", text="created", json=invalid_json)
            automation_writes.append(json)
            return SimpleNamespace(
                status_code=201,
                content=b"{}",
                text="",
                json=lambda: {"status": "accepted"},
            )

        def fake_get(url, *, params, headers=None, timeout):
            careplan = next(
                deepcopy(entry["resource"])
                for entry in saved_bundle.get("entry", [])
                if entry["resource"].get("resourceType") == "CarePlan"
            )
            careplan["id"] = "careplan-server"
            careplan["subject"]["reference"] = "Patient/patient-server"
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"entry": [{"resource": careplan}]},
                raise_for_status=lambda: None,
            )

        monkeypatch.setattr(writer_module.requests, "post", fake_post)
        monkeypatch.setattr(writer_module.requests, "get", fake_get)
        cp_id = self._store_care_plan()
        first = approve_care_plan(cp_id, reviewer=ReviewerContext(display="Dr. Smith"))
        assert first["delivery_status"] == "stored_locally"

        second = approve_care_plan(cp_id, reviewer=ReviewerContext(display="Dr. Smith"))

        assert second["delivery_status"] == "delivered"
        assert len(fhir_writes) == 1, "re-drive reconciles the committed CarePlan instead of posting twice"
        assert len(automation_writes) == 1

    def test_reject_not_found(self):
        assert reject_care_plan("nonexistent", "reason") is None


class TestApproveReviewerAndConflict:
    def _store(self, bundle: dict) -> str:
        return fhir_server_writer({
            "fhir_bundle": bundle,
            "patient_reference": "Patient/patient-1",
        })["careplan_id"]

    def test_reviewer_context_becomes_verifier(self):
        cp_id = self._store(_sample_bundle())
        reviewer = ReviewerContext(
            display="Dr. Alice",
            reference="Practitioner/alice",
            identifier_system="http://hl7.org/fhir/sid/us-npi",
            identifier_value="123",
        )
        approve_care_plan(cp_id, reviewer=reviewer)

        who = None
        for entry in get_care_plan(cp_id)["bundle"]["entry"]:
            r = entry["resource"]
            if r["resourceType"] == "Provenance":
                for a in r.get("agent", []):
                    if a.get("type", {}).get("coding", [{}])[0].get("code") == "verifier":
                        who = a["who"]
        assert who is not None
        assert who["display"] == "Dr. Alice"
        assert who["reference"] == "Practitioner/alice"
        assert who["identifier"] == {
            "system": "http://hl7.org/fhir/sid/us-npi", "value": "123"
        }

    def test_default_reviewer_when_none(self, monkeypatch):
        monkeypatch.delenv("ACP_REVIEWER_DISPLAY", raising=False)
        cp_id = self._store(_sample_bundle())
        approve_care_plan(cp_id)
        displays = [
            a["who"].get("display")
            for e in get_care_plan(cp_id)["bundle"]["entry"]
            if e["resource"]["resourceType"] == "Provenance"
            for a in e["resource"].get("agent", [])
            if a.get("type", {}).get("coding", [{}])[0].get("code") == "verifier"
        ]
        assert "Demo Clinician" in displays

    def test_approve_flips_conflict_status(self):
        cp_id = self._store(_bundle_with_conflict())
        approve_care_plan(cp_id, reviewer=ReviewerContext(display="Dr. Smith"))

        conflict_provs = [
            e["resource"]
            for e in get_care_plan(cp_id)["bundle"]["entry"]
            if e["resource"]["resourceType"] == "Provenance"
            and is_conflict_provenance(e["resource"])
        ]
        assert len(conflict_provs) == 1
        status = {
            ext["url"]: ext for ext in conflict_provs[0]["extension"]
        }[f"{ACP_EXT_BASE}/conflict-status"]
        assert status["valueCode"] == "acknowledged"

    def test_back_compat_clinician_string(self):
        # F12: the legacy bare-clinician field is reconciled by
        # reviewer_from_payload (the single shim), then approve consumes the
        # resulting ReviewerContext — no clinician kwarg on approve_care_plan.
        cp_id = self._store(_sample_bundle())
        reviewer = reviewer_from_payload(None, clinician="Dr. Legacy")
        result = approve_care_plan(cp_id, reviewer=reviewer)
        assert result["status"] == "active"
        displays = [
            a["who"].get("display")
            for e in get_care_plan(cp_id)["bundle"]["entry"]
            if e["resource"]["resourceType"] == "Provenance"
            for a in e["resource"].get("agent", [])
            if a.get("type", {}).get("coding", [{}])[0].get("code") == "verifier"
        ]
        assert "Dr. Legacy" in displays


def _verifier_displays(bundle: dict) -> list[str]:
    return [
        a["who"].get("display")
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Provenance"
        for a in e["resource"].get("agent", [])
        if a.get("type", {}).get("coding", [{}])[0].get("code") == "verifier"
    ]


class TestWriterApprovedTransition:
    """F1: the deployed WriteFHIR path (approved=True) must run the FULL approval
    transition — verifier + conflict-ack — not just the security-tag swap the old
    _apply_active_tags did. Regression pin for the split-path miss in issue #169.
    """

    def test_approved_writer_records_verifier(self, monkeypatch):
        monkeypatch.delenv("ACP_REVIEWER_DISPLAY", raising=False)
        result = fhir_server_writer({
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
            "approved": True,
        })
        bundle = get_care_plan(result["careplan_id"])["bundle"]
        # Before F1 the approved writer path added no verifier at all.
        assert "Demo Clinician" in _verifier_displays(bundle)

    def test_approved_writer_uses_state_reviewer(self):
        result = fhir_server_writer({
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
            "approved": True,
            "reviewer": ReviewerContext(
                display="Dr. Deployed", reference="Practitioner/dep"
            ).model_dump(),
        })
        bundle = get_care_plan(result["careplan_id"])["bundle"]
        assert "Dr. Deployed" in _verifier_displays(bundle)

    def test_approved_writer_acknowledges_conflicts(self):
        result = fhir_server_writer({
            "fhir_bundle": _bundle_with_conflict(),
            "patient_reference": "Patient/patient-1",
            "approved": True,
        })
        bundle = get_care_plan(result["careplan_id"])["bundle"]
        conflict_provs = [
            e["resource"]
            for e in bundle["entry"]
            if e["resource"]["resourceType"] == "Provenance"
            and is_conflict_provenance(e["resource"])
        ]
        assert conflict_provs
        for prov in conflict_provs:
            status = {ext["url"]: ext for ext in prov["extension"]}[
                f"{ACP_EXT_BASE}/conflict-status"
            ]
            # Before F1 the deployed path left conflicts as "detected".
            assert status["valueCode"] == "acknowledged"

    def test_draft_writer_leaves_ai_tags(self):
        # Not-yet-approved writes stay AIAST with no verifier.
        result = fhir_server_writer({
            "fhir_bundle": _sample_bundle(),
            "patient_reference": "Patient/patient-1",
            "approved": False,
        })
        bundle = get_care_plan(result["careplan_id"])["bundle"]
        assert _verifier_displays(bundle) == []
        codes = {
            sec.get("code")
            for e in bundle["entry"]
            for sec in e["resource"].get("meta", {}).get("security", [])
        }
        assert "AIAST" in codes
