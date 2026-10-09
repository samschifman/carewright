"""Template grounding checks against the cited clinical section."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from cpg_contracts.automation.ir import (
    Literal_,
    SourceRef,
    StructuralProvenance,
)
from cpg_contracts.automation.validators.grounding import (
    validate_template_structure,
    validate_template_text,
)
from cpg_contracts.automation.validators.textmatch import contains, normalize


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
SECTION = (FIXTURES / "syn-htn-v2-section-3.5-3.6.md").read_text(encoding="utf-8")
ABBREVIATIONS = {"BP": "blood pressure"}


def fixture_ir():
    from cpg_contracts.automation.ir import ProcessIR

    return ProcessIR.model_validate_json(
        (FIXTURES / "home-bp-monitoring.ir.json").read_text(encoding="utf-8")
    )


def findings_for(ir, section: str = SECTION):
    return validate_template_text(ir, section, ABBREVIATIONS)


def codes(findings) -> set[str]:
    return {finding.code for finding in findings}


def test_appendix_b_template_passes_structure_and_text_grounding() -> None:
    ir = fixture_ir()

    assert validate_template_structure(ir) == []
    assert findings_for(ir) == []


def test_normalize_expands_abbreviations_and_contains_uses_fuzzy_token_windows() -> None:
    assert normalize("Home BP readings!", ABBREVIATIONS) == "home blood pressure readings"
    assert contains(
        "Patients should take two readings one minute apart in the morning before medications and two readings one minute apart in the evening.",
        "Patients should take two measurements one minute apart in the morning before medications and two readings one minute apart in the evening",
        abbreviations=ABBREVIATIONS,
    )


def test_source_citation_must_be_found_in_the_section() -> None:
    ir = fixture_ir()
    task = next(item for item in ir.process.flowElements if item.id == "notify")
    task.acp.provenance.source_text = "Invented advice unrelated to the guideline."

    assert "T1" in codes(findings_for(ir))


def test_cpg_default_value_must_appear_in_its_citation() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source.source_text = "After four attempts the team should contact the patient."

    assert "T2" in codes(findings_for(ir))


def test_authored_defaults_are_only_allowed_for_string_parameters() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source = SourceRef(kind="authored")

    assert "T2" in codes(validate_template_structure(ir))


def test_reviewer_default_must_keep_cpg_citation_and_satisfy_constraints() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Reviewer",
        note="Updated after review",
        previous=deepcopy(parameter.source),
    )
    parameter.default = 8

    assert "T2" in codes(validate_template_structure(ir))


def test_reviewer_default_without_previous_cpg_citation_is_rejected() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Reviewer",
        note="Updated after review",
        previous=SourceRef(kind="authored"),
    )

    assert "T2" in codes(validate_template_structure(ir))


def test_policy_default_source_is_limited_to_reserved_parameters() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source = SourceRef(kind="policy", policy="project.policy")

    assert "T2" in codes(validate_template_structure(ir))


def test_structural_provenance_rule_and_supports_must_be_valid() -> None:
    ir = fixture_ir()
    start = next(item for item in ir.process.flowElements if item.id == "start")
    start.acp.provenance = StructuralProvenance(
        kind="structural", derivation_rule="parameter-binding", supports=[]
    )

    assert "T3" in codes(validate_template_structure(ir))


def test_structural_boundary_must_be_plan_bound_to_max_duration() -> None:
    ir = fixture_ir()
    boundary = next(item for item in ir.process.flowElements if item.id == "period_elapsed")
    boundary.acp.provenance.derivation_rule = "end-event"

    assert "T3" in codes(validate_template_structure(ir))


def test_plan_bound_boundary_must_attach_to_main_scope() -> None:
    ir = fixture_ir()
    boundary = next(item for item in ir.process.flowElements if item.id == "period_elapsed")
    boundary.attachedToRef = "request_readings"

    assert "T3" in codes(validate_template_structure(ir))


def test_split_must_be_supported_by_source_derived_condition_operands() -> None:
    ir = fixture_ir()
    flow = next(item for item in ir.process.flowElements if item.id == "f_high")
    flow.conditionExpression = "invented_threshold > 0"

    assert "T3" in codes(validate_template_structure(ir))


def test_source_derived_numeric_literals_must_be_cited() -> None:
    ir = fixture_ir()
    flow = next(item for item in ir.process.flowElements if item.id == "f_escalate")
    flow.conditionExpression = "reminder_attempts >= 9"

    assert "T4" in codes(findings_for(ir))


def test_enum_literals_must_match_catalog_and_be_grounded() -> None:
    ir = fixture_ir()
    notify = next(item for item in ir.process.flowElements if item.id == "notify")
    notify.inputs["urgency"] = Literal_(literal="immediate", type="enum")

    assert "T4" in codes(validate_template_structure(ir))


def test_unit_literal_must_match_quantity_output_property() -> None:
    ir = fixture_ir()
    task = next(item for item in ir.process.flowElements if item.id == "query_diastolic")
    task.inputs["unit"] = Literal_(literal="kg", type="unit")

    assert "T4" in codes(validate_template_structure(ir))


def test_code_capability_input_requires_a_concept_reference() -> None:
    ir = fixture_ir()
    task = next(item for item in ir.process.flowElements if item.id == "request_readings")
    task.inputs["code"] = Literal_(literal="8480-6", type="code")

    assert "T4" in codes(validate_template_structure(ir))


def test_structural_constant_flag_does_not_hide_other_numeric_literals() -> None:
    ir = fixture_ir()
    flow = next(item for item in ir.process.flowElements if item.id == "f_some")
    flow.conditionExpression = "reading_count > 4"

    assert "T4" in codes(validate_template_structure(ir))


def test_clinically_meaningful_element_cannot_have_structural_provenance() -> None:
    ir = fixture_ir()
    task = next(item for item in ir.process.flowElements if item.id == "notify")
    task.acp.provenance = StructuralProvenance(
        kind="structural", derivation_rule="exclusive-split", supports=["f_high"]
    )

    assert "T5" in codes(validate_template_structure(ir))


def test_public_template_validator_is_traced_when_mlflow_is_installed() -> None:
    import pytest

    pytest.importorskip("mlflow")

    assert getattr(validate_template_structure, "__mlflow_traced__", False)
    assert getattr(validate_template_text, "__mlflow_traced__", False)
