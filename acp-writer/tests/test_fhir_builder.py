"""Tests for the deterministic FHIR Bundle builder."""

import json
from datetime import datetime, timezone

from acp_writer.planning_brief import (
    ActivityType,
    ActivityWorkflow,
    DMNAuditEntry,
    FHIRCode,
    PlanActivity,
    PlanGoal,
    PlanningBrief,
    ReviewStatus,
    TargetValue,
)
from acp_writer.planning_brief import (
    ConflictCategory,
    ConflictEntry,
    ConflictSeverity,
    ConflictSource,
    ConflictStatus,
)
from acp_writer.services.ai_transparency import ACP_EXT_BASE
from acp_writer.validators.fhir_bundle_builder import AIAST_SECURITY, build_fhir_bundle


def _hypertension_brief() -> PlanningBrief:
    return PlanningBrief(
        patient_reference="Patient/patient-1",
        applicable_cpgs=["SYN-HTN-2026-001"],
        dmn_audit_trail=[
            DMNAuditEntry(
                model_id="treatment-recommendation",
                model_name="Treatment Recommendation",
                inputs={"Systolic BP": 142},
                outputs={"Action": "Start medication", "Medication": "Lisinopril"},
                timestamp=datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc),
            ),
        ],
        goals=[
            PlanGoal(
                description="Lower blood pressure to target range",
                target_measure_code=FHIRCode(system="http://loinc.org", code="8480-6", display="Systolic BP"),
                target_value=TargetValue(high=140, unit="mmHg"),
                source_recommendation_id="rec-123",
                source_cpg="SYN-HTN-2026-001",
            ),
        ],
        activities=[
            PlanActivity(
                type=ActivityType.MEDICATION,
                description="Start Lisinopril 10mg daily",
                code=FHIRCode(system="http://www.nlm.nih.gov/research/umls/rxnorm", code="29046", display="Lisinopril"),
                dose="10 mg",
                route="oral",
                frequency="daily",
                source_recommendation_id="rec-456",
                source_recommendation_ids=["rec-456", "rec-456-row-2"],
                source_cpg="SYN-HTN-2026-001",
                source_dmn_call=0,
                workflow=ActivityWorkflow(actor="prescribing_physician"),
            ),
            PlanActivity(
                type=ActivityType.MONITORING,
                description="Basic Metabolic Panel",
                code=FHIRCode(system="http://loinc.org", code="51990-0", display="Basic metabolic panel"),
                frequency="4 weeks",
                source_recommendation_id="rec-789",
                source_cpg="SYN-HTN-2026-001",
            ),
            PlanActivity(
                type=ActivityType.LIFESTYLE,
                description="DASH diet - reduce sodium intake",
                source_recommendation_id="rec-abc",
                source_cpg="SYN-HTN-2026-001",
            ),
        ],
        review_status=ReviewStatus.APPROVED,
    )


def _get_resources(bundle: dict, resource_type: str) -> list[dict]:
    return [
        e["resource"]
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == resource_type
    ]


class TestBundleStructure:
    def test_is_transaction_bundle(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        assert bundle["resourceType"] == "Bundle"
        assert bundle["type"] == "transaction"
        assert bundle.get("timestamp") is not None

    def test_all_entries_have_request(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        for entry in bundle["entry"]:
            assert "request" in entry
            assert entry["request"]["method"] == "POST"

    def test_all_entries_have_fullurl(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        for entry in bundle["entry"]:
            assert entry["fullUrl"].startswith("urn:uuid:")

    def test_json_serializable(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        json_str = json.dumps(bundle)
        restored = json.loads(json_str)
        assert restored["resourceType"] == "Bundle"


class TestCarePlan:
    def test_careplan_present(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        careplans = _get_resources(bundle, "CarePlan")
        assert len(careplans) == 1

    def test_careplan_fields(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        cp = _get_resources(bundle, "CarePlan")[0]
        assert cp["status"] == "draft"
        assert cp["intent"] == "proposal"
        assert cp["subject"]["reference"].startswith("urn:uuid:")
        assert len(cp["goal"]) == 1
        assert len(cp["activity"]) == 3


class TestPatientResource:
    def test_patient_included_in_bundle(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        patients = _get_resources(bundle, "Patient")
        assert len(patients) == 1

    def test_patient_is_first_entry(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        first = bundle["entry"][0]["resource"]
        assert first["resourceType"] == "Patient"

    def test_patient_conditional_create_with_demographics(self):
        demographics = {
            "id": "patient-1",
            "reference": "Patient/patient-1",
            "name": "John Doe",
            "gender": "male",
            "birth_date": "1970-01-01",
            "identifiers": [
                {"system": "http://hospital.example/mrn", "value": "MRN-12345"},
            ],
        }
        bundle = build_fhir_bundle(_hypertension_brief(), patient_demographics=demographics)
        patient_entry = bundle["entry"][0]
        patient = patient_entry["resource"]
        assert patient["resourceType"] == "Patient"
        assert patient["identifier"][0]["value"] == "MRN-12345"
        assert patient["gender"] == "male"
        assert patient["birthDate"] == "1970-01-01"
        assert patient_entry["request"]["ifNoneExist"] == "identifier=http://hospital.example/mrn|MRN-12345"

    def test_patient_without_demographics(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        patient_entry = bundle["entry"][0]
        assert "ifNoneExist" not in patient_entry["request"]

    def test_all_subject_refs_use_patient_urn(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        patient_urn = bundle["entry"][0]["fullUrl"]
        for entry in bundle["entry"]:
            resource = entry["resource"]
            subject = resource.get("subject", {})
            if subject:
                assert subject["reference"] == patient_urn


class TestGoals:
    def test_goals_created(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        goals = _get_resources(bundle, "Goal")
        assert len(goals) == 1

    def test_goal_has_target(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        goal = _get_resources(bundle, "Goal")[0]
        assert goal["lifecycleStatus"] == "proposed"
        assert "target" in goal
        target = goal["target"][0]
        assert target["measure"]["coding"][0]["code"] == "8480-6"
        assert target["detailRange"]["high"]["value"] == 140


class TestActivities:
    def test_medication_request(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        meds = _get_resources(bundle, "MedicationRequest")
        assert len(meds) == 1
        assert meds[0]["status"] == "draft"
        assert meds[0]["medicationCodeableConcept"]["coding"][0]["code"] == "29046"
        assert "dosageInstruction" in meds[0]

    def test_service_request_monitoring(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        srs = _get_resources(bundle, "ServiceRequest")
        assert len(srs) == 1
        assert srs[0]["code"]["coding"][0]["code"] == "51990-0"

    def test_lifestyle_inline(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        cp = _get_resources(bundle, "CarePlan")[0]
        inline = [a for a in cp["activity"] if "detail" in a]
        assert len(inline) == 1
        assert "DASH" in inline[0]["detail"]["description"]


CONFLICT_ID_URL = f"{ACP_EXT_BASE}/conflict-id"


def _provenances(bundle: dict) -> list[dict]:
    return _get_resources(bundle, "Provenance")


def _is_conflict_prov(p: dict) -> bool:
    return any("conflict-id" in e.get("url", "") for e in p.get("extension", []))


def _bundle_level_prov(bundle: dict) -> dict:
    """The single aggregate AI-Provenance (carries CPG derivation entities)."""
    provs = [
        p for p in _provenances(bundle)
        if not _is_conflict_prov(p)
        and any(e.get("role") == "derivation" for e in p.get("entity", []))
    ]
    assert len(provs) == 1
    return provs[0]


def _activity_source_provs(bundle: dict) -> list[dict]:
    """Per-activity source provenances (entity role 'source', not conflicts)."""
    activity_refs = {
        f"urn:uuid:{entry['resource']['id']}"
        for entry in bundle.get("entry", [])
        if entry.get("resource", {}).get("resourceType") in {"MedicationRequest", "ServiceRequest"}
    }
    return [
        p for p in _provenances(bundle)
        if not _is_conflict_prov(p)
        and p.get("entity")
        and all(e.get("role") == "source" for e in p["entity"])
        and any(
            target.get("reference") in activity_refs
            or any("targetPath" in str(extension.get("url", "")) for extension in target.get("extension", []))
            for target in p.get("target", [])
        )
    ]


def _goal_source_provs(bundle: dict) -> list[dict]:
    goal_refs = {
        f"urn:uuid:{entry['resource']['id']}"
        for entry in bundle.get("entry", [])
        if entry.get("resource", {}).get("resourceType") == "Goal"
    }
    return [
        p for p in _provenances(bundle)
        if not _is_conflict_prov(p)
        and p.get("entity")
        and all(e.get("role") == "source" for e in p["entity"])
        and any(target.get("reference") in goal_refs for target in p.get("target", []))
    ]


class TestAITransparency:
    def test_aiast_on_all_resources(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        for entry in bundle["entry"]:
            resource = entry["resource"]
            security = resource.get("meta", {}).get("security", [])
            aiast_codes = [s["code"] for s in security if s.get("code") == "AIAST"]
            assert len(aiast_codes) >= 1, f"{resource['resourceType']}/{resource.get('id')} missing AIAST"

    def test_ai_device_present(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        devices = _get_resources(bundle, "Device")
        assert len(devices) == 1
        device = devices[0]
        assert device["type"]["coding"][0]["code"] == "Artificial-Intelligence"
        assert device["meta"]["profile"] == [
            "http://hl7.org/fhir/uv/aitransparency/StructureDefinition/AI-Device"
        ]
        # AIKind extension names the LLM model kind.
        aikind = device["extension"][0]
        assert aikind["url"] == (
            "http://hl7.org/fhir/uv/aitransparency/StructureDefinition/aitransparency.AIKind"
        )
        assert aikind["valueCodeableConcept"]["coding"][0]["code"] == "Large-Language-Models"

    def test_ai_device_records_model_id(self):
        bundle = build_fhir_bundle(_hypertension_brief(), model_id="my-model-x")
        device = _get_resources(bundle, "Device")[0]
        versions = [v["value"] for v in device["version"]]
        assert "my-model-x" in versions

    def test_ai_provenance_present(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        ai_prov = _bundle_level_prov(bundle)
        assert ai_prov["meta"]["profile"] == [
            "http://hl7.org/fhir/uv/aitransparency/StructureDefinition/AI-Provenance"
        ]
        assert ai_prov["reason"][0]["coding"][0]["code"] == "AIAST"
        assert "occurredDateTime" in ai_prov
        # AI agent carries the fixed Artificial-Intelligence role coding.
        role = ai_prov["agent"][0]["role"][0]["coding"][0]
        assert role["code"] == "Artificial-Intelligence"
        assert len(ai_prov["target"]) >= 3

    def test_bundle_provenance_targets_ai_asserted_patient(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        patient = _get_resources(bundle, "Patient")[0]
        patient_ref = f"urn:uuid:{patient['id']}"
        ai_prov = _bundle_level_prov(bundle)
        assert patient_ref in {item["reference"] for item in ai_prov["target"]}

    def test_all_provenances_conform(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        for p in _provenances(bundle):
            assert p["meta"]["profile"] == [
                "http://hl7.org/fhir/uv/aitransparency/StructureDefinition/AI-Provenance"
            ]
            assert p["reason"][0]["coding"][0]["code"] == "AIAST"
            assert "occurredDateTime" in p

    def test_per_activity_provenance(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        activity_provs = _activity_source_provs(bundle)
        assert len(activity_provs) == 4
        for prov in activity_provs:
            entities = prov.get("entity", [])
            assert len(entities) >= 1
            assert entities[0]["role"] == "source"

    def test_inline_activity_provenance_uses_target_path(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        activity_provs = _activity_source_provs(bundle)
        inline_provs = [
            p for p in activity_provs
            if any("targetPath" in str(ext.get("url", "")) for t in p.get("target", []) for ext in t.get("extension", []))
        ]
        assert len(inline_provs) == 1
        target = inline_provs[0]["target"][0]
        ext = target["extension"][0]
        assert ext["url"] == "http://hl7.org/fhir/StructureDefinition/targetPath"
        assert "CarePlan.activity[" in ext["valueString"]
        assert ".detail" in ext["valueString"]

    def test_standalone_activity_provenance_no_target_path(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        activity_provs = _activity_source_provs(bundle)
        standalone_provs = [
            p for p in activity_provs
            if not any(t.get("extension") for t in p.get("target", []))
        ]
        assert len(standalone_provs) == 3
        for prov in standalone_provs:
            assert prov["target"][0]["reference"].startswith("urn:uuid:")

    def test_provenance_has_cpg_source(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        ai_prov = _bundle_level_prov(bundle)
        cpg_entities = [
            e for e in ai_prov["entity"]
            if e["role"] == "derivation" and "display" in e.get("what", {})
        ]
        assert len(cpg_entities) == 1
        assert "SYN-HTN-2026-001" in cpg_entities[0]["what"]["display"]


class TestInputPromptCapture:
    def test_prompts_become_docrefs(self):
        bundle = build_fhir_bundle(
            _hypertension_brief(),
            prompts={"plan_composer": "Compose a plan", "conflict_analyst": "Find conflicts"},
        )
        docrefs = _get_resources(bundle, "DocumentReference")
        input_prompts = [
            d for d in docrefs
            if d["type"]["coding"][0]["code"] == "AIInputPrompt"
        ]
        assert len(input_prompts) == 2
        for d in input_prompts:
            assert d["status"] == "current"
            assert d["content"][0]["attachment"]["contentType"] == "text/markdown"

    def test_prompt_base64_roundtrips(self):
        import base64

        bundle = build_fhir_bundle(
            _hypertension_brief(), prompts={"plan_composer": "Hello prompt"}
        )
        docref = [
            d for d in _get_resources(bundle, "DocumentReference")
            if d["type"]["coding"][0]["code"] == "AIInputPrompt"
        ][0]
        data = docref["content"][0]["attachment"]["data"]
        assert base64.b64decode(data).decode("utf-8") == "Hello prompt"

    def test_input_prompt_referenced_as_entity(self):
        bundle = build_fhir_bundle(
            _hypertension_brief(), prompts={"plan_composer": "Compose"}
        )
        docref = [
            d for d in _get_resources(bundle, "DocumentReference")
            if d["type"]["coding"][0]["code"] == "AIInputPrompt"
        ][0]
        ai_prov = _bundle_level_prov(bundle)
        refs = [e["what"].get("reference") for e in ai_prov["entity"]]
        assert f"urn:uuid:{docref['id']}" in refs

    def test_input_prompt_is_provenance_target(self):
        bundle = build_fhir_bundle(
            _hypertension_brief(), prompts={"plan_composer": "Compose"}
        )
        docref = [
            d for d in _get_resources(bundle, "DocumentReference")
            if d["type"]["coding"][0]["code"] == "AIInputPrompt"
        ][0]
        ai_prov = _bundle_level_prov(bundle)
        assert f"urn:uuid:{docref['id']}" in {
            item["reference"] for item in ai_prov["target"]
        }

    def test_capture_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("ACP_CAPTURE_PROMPTS", "false")
        bundle = build_fhir_bundle(
            _hypertension_brief(), prompts={"plan_composer": "Compose"}
        )
        docrefs = _get_resources(bundle, "DocumentReference")
        assert docrefs == []


class TestModelCard:
    def test_absent_by_default(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        model_cards = [
            d for d in _get_resources(bundle, "DocumentReference")
            if d["type"]["coding"][0]["code"] == "AIModelCard"
        ]
        assert model_cards == []

    def test_present_when_env_set(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_CARD_URL", "https://example.org/card.html")
        bundle = build_fhir_bundle(_hypertension_brief())
        model_cards = [
            d for d in _get_resources(bundle, "DocumentReference")
            if d["type"]["coding"][0]["code"] == "AIModelCard"
        ]
        assert len(model_cards) == 1
        assert model_cards[0]["content"][0]["attachment"]["url"] == "https://example.org/card.html"


def _brief_with_conflict() -> PlanningBrief:
    brief = _hypertension_brief()
    # A second goal so goal_indices=[0,1] is valid for a divergent-target conflict.
    brief.goals.append(
        PlanGoal(
            description="Lower blood pressure to tighter target",
            target_measure_code=FHIRCode(system="http://loinc.org", code="8480-6", display="Systolic BP"),
            target_value=TargetValue(high=130, unit="mmHg"),
            source_recommendation_id="dm2-rec-002",
            source_cpg="SYN-DM2-2026-001",
        )
    )
    brief.conflicts = [
        ConflictEntry(
            id="conf-abc12345",
            category=ConflictCategory.DIVERGENT_TARGET,
            severity=ConflictSeverity.WARNING,
            status=ConflictStatus.DETECTED,
            description="Two guidelines set different BP targets",
            rationale="HTN CPG targets <140/90; DM2 CPG targets <130/80",
            confidence="high",
            goal_indices=[0, 1],
            activity_indices=[0],  # the MedicationRequest (standalone)
            sources=[
                ConflictSource(cpg_id="SYN-HTN-2026-001", recommendation_id="rec-123", excerpt="<140/90"),
                ConflictSource(cpg_id="SYN-DM2-2026-001", recommendation_id="dm2-rec-002", excerpt="<130/80"),
            ],
        )
    ]
    return brief


class TestConflictProvenance:
    def test_marker_absent_without_conflicts(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        cp = _get_resources(bundle, "CarePlan")[0]
        exts = cp.get("extension", [])
        assert not any("careplan-conflict-detected" in e.get("url", "") for e in exts)

    def test_marker_present_with_conflicts(self):
        bundle = build_fhir_bundle(_brief_with_conflict())
        cp = _get_resources(bundle, "CarePlan")[0]
        markers = [e for e in cp.get("extension", []) if "careplan-conflict-detected" in e["url"]]
        assert len(markers) == 1
        assert markers[0]["valueBoolean"] is True

    def test_one_provenance_per_conflict(self):
        bundle = build_fhir_bundle(_brief_with_conflict())
        conflict_provs = [p for p in _provenances(bundle) if _is_conflict_prov(p)]
        assert len(conflict_provs) == 1

    def test_conflict_extensions(self):
        bundle = build_fhir_bundle(_brief_with_conflict())
        prov = [p for p in _provenances(bundle) if _is_conflict_prov(p)][0]
        ext = {e["url"]: e for e in prov["extension"]}
        assert ext[f"{ACP_EXT_BASE}/conflict-id"]["valueString"] == "conf-abc12345"
        assert ext[f"{ACP_EXT_BASE}/conflict-severity"]["valueCode"] == "warning"
        assert ext[f"{ACP_EXT_BASE}/conflict-category"]["valueCode"] == "divergent_target"
        assert ext[f"{ACP_EXT_BASE}/conflict-status"]["valueCode"] == "detected"
        assert "Two guidelines" in ext[f"{ACP_EXT_BASE}/conflict-description"]["valueString"]
        # AIconfidence uses the certainty-rating value set.
        conf = ext["http://hl7.org/fhir/uv/aitransparency/StructureDefinition/AIconfidence"]
        assert conf["valueCodeableConcept"]["coding"][0]["code"] == "high"

    def test_activity_source_provenances_carry_no_aiconfidence(self):
        # C7: AIconfidence is a conflict-Provenance feature only. Per-activity
        # source Provenances link to the source recommendation but carry no
        # confidence rating — the docs must not claim otherwise.
        aiconf_url = "http://hl7.org/fhir/uv/aitransparency/StructureDefinition/AIconfidence"
        bundle = build_fhir_bundle(_brief_with_conflict())
        source_provs = _activity_source_provs(bundle)
        assert source_provs  # the med activity has a source_recommendation_id
        for p in source_provs:
            assert all(e.get("url") != aiconf_url for e in p.get("extension", []))

    def test_merged_activity_preserves_each_recommendation_in_provenance(self):
        bundle = build_fhir_bundle(_hypertension_brief())
        recommendation_ids = {
            entity.get("what", {}).get("identifier", {}).get("value")
            for provenance in _activity_source_provs(bundle)
            for entity in provenance.get("entity", [])
        }
        assert {"rec-456", "rec-456-row-2"} <= recommendation_ids

    def test_merged_goal_preserves_each_recommendation_in_provenance(self):
        brief = _hypertension_brief()
        brief.goals[0].source_recommendation_ids = ["goal-rec-1", "goal-rec-2"]
        bundle = build_fhir_bundle(brief)
        recommendation_ids = {
            entity.get("what", {}).get("identifier", {}).get("value")
            for provenance in _goal_source_provs(bundle)
            for entity in provenance.get("entity", [])
        }
        assert {"goal-rec-1", "goal-rec-2"} <= recommendation_ids

    def test_conflict_targets_resolve(self):
        bundle = build_fhir_bundle(_brief_with_conflict())
        prov = [p for p in _provenances(bundle) if _is_conflict_prov(p)][0]
        goals = _get_resources(bundle, "Goal")
        meds = _get_resources(bundle, "MedicationRequest")
        goal_urns = {f"urn:uuid:{g['id']}" for g in goals}
        med_urns = {f"urn:uuid:{m['id']}" for m in meds}
        target_refs = {t["reference"] for t in prov["target"]}
        # Both goals + the medication are targeted directly.
        assert goal_urns <= target_refs
        assert med_urns & target_refs

    def test_conflict_rationale_in_note(self):
        bundle = build_fhir_bundle(_brief_with_conflict())
        prov = [p for p in _provenances(bundle) if _is_conflict_prov(p)][0]
        assert "HTN CPG targets" in prov["note"][0]["text"]
        assert "authorReference" in prov["note"][0]

    def test_conflict_sources_as_entities(self):
        bundle = build_fhir_bundle(_brief_with_conflict())
        prov = [p for p in _provenances(bundle) if _is_conflict_prov(p)][0]
        source_entities = [e for e in prov["entity"] if e["role"] == "source"]
        assert len(source_entities) == 2
        idents = {e["what"]["identifier"]["value"] for e in source_entities}
        assert idents == {"rec-123", "dm2-rec-002"}


class TestNodeIntegration:
    def test_fhir_bundle_generator_node(self):
        from acp_writer.nodes.fhir_bundle_generator import fhir_bundle_generator

        brief = _hypertension_brief()
        state = {"planning_brief": brief.model_dump(mode="json")}
        result = fhir_bundle_generator(state)

        assert result["fhir_bundle"]["type"] == "transaction"
        assert len(result["fhir_bundle"]["entry"]) > 0

    def test_empty_brief(self):
        from acp_writer.nodes.fhir_bundle_generator import fhir_bundle_generator

        result = fhir_bundle_generator({"planning_brief": {}})
        assert result["fhir_bundle"]["entry"] == []

    def test_writes_artifact(self, tmp_path):
        from acp_writer.nodes.fhir_bundle_generator import fhir_bundle_generator

        brief = _hypertension_brief()
        state = {
            "planning_brief": brief.model_dump(mode="json"),
            "output_dir": str(tmp_path),
        }
        fhir_bundle_generator(state)
        assert (tmp_path / "fhir-bundle.json").exists()

    def test_loose_conflict_does_not_empty_bundle(self):
        # F4: one malformed conflict must not sink the whole brief. Before the
        # PlanningBrief before-validator, validate raised and the goals/
        # activities were silently dropped into an empty bundle.
        from acp_writer.nodes.fhir_bundle_generator import fhir_bundle_generator

        brief = _hypertension_brief().model_dump(mode="json")
        brief["conflicts"] = [
            {"description": "loose", "severity": "high", "sources": ["SYN-HTN-2026-001"]}
        ]
        result = fhir_bundle_generator({"planning_brief": brief})
        assert len(result["fhir_bundle"]["entry"]) > 0
        assert "fhir_generation_error" not in result

    def test_invalid_brief_surfaces_error(self):
        # F4: a genuinely invalid brief (bad goals/activities) must NOT report an
        # empty bundle as success — it surfaces fhir_generation_error.
        from acp_writer.nodes.fhir_bundle_generator import fhir_bundle_generator

        bad = {
            "patient_reference": "Patient/1",
            "applicable_cpgs": ["CPG-A"],
            "goals": [{"description": "g"}],  # missing required source_cpg
            "activities": [],
        }
        result = fhir_bundle_generator({"planning_brief": bad})
        assert result["fhir_bundle"]["entry"] == []
        assert result["fhir_generation_error"]
