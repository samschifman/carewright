"""Tests for resolving automation template links during assembly."""

import json
from pathlib import Path

from cpg_contracts import DecisionModelSummary, DecisionVariable
from cpg_ingester.nodes.assembly import assembly

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden" / "automation"


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
