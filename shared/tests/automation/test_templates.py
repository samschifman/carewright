"""Automation template contract validation and serialization."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.ir import ProcessIR, ir_version
from cpg_contracts.automation.templates import (
    AutomationTemplate,
    AutomationTemplateSummary,
    Automatability,
    ValidationRecord,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(
        (FIXTURES / "home-bp-monitoring.ir.json").read_text(encoding="utf-8")
    )


def template_summary(ir: ProcessIR) -> AutomationTemplateSummary:
    return AutomationTemplateSummary(
        id=ir.process.id,
        version=ir_version(ir),
        name=ir.process.name,
        description=ir.process.description,
        source_cpg=ir.process.acp.provenance.source_cpg,
        section=ir.process.acp.provenance.section,
        triggers=ir.process.acp.triggers,
        linked_recommendation_ids=["rec-home-bp"],
        linked_decision_model_ids=[],
        parameters=ir.process.acp.parameters,
        capabilities_used=sorted(
            {
                element.taskName
                for element in ir.process.flowElements
                if getattr(element, "type", None) == "task"
            }
        ),
        catalog_version=load_catalog().version,
        ir_version=ir.ir_version,
        pattern_family="remind-until-done",
        automatability=Automatability(tier="A", rationale="The process has explicit cadence."),
        validation=ValidationRecord(
            status="valid",
            rungs_passed=["L0", "L1", "L2", "L3", "L4", "L5a", "L5b"],
            deferred_invariants=ir.process.acp.invariants,
        ),
        artifact_id="artifact-home-bp",
    )


def test_automation_template_roundtrips_and_pins_canonical_ir_version() -> None:
    ir = fixture_ir()
    template = AutomationTemplate(
        summary=template_summary(ir),
        ir=ir,
        bpmn_xml="<bpmn2:definitions />",
    )

    parsed = AutomationTemplate.model_validate_json(template.model_dump_json())

    assert parsed.contract_version == "1.1"
    assert parsed.summary.version == ir_version(ir)
    assert parsed == template


def test_automation_template_rejects_stale_summary_hash() -> None:
    ir = fixture_ir()
    summary = template_summary(ir).model_dump(mode="python")
    summary["version"] = "000000000000"

    with pytest.raises(ValidationError, match="canonical IR hash"):
        AutomationTemplate(
            summary=summary,
            ir=ir,
            bpmn_xml="<bpmn2:definitions />",
        )


def test_template_summary_requires_a_recommendation_link() -> None:
    ir = fixture_ir()
    data = template_summary(ir).model_dump(mode="python")
    data["linked_recommendation_ids"] = []

    with pytest.raises(ValidationError):
        AutomationTemplateSummary.model_validate(data)


def test_validation_record_accepts_all_supported_statuses() -> None:
    for status in ("valid", "incomplete", "escalated"):
        record = ValidationRecord(status=status)
        assert record.status == status
