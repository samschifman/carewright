"""Tests for resolving automation template links during assembly."""

import json
from pathlib import Path

import pytest
from cpg_contracts import DecisionModelSummary, DecisionVariable
from cpg_contracts.automation.ir import ProcessIR, ir_version
from fastapi.testclient import TestClient
from cpg_ingester.nodes.assembly import assembly
from cpg_ingester.services import assembly_svc

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden" / "automation"
ASTRA_RUN_DIR = (
    Path(__file__).resolve().parents[2]
    / "working"
    / "slice-run"
    / "ingester"
    / "20261014T161107Z-93281"
)


def _generated_template(item):
    template = json.loads(
        (GOLDEN_DIR / "home-bp-monitoring.template.json").read_text()
    )
    return {
        "item": item,
        "process_ir": template["ir"],
        "bpmn_xml": template["bpmn_xml"],
        "template_summary": template["summary"],
        "static_rungs_passed": ["L0", "L1", "L2", "L3", "L4", "L5a", "L5b"],
        "deferred_invariants": [],
    }


def _state(tmp_path, *, linked_recommendations=None, bpmn_results=None):
    recommendation = {
        "id": "rec-home-bp",
        "title": "Home blood pressure monitoring",
        "source_cpg": "SYN-HTN-2026-002",
    }
    return {
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "item_manifest": [{
            "id": "rec-home-bp",
            "type": "recommendation",
            "name": "Home blood pressure monitoring",
        }],
        "recommendation_results": [recommendation],
        "bpmn_results": bpmn_results or [_generated_template({
            "id": "process-guid",
            "template_id": "home-bp-monitoring",
            "name": "Home blood pressure monitoring with outreach and escalation",
            "section": "3.5 Home Blood Pressure Monitoring",
            "linked_recommendations": (
                ["Home blood pressure monitoring"]
                if linked_recommendations is None else linked_recommendations
            ),
            "linked_decisions": [],
        })],
        "output_dir": str(tmp_path),
    }


def test_resolves_recommendation_name_in_trigger_and_publishes_normalized_template(tmp_path):
    result = assembly(_state(tmp_path))
    template_result = result["bpmn_results"][0]
    template = template_result["automation_template"]

    assert template["summary"]["linked_recommendation_ids"] == ["rec-home-bp"]
    assert template["summary"]["triggers"][0]["recommendation_id"] == "rec-home-bp"
    assert template["summary"]["version"] != "499a6a2c1b81"
    assert template_result["item"]["template_id"] == "home-bp-monitoring"
    assert result["recommendation_results"][0]["automation_template_ids"] == [
        "home-bp-monitoring"
    ]
    assert (tmp_path / "bpmn/home-bp-monitoring.template.json").exists()
    assert (tmp_path / "bpmn/home-bp-monitoring.bpmn").exists()


def test_pre_delivery_parameter_edit_rebinds_recompiles_and_versions_template(tmp_path):
    state = _state(tmp_path)
    state["artifact_review"] = {
        "action": "approve",
        "parameterEdits": [{
            "templateId": "home-bp-monitoring",
            "parameter": "max_reminder_attempts",
            "value": 3,
            "note": "Reviewer selected the bounded value.",
        }],
    }
    original_version = state["bpmn_results"][0]["template_summary"]["version"]

    result = assembly(state)
    template_result = result["bpmn_results"][0]
    template = template_result["automation_template"]
    edited = next(
        parameter for parameter in template["ir"]["process"]["acp"]["parameters"]
        if parameter["name"] == "max_reminder_attempts"
    )

    assert edited["default"] == 3
    assert edited["source"]["kind"] == "reviewer"
    assert edited["source"]["previous"]["kind"] == "cpg"
    assert edited["source"]["note"] == "Reviewer selected the bounded value."
    assert template["summary"]["version"] == ir_version(
        ProcessIR.model_validate(template["ir"])
    )
    assert template["summary"]["version"] != original_version
    assert "Reviewer selected the bounded value." in template["bpmn_xml"]
    assert template["summary"]["validation"]["status"] == "valid"


def test_l7_acknowledgement_makes_template_deliverable_but_mechanical_escalation_stays_blocked(tmp_path):
    state = _state(tmp_path)
    state["bpmn_results"][0].update({
        "escalated": True,
        "escalation_reason": "semantic-budget-exhausted",
        "escalation_errors": ["Review was inconclusive."],
    })
    state["bpmn_results"][0]["template_summary"]["validation"].update({
        "status": "escalated",
        "errors": ["Review was inconclusive."],
    })
    state["artifact_review"] = {
        "action": "approve",
        "acknowledgements": [{
            "templateId": "home-bp-monitoring",
            "note": "The care team accepted the remaining review note.",
        }],
    }

    result = assembly(state)
    template_result = result["bpmn_results"][0]

    assert template_result.get("escalated") is not True
    assert template_result["automation_template"]["summary"]["validation"]["status"] == "escalated"
    assert template_result["automation_template"]["summary"]["validation"]["acknowledgement"] == (
        "The care team accepted the remaining review note."
    )
    assert (tmp_path / "bpmn/home-bp-monitoring.template.json").exists()

    blocked = _state(tmp_path / "mechanical")
    blocked["bpmn_results"][0].update({
        "escalated": True,
        "escalation_reason": "static-budget-exhausted",
        "escalation_errors": ["Static validation failed."],
    })
    blocked["bpmn_results"][0]["template_summary"]["validation"].update({
        "status": "escalated",
        "errors": ["Static validation failed."],
    })
    blocked["artifact_review"] = state["artifact_review"]
    blocked_result = assembly(blocked)["bpmn_results"][0]
    assert blocked_result["escalated"] is True
    assert "automation_template" not in blocked_result


def test_assembly_service_applies_review_edits_from_request(monkeypatch, tmp_path):
    template_result = _generated_template({
        "id": "process-guid",
        "template_id": "home-bp-monitoring",
        "name": "Home blood pressure monitoring with outreach and escalation",
        "section": "3.5 Home Blood Pressure Monitoring",
        "linked_recommendations": ["Home blood pressure monitoring"],
        "linked_decisions": [],
    })
    monkeypatch.setattr(assembly_svc, "_store", None)

    with TestClient(assembly_svc.app) as client:
        response = client.post("/api/v1/assemble", json={
            "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
            "item_manifest": [{
                "id": "rec-home-bp", "type": "recommendation",
                "name": "Home blood pressure monitoring",
            }],
            "recommendation_results": [{
                "id": "rec-home-bp", "title": "Home blood pressure monitoring",
                "source_cpg": "SYN-HTN-2026-002",
            }],
            "bpmn_results": [template_result],
            "artifact_review": {
                "action": "approve",
                "parameterEdits": [{
                    "templateId": "home-bp-monitoring",
                    "parameter": "max_reminder_attempts",
                    "value": 3,
                    "note": "Reviewer selected the bounded value.",
                }],
            },
        })

    assert response.status_code == 200
    edited = next(
        parameter
        for parameter in response.json()["bpmn_results"][0]["automation_template"]["ir"][
            "process"]["acp"]["parameters"]
        if parameter["name"] == "max_reminder_attempts"
    )
    assert edited["source"]["kind"] == "reviewer"


def test_parameter_edit_runs_optional_engine_preflight(monkeypatch, tmp_path):
    calls = []

    class Response:
        status_code = 200
        ok = True

        @staticmethod
        def json():
            return {"valid": True, "messages": []}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setenv("BPMN_PREFLIGHT_URL", "http://engine.test/api/v1/automation/templates")
    monkeypatch.setattr("cpg_ingester.nodes.assembly.requests.post", post)
    state = _state(tmp_path)
    state["artifact_review"] = {
        "parameterEdits": [{
            "templateId": "home-bp-monitoring",
            "parameter": "max_reminder_attempts",
            "value": 3,
            "note": "Reviewer selected the bounded value.",
        }],
    }

    result = assembly(state)

    assert len(calls) == 1
    assert "validate_only=true" in calls[0][0]
    assert "source_cpg=SYN-HTN-2026-002" in calls[0][0]
    assert next(
        item for item in calls[0][1]["json"]["ir"]["process"]["acp"]["parameters"]
        if item["name"] == "max_reminder_attempts"
    )["default"] == 3
    validation = result["bpmn_results"][0]["template_summary"]["validation"]
    assert validation["kogito_checked"] is True


def _state_with_saved_run_recommendation_names(tmp_path, linked_recommendations):
    recommendation_id = "f3672ae9-2bf5-4e41-9ebe-838b09adc3f8"
    state = _state(tmp_path, linked_recommendations=linked_recommendations)
    state["item_manifest"] = [{
        "id": recommendation_id,
        "type": "recommendation",
        "title": "Structured Home BP Measurement and Weekly Review",
    }]
    state["recommendation_results"] = [{
        "id": recommendation_id,
        "title": "Structured Home Blood Pressure Measurement and Weekly Review",
        "source_cpg": "SYN-HTN-2026-002",
    }]
    return state


def test_manifest_recommendation_title_resolves_abbreviation_expansion_from_saved_run(tmp_path):
    result = assembly(_state_with_saved_run_recommendation_names(
        tmp_path, ["Structured Home BP Measurement and Weekly Review"]
    ))

    template_result = result["bpmn_results"][0]
    assert template_result.get("escalated", False) is False
    assert template_result["automation_template"]["summary"][
        "linked_recommendation_ids"
    ] == ["f3672ae9-2bf5-4e41-9ebe-838b09adc3f8"]


def test_unmatched_manifest_recommendation_name_keeps_unresolved_message(tmp_path):
    result = assembly(_state_with_saved_run_recommendation_names(
        tmp_path, ["No matching recommendation title"]
    ))

    template_result = result["bpmn_results"][0]
    assert template_result["escalated"] is True
    assert template_result["escalation_reason"] == "unresolved-recommendation-link"
    assert template_result["escalation_errors"] == [
        "Could not resolve linked recommendation(s): No matching recommendation title"
    ]


def test_template_without_recommendation_link_is_escalated(tmp_path):
    result = assembly(_state(tmp_path, linked_recommendations=[]))

    assert result["bpmn_results"][0]["escalated"] is True
    assert result["bpmn_results"][0]["escalation_reason"] == "no-recommendation-link"
    assert "automation_template" not in result["bpmn_results"][0]
    assert result["escalated_items"][0]["type"] == "automation"
    assert result["recommendation_results"][0]["automation_template_ids"] is None
    assert not (tmp_path / "bpmn/home-bp-monitoring.template.json").exists()


def test_duplicate_template_ids_are_escalated(tmp_path):
    item = {
        "id": "process-guid",
        "template_id": "home-bp-monitoring",
        "name": "Home blood pressure monitoring with outreach and escalation",
        "section": "3.5 Home Blood Pressure Monitoring",
        "linked_recommendations": ["Home blood pressure monitoring"],
        "linked_decisions": [],
    }
    results = [_generated_template(item), _generated_template(item)]
    result = assembly(_state(tmp_path, bpmn_results=results))

    assert all(entry["escalated"] for entry in result["bpmn_results"])
    assert all(entry["escalation_reason"] == "duplicate-template-id"
               for entry in result["bpmn_results"])
    assert result["assembly_report"]["automation_templates_count"] == 0


def test_resolves_linked_decision_names_to_model_ids(tmp_path):
    item = {
        "id": "process-acei",
        "template_id": "acei-lab-follow-up",
        "name": "ACE inhibitor laboratory follow-up",
        "section": "3.3 Monitoring Plan",
        "linked_recommendations": ["ACE inhibitor laboratory follow-up"],
        "linked_decisions": ["Treatment Recommendation", "Monitoring Plan"],
    }
    golden = json.loads(
        (GOLDEN_DIR / "acei-lab-follow-up.template.json").read_text()
    )
    recommendation = {
        "id": "rec-acei",
        "title": "ACE inhibitor laboratory follow-up",
    }
    treatment_summary = DecisionModelSummary(
        id="treatment-recommendation",
        name="Treatment Recommendation",
        inputs=[
            DecisionVariable(name="Systolic BP", type="number"),
            DecisionVariable(name="Has Diabetes", type="boolean"),
            DecisionVariable(name="Has Kidney Disease", type="boolean"),
        ],
        outputs=[
            DecisionVariable(name="Action", type="string"),
            DecisionVariable(name="Medication", type="string"),
            DecisionVariable(name="Dose", type="string"),
            DecisionVariable(name="Follow Up Weeks", type="number"),
        ],
        namespace="https://example.org/treatment-recommendation",
    )
    monitoring_summary = DecisionModelSummary(
        id="monitoring-plan",
        name="Monitoring Plan",
        inputs=[
            DecisionVariable(name="Treatment Action", type="string"),
            DecisionVariable(name="Has Kidney Disease", type="boolean"),
        ],
        outputs=[
            DecisionVariable(name="Lab Order", type="string"),
            DecisionVariable(name="Lab Timing Weeks", type="number"),
        ],
        namespace="https://example.org/monitoring-plan",
    )
    result = assembly({
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "item_manifest": [
            {"id": "rec-acei", "type": "recommendation",
             "name": "ACE inhibitor laboratory follow-up"},
            {"id": "decision-treatment", "type": "decision",
             "model_id": "treatment-recommendation",
             "name": "Treatment Recommendation"},
            {"id": "decision-monitoring", "type": "decision",
             "model_id": "monitoring-plan", "name": "Monitoring Plan"},
        ],
        "recommendation_results": [recommendation],
        "dmn_results": [
            {"dmn_xml": "<definitions/>",
             "decision_model_summary": treatment_summary.model_dump(mode="json")},
            {"dmn_xml": "<definitions/>",
             "decision_model_summary": monitoring_summary.model_dump(mode="json")},
        ],
        "bpmn_results": [{
            "item": item,
            "process_ir": golden["ir"],
            "bpmn_xml": golden["bpmn_xml"],
            "template_summary": golden["summary"],
        }],
        "output_dir": str(tmp_path),
    })

    normalized = result["bpmn_results"][0]["automation_template"]
    assert normalized["summary"]["linked_decision_model_ids"] == [
        "monitoring-plan", "treatment-recommendation"
    ]
    assert normalized["summary"]["triggers"][0]["model_id"] == "treatment-recommendation"


def _acei_assembly_state(tmp_path, *, dmn_results, manifest_decision=True):
    golden = json.loads(
        (GOLDEN_DIR / "acei-lab-follow-up.template.json").read_text()
    )
    manifest = [{
        "id": "rec-acei",
        "type": "recommendation",
        "name": "ACE inhibitor laboratory follow-up",
    }]
    if manifest_decision:
        manifest.append({
            "id": "decision-monitoring",
            "type": "decision",
            "model_id": "monitoring-plan",
            "name": "Monitoring Plan",
        })
    return {
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "item_manifest": manifest,
        "recommendation_results": [{
            "id": "rec-acei",
            "title": "ACE inhibitor laboratory follow-up",
            "source_cpg": "SYN-HTN-2026-002",
        }],
        "dmn_results": dmn_results,
        "bpmn_results": [{
            "item": {
                "id": "process-acei",
                "template_id": "acei-lab-follow-up",
                "name": "ACE inhibitor laboratory follow-up",
                "section": "3.3 Monitoring Plan",
                "linked_recommendations": ["ACE inhibitor laboratory follow-up"],
                "linked_decisions": ["Monitoring Plan"],
            },
            "process_ir": golden["ir"],
            "bpmn_xml": golden["bpmn_xml"],
            "template_summary": golden["summary"],
        }],
        "output_dir": str(tmp_path),
    }


def test_business_rule_task_referencing_escalated_dmn_escalates_as_undelivered(tmp_path):
    result = assembly(_acei_assembly_state(
        tmp_path,
        dmn_results=[{
            "dmn_xml": "<definitions/>",
            "escalated": True,
            "decision_model_summary": {
                "id": "monitoring-plan",
                "name": "Monitoring Plan",
            },
        }],
    ))

    template_result = result["bpmn_results"][0]
    assert template_result["escalated"] is True
    assert template_result["escalation_reason"] == "undelivered-decision-reference"
    message = template_result["escalation_errors"][0]
    assert "run_monitoring_plan" in message
    assert "Monitoring Plan" in message


def test_business_rule_task_referencing_missing_dmn_escalates_as_undelivered(tmp_path):
    result = assembly(_acei_assembly_state(tmp_path, dmn_results=[]))

    template_result = result["bpmn_results"][0]
    assert template_result["escalated"] is True
    assert template_result["escalation_reason"] == "undelivered-decision-reference"
    message = template_result["escalation_errors"][0]
    assert "run_monitoring_plan" in message
    assert "Monitoring Plan" in message


def test_undelivered_decision_escalation_clears_when_decision_is_delivered(tmp_path):
    state = _acei_assembly_state(
        tmp_path,
        dmn_results=[
            {
                "dmn_xml": "<definitions/>",
                "decision_model_summary": {
                    "id": "monitoring-plan", "name": "Monitoring Plan",
                    "namespace": "https://example.org/monitoring-plan",
                    "inputs": [
                        {"name": "Treatment Action", "type": "string"},
                        {"name": "Has Kidney Disease", "type": "boolean"},
                    ],
                    "outputs": [
                        {"name": "Lab Order", "type": "string"},
                        {"name": "Lab Timing Weeks", "type": "number"},
                    ],
                },
            },
            {
                "dmn_xml": "<definitions/>",
                "decision_model_summary": {
                    "id": "treatment-recommendation", "name": "Treatment Recommendation",
                    "namespace": "https://example.org/treatment-recommendation",
                    "inputs": [
                        {"name": "Systolic BP", "type": "number"},
                        {"name": "Has Diabetes", "type": "boolean"},
                        {"name": "Has Kidney Disease", "type": "boolean"},
                    ],
                    "outputs": [
                        {"name": "Action", "type": "string"},
                        {"name": "Medication", "type": "string"},
                        {"name": "Dose", "type": "string"},
                        {"name": "Follow Up Weeks", "type": "number"},
                    ],
                },
            },
        ],
    )
    state["item_manifest"].append({
        "id": "decision-treatment", "type": "decision",
        "model_id": "treatment-recommendation", "name": "Treatment Recommendation",
    })
    state["bpmn_results"][0].update({
        "escalated": True,
        "escalation_reason": "undelivered-decision-reference",
        "escalation_errors": ["previous run did not deliver the linked decision"],
    })

    result = assembly(state)

    template_result = result["bpmn_results"][0]
    assert template_result.get("escalated") is not True, template_result.get("escalation_errors")
    assert template_result["template_summary"]["validation"]["status"] == "valid"
    assert template_result["automation_template"]["ir"]["process"]["id"] == "acei-lab-follow-up"


def test_existing_linked_template_is_revalidated_after_decision_delivery(tmp_path):
    result = assembly(_acei_assembly_state(
        tmp_path,
        dmn_results=[
            {
                "dmn_xml": "<definitions/>",
                "decision_model_summary": {
                    "id": "monitoring-plan", "name": "Monitoring Plan",
                    "namespace": "https://example.org/monitoring-plan",
                    "inputs": [], "outputs": [],
                },
            },
            {
                "dmn_xml": "<definitions/>",
                "decision_model_summary": {
                    "id": "treatment-recommendation", "name": "Treatment Recommendation",
                    "namespace": "https://example.org/treatment-recommendation",
                    "inputs": [], "outputs": [],
                },
            },
        ],
    ))

    template_result = result["bpmn_results"][0]

    assert template_result.get("escalated") is True
    assert template_result["escalation_reason"] == "invalid-template"
    assert any("Lab Order" in message for message in template_result["escalation_errors"])


def test_escalated_linked_decision_without_business_rule_task_does_not_block_template(tmp_path):
    state = _state(tmp_path)
    state["item_manifest"].append({
        "id": "decision-weekly-home-bp",
        "type": "decision",
        "model_id": "weekly-home-bp-interpretation-and-notification",
        "name": "Weekly Home BP Interpretation and Notification",
    })
    state["dmn_results"] = [{
        "dmn_xml": "<definitions/>",
        "escalated": True,
        "decision_model_summary": {
            "id": "weekly-home-bp-interpretation-and-notification",
            "name": "Weekly Home BP Interpretation and Notification",
            "inputs": [],
            "outputs": [],
        },
    }]
    state["bpmn_results"][0]["item"]["linked_decisions"] = [
        "Weekly Home BP Interpretation and Notification"
    ]

    result = assembly(state)

    template_result = result["bpmn_results"][0]
    assert template_result.get("escalated") is not True
    assert template_result["automation_template"]["summary"][
        "linked_decision_model_ids"
    ] == ["weekly-home-bp-interpretation-and-notification"]


def test_saved_astra_replay_escalates_enum_values_outside_linked_decision_outputs(tmp_path):
    generation_path = ASTRA_RUN_DIR / "generation-result.json"
    if not generation_path.exists():
        pytest.skip("saved Astra replay artifacts are not present")

    generation = json.loads(generation_path.read_text())
    analysis = json.loads((ASTRA_RUN_DIR / "analysis-result.json").read_text())
    manifest = json.loads((ASTRA_RUN_DIR / "manifest.json").read_text())
    assembled = assembly({
        **analysis,
        **generation,
        "item_manifest": manifest,
        "output_dir": str(tmp_path),
    })

    source_template = next(
        result for result in generation["bpmn_results"]
        if any(
            (parameter.get("constraints") or {}).get("enum")
            for parameter in result.get("process_ir", {}).get("process", {})
            .get("acp", {}).get("parameters", [])
        )
        and any(
            element.get("type") == "businessRuleTask"
            for element in result.get("process_ir", {}).get("process", {})
            .get("flowElements", [])
        )
    )
    template_id = source_template["template_summary"]["id"]
    assembled_template = next(
        result for result in assembled["bpmn_results"]
        if result.get("template_summary", {}).get("id") == template_id
    )

    assert assembled_template["escalation_reason"] == "decision-output-mismatch"
    enum_parameter = next(
        parameter for parameter in source_template["process_ir"]["process"]
        ["acp"]["parameters"]
        if (parameter.get("constraints") or {}).get("enum")
    )
    message = " ".join(assembled_template["escalation_errors"])
    assert enum_parameter["name"] in message
    assert "enum values" in message and "output values" in message
