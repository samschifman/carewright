"""Prompt contract tests for process IR creation."""

import json
from pathlib import Path

from cpg_ingester.prompts.bpmn_ir_creator import build_bpmn_ir_creator_system


def test_prompt_includes_catalog_and_forbids_xml_and_coordinates():
    prompt = build_bpmn_ir_creator_system()
    flat_prompt = " ".join(prompt.split())

    assert "patient.request_observation" in prompt
    assert "ehr.query_observations" in prompt
    assert "Never write BPMN XML" in prompt
    assert "never write coordinates" in prompt
    assert "max_duration" in prompt
    assert "ProcessIR" in prompt
    assert "Every flow node has a short, human-readable name" in flat_prompt
    assert "Every node with multiple incoming sequence flows" in flat_prompt
    assert "set `constraints.enum` to the complete finite set" in flat_prompt
    assert "The patient's own actions are not process steps" in flat_prompt
    assert "Use `userTask` only for clinician" in flat_prompt
    assert "care team's automation expressed with catalog capabilities" in flat_prompt
    assert "Never use a `property` reference for a timer" in flat_prompt
    assert "express one week as `P7D`, never `P1W`" in flat_prompt
    assert "one exact, contiguous quote" in flat_prompt
    assert "An `acp.triggers` entry of kind `dmn-output`" in flat_prompt


def test_prompt_renders_follow_up_golden_from_file_and_not_home_bp():
    prompt = build_bpmn_ir_creator_system()
    example_path = (
        Path(__file__).parents[1]
        / "data/golden/automation/follow-up-visit.ir.json"
    )
    example = json.loads(example_path.read_text(encoding="utf-8"))
    rendered = json.dumps(example, indent=2, ensure_ascii=False)

    assert "## Worked example: follow-up-visit" in prompt
    assert rendered in prompt
    assert "home-bp-monitoring" not in prompt


def test_prompt_renders_human_feedback_on_first_attempt():
    from cpg_ingester.prompts.bpmn_ir_creator import build_bpmn_ir_creator_user

    prompt = build_bpmn_ir_creator_user(
        item={"name": "Home BP Monitoring"},
        source_pages="Review this source carefully.",
        linked_dmn_summaries=[],
        linked_recs=[],
        mermaid_figures="(none)",
        abbreviations="(none)",
        human_feedback="Keep the weekly interval from the CPG.",
        repair_feedback="",
    )

    assert "Keep the weekly interval from the CPG." in prompt
    assert "## Reviewer feedback from round 1" in prompt


def test_prompt_declares_required_parameters_for_unspecified_values():
    flat_prompt = " ".join(build_bpmn_ir_creator_system().split())

    assert "free-text parameters carry their authored text as a non-empty string default" in flat_prompt
    assert "The no-default rule applies to unstated timings, thresholds, and counts only" in flat_prompt
    assert (
        "When the source does not state a timing, threshold, or count, declare a required"
        in flat_prompt
    )
    assert "constraints only when the source states a range" in flat_prompt
    assert "never choose a value" in flat_prompt


def test_authored_default_finding_has_a_targeted_repair_hint():
    from cpg_ingester.reference.bpmn_error_patterns import format_bpmn_error_pattern_hints

    hints = format_bpmn_error_pattern_hints([
        "authored text parameter 'instructions_text' requires a non-empty string default"
    ])

    assert "Give each authored free-text parameter a non-empty string default" in hints
    assert "unstated timings, thresholds, and counts" in hints
