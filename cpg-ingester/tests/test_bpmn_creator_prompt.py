"""Prompt contract tests for process IR creation."""

from cpg_ingester.prompts.bpmn_ir_creator import build_bpmn_ir_creator_system


def test_prompt_includes_catalog_and_forbids_xml_and_coordinates():
    prompt = build_bpmn_ir_creator_system()

    assert "patient.request_observation" in prompt
    assert "ehr.query_observations" in prompt
    assert "Never write BPMN XML" in prompt
    assert "never write coordinates" in prompt
    assert "max_duration" in prompt
    assert "ProcessIR" in prompt


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
    assert "PREVIOUS HUMAN REVIEW FEEDBACK" in prompt
