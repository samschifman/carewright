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
            {"decision_model_summary": treatment_summary.model_dump(mode="json")},
            {"decision_model_summary": monitoring_summary.model_dump(mode="json")},
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
