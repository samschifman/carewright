"""Template grounding checks against the cited clinical section."""

from __future__ import annotations

from pathlib import Path

import pytest

from cpg_contracts.automation.ir import (
    Constraints,
    Literal_,
    Parameter,
    SourceRef,
    StructuralProvenance,
)
from cpg_contracts.automation.validators.grounding import (
    validate_template_structure,
    validate_template_text,
)
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.validators.ladder import run_static_ladder
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


def test_exact_whole_token_match_near_end_of_long_section_skips_fuzzy_matching(
    monkeypatch,
) -> None:
    from cpg_contracts.automation.validators import textmatch

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("exact whole-token match must not enter fuzzy matching")

    monkeypatch.setattr(textmatch, "SequenceMatcher", fail_if_called)
    citation = "The patient should measure blood pressure daily."
    section = "introductory filler " * 1001 + citation

    assert contains(section, citation)


def test_exact_matching_uses_whole_token_boundaries() -> None:
    assert not contains("The patient has hypertension.", "hyper")


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


def test_five_minute_parameter_default_matches_written_out_cpg_duration() -> None:
    ir = fixture_ir()
    parameter = next(
        item for item in ir.process.acp.parameters if item.name == "repeat_due"
    )
    assert parameter.default == "PT5M"

    findings = findings_for(ir)
    assert not any(
        finding.code == "T2" and finding.element_id == parameter.name
        for finding in findings
    )


def test_week_duration_grounds_against_a_week_phrase() -> None:
    ir = fixture_ir()
    timer = next(item for item in ir.process.flowElements if item.id == "wait_interval")
    timer.timerEventDefinition.timeDuration = Literal_(literal="P7D", type="duration")
    cited_text = "If a week passes without readings, a reminder should be sent to the patient."
    timer.acp.provenance.source_text = cited_text

    findings = findings_for(ir, cited_text)

    assert not any(
        finding.element_id == "wait_interval"
        and finding.code in {"T4", "T4-value-uncorroborated"}
        for finding in findings
    )

    ladder = run_static_ladder(
        ir,
        catalog=load_catalog(),
        stage="template",
        section_text=f"{SECTION}\n\n{cited_text}",
    )
    assert ladder.ok
    assert not any(item.code == "T4-value-uncorroborated" for item in ladder.findings)


def test_three_day_duration_warns_against_a_week_phrase() -> None:
    ir = fixture_ir()
    timer = next(item for item in ir.process.flowElements if item.id == "wait_interval")
    timer.timerEventDefinition.timeDuration = Literal_(literal="P3D", type="duration")
    cited_text = "If a week passes without readings, a reminder should be sent to the patient."
    timer.acp.provenance.source_text = cited_text

    warning = next(
        finding for finding in findings_for(ir, cited_text)
        if finding.code == "T4-value-uncorroborated"
        and finding.element_id == "wait_interval"
    )
    assert warning.severity == "WARNING"
    assert "P3D" in warning.message
    assert cited_text in warning.message


def test_numeric_literal_without_citation_remains_an_error() -> None:
    ir = fixture_ir()
    flow = next(item for item in ir.process.flowElements if item.id == "f_escalate")
    flow.acp.provenance = None
    flow.conditionExpression = "reminder_attempts >= 9"

    finding = next(
        finding for finding in findings_for(ir)
        if finding.code == "T4" and finding.element_id == "f_escalate"
    )
    assert finding.severity == "ERROR"
    assert "no source citation" in finding.message


def test_duration_literal_without_citation_remains_an_error() -> None:
    ir = fixture_ir()
    timer = next(item for item in ir.process.flowElements if item.id == "wait_interval")
    timer.timerEventDefinition.timeDuration = Literal_(literal="P7D", type="duration")
    timer.acp.provenance = None

    finding = next(
        item for item in findings_for(ir)
        if item.code == "T4" and item.element_id == "wait_interval"
    )
    assert finding.severity == "ERROR"
    assert "no source citation" in finding.message


def test_catalog_enum_input_is_not_required_to_appear_in_cpg_text() -> None:
    ir = fixture_ir()
    remind = next(item for item in ir.process.flowElements if item.id == "remind")
    remind.inputs["kind"] = Literal_(literal="result", type="enum")

    findings = findings_for(ir)
    assert not any(
        finding.code == "T4" and "literal 'result'" in finding.message
        for finding in findings
    )


def test_authored_defaults_are_only_allowed_for_string_parameters() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source = SourceRef(kind="authored")

    assert "T2" in codes(validate_template_structure(ir))


def test_reviewer_numeric_default_keeps_cpg_citation_but_is_not_token_checked() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Reviewer",
        note="Updated after review",
        previous=SourceRef(
            kind="cpg",
            source_text="After four attempts the team should contact the patient.",
        ),
    )
    parameter.default = 3

    assert "T2" not in codes(validate_template_structure(ir))
    assert not any(
        finding.code == "T2" and finding.element_id == parameter.name
        for finding in findings_for(ir)
    )


@pytest.mark.parametrize(
    ("parameter_type", "default", "constraints"),
    [
        ("duration", "PT1S", None),
        ("boolean", True, None),
        ("string", "selected", Constraints(enum=["selected", "other"])),
    ],
)
def test_reviewer_cpg_citation_is_valid_for_non_numeric_default_types(
    parameter_type, default, constraints
) -> None:
    ir = fixture_ir()
    parameter = Parameter(
        name=f"reviewed_{parameter_type}_value",
        type=parameter_type,
        required=True,
        default=default,
        constraints=constraints,
        source=SourceRef(
            kind="reviewer",
            reviewer="Reviewer",
            note="Updated after review",
            previous=SourceRef(kind="cpg", source_text="The original citation."),
        ),
    )
    ir.process.acp.parameters.append(parameter)

    assert not any(
        finding.code == "T2" and finding.element_id == parameter.name
        for finding in validate_template_structure(ir)
    )
    assert not any(
        finding.code == "T2" and finding.element_id == parameter.name
        for finding in findings_for(ir)
    )


def test_reviewer_string_default_may_replace_authored_text() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "instructions_text")
    parameter.default = "Review the available information."
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Reviewer",
        note="Clarified the authored text.",
        previous=SourceRef(kind="authored"),
    )

    assert "T2" not in codes(validate_template_structure(ir))
    assert not any(
        finding.code == "T2" and finding.element_id == parameter.name
        for finding in findings_for(ir)
    )


def test_reviewer_previous_chain_retains_cpg_citation() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.default = 3
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Second reviewer",
        note="Confirmed the prior review.",
        previous=SourceRef(
            kind="reviewer",
            reviewer="First reviewer",
            note="Updated after review.",
            previous=SourceRef(
                kind="cpg",
                source_text="After four attempts the team should contact the patient.",
            ),
        ),
    )

    assert not any(
        finding.code == "T2" and finding.element_id == parameter.name
        for finding in validate_template_structure(ir)
    )


def test_reviewer_edited_text_still_passes_through_l3_phi_scan() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "instructions_text")
    parameter.default = "Review reference ZZ12345."
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Reviewer",
        note="Edited the authored text.",
        previous=SourceRef(kind="authored"),
    )

    result = run_static_ladder(
        ir,
        catalog=load_catalog(),
        stage="template",
        section_text=SECTION,
    )

    assert any(item.rung == "L3" and item.code == "mrn-like" for item in result.findings)


def test_reviewer_authored_previous_is_rejected_for_non_string_parameter() -> None:
    ir = fixture_ir()
    parameter = next(item for item in ir.process.acp.parameters if item.name == "max_reminder_attempts")
    parameter.source = SourceRef(
        kind="reviewer",
        reviewer="Reviewer",
        note="Updated after review",
        previous=SourceRef(kind="authored"),
    )

    assert any(
        finding.code == "T2" and parameter.name in finding.message
        for finding in validate_template_structure(ir)
    )


def test_reviewer_source_without_previous_is_rejected_by_contract() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="reviewer source requires previous"):
        SourceRef.model_validate(
            {"kind": "reviewer", "reviewer": "Reviewer", "note": "Edited"}
        )


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

    finding = next(
        item for item in findings_for(ir)
        if item.element_id == "f_escalate" and item.code == "T4-value-uncorroborated"
    )
    assert finding.severity == "WARNING"


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
