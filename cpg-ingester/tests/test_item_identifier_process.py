"""Tests for identifying care processes in a guideline."""

import json
import tempfile
from unittest.mock import MagicMock, patch

from cpg_ingester.nodes.classification_reviewer import classification_reviewer
from cpg_ingester.nodes.item_identifier import _validate_process, item_identifier


def _process(tier="A"):
    return {
        "name": "Home Blood Pressure Monitoring",
        "section": "3.5",
        "pages": [8, 9],
        "trigger_hint": "After treatment initiation",
        "actors": ["patient", "care team"],
        "steps": ["Request readings", "Review readings"],
        "timing_cues": ["weekly"],
        "pattern_family_hint": "schedule-and-check",
        "tier_hint": tier,
        "linked_recommendations": ["Home BP Monitoring"],
        "linked_decisions": ["Treatment Recommendation"],
    }


def _response(process, *, recommendations=None):
    return json.dumps({
        "decisions": [{
            "name": "Treatment Recommendation",
            "section": "3.2",
            "inputs": [{"name": "BP", "type": "number"}],
            "cross_references": [],
        }],
        "recommendations": recommendations or [{
            "title": "Home BP Monitoring",
            "section": "3.5",
            "cross_references": [],
        }],
        "processes": [process],
    })


def test_process_is_validated_and_gets_guid_and_template_id():
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=_response(_process()))
    with tempfile.TemporaryDirectory() as output_dir, patch(
        "cpg_ingester.nodes.item_identifier.get_llm", return_value=llm
    ):
        result = item_identifier({
            "markdown": "Home BP Monitoring",
            "section_map": [{
                "heading": "3.5 Home BP Monitoring", "classification": "recommendation",
                "page_start": 8, "page_end": 9,
            }],
            "output_dir": output_dir,
        })

    process = next(item for item in result["item_manifest"] if item["type"] == "process")
    assert len(process["id"]) == 36
    assert process["template_id"] == "home-blood-pressure-monitoring"
    assert process["linked_recommendations"] == ["Home BP Monitoring"]
    assert process["linked_decisions"] == ["Treatment Recommendation"]


def test_invalid_process_tier_is_reported():
    issues = _validate_process(_process("C"))
    assert any("tier_hint" in issue for issue in issues)


def test_reviewer_feedback_demotes_process_from_a_to_b():
    reviewer_llm = MagicMock()
    reviewer_llm.invoke.return_value = MagicMock(content=json.dumps({
        "issues_found": True,
        "feedback": "Home Blood Pressure Monitoring is Tier A, but the CPG does not specify its timing; demote it to Tier B.",
        "issues": [{
            "item_name": "Home Blood Pressure Monitoring",
            "issue_type": "process_tier_misclassification",
            "description": "Timing is not specified; demote Tier A to Tier B.",
        }],
    }))
    with tempfile.TemporaryDirectory() as output_dir, patch(
        "cpg_ingester.nodes.classification_reviewer.get_llm", return_value=reviewer_llm
    ):
        review = classification_reviewer({
            "item_manifest": [{"type": "process", **_process()}],
            "markdown": "The process is described, but no timing is specified.",
            "output_dir": output_dir,
        })

    creator_llm = MagicMock()
    creator_llm.invoke.return_value = MagicMock(content=_response(_process("B")))
    with tempfile.TemporaryDirectory() as output_dir, patch(
        "cpg_ingester.nodes.item_identifier.get_llm", return_value=creator_llm
    ):
        result = item_identifier({
            "markdown": "The process is described, but no timing is specified.",
            "classification_review_feedback": review["classification_review_feedback"],
            "output_dir": output_dir,
        })

    process = next(item for item in result["item_manifest"] if item["type"] == "process")
    assert process["tier_hint"] == "B"
    assert "demote it to Tier B" in creator_llm.invoke.call_args.args[0][1]["content"]
