"""Activity automation and publication contract tests."""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from cpg_contracts.automation.instances import (
    ActivityAutomation,
    Evidence,
    ParameterBinding,
    PublicationPayload,
    PublishedAutomation,
)
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR, TemplateRef, ir_version
from cpg_contracts.automation.templates import ValidationRecord


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(
        (FIXTURES / "home-bp-monitoring.ir.json").read_text(encoding="utf-8")
    )


def activity_automation(
    *,
    status: str = "valid",
    bindings: list[ParameterBinding] | None = None,
    enabled: bool | None = None,
) -> ActivityAutomation:
    template = fixture_ir()
    instance = deepcopy(template)
    instance.process.id = "home_bp_instance"
    instance.process.kind = "instance"
    reference = TemplateRef(
        template_id=template.process.id,
        version=ir_version(template),
    )
    instance.process.acp.template_refs = [reference]
    for element in instance.process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=reference.template_id,
            version=reference.version,
            element_id=element.id,
        )
    capability_ids = sorted(
        {
            element.taskName
            for element in instance.process.flowElements
            if getattr(element, "type", None) == "task"
        }
    )
    return ActivityAutomation(
        id="d0f1b4b1-2514-4b15-97f2-d2efac42b147",
        template_refs=[reference],
        template_snapshots=[template],
        ir=instance,
        bpmn_xml=compile(instance, catalog=load_catalog()),
        bindings=bindings or [],
        derivation_evidence={
            "ev-seq": Evidence(kind="plan-sequence", payload={"activity_ids": ["a", "b"]})
        },
        capabilities_used=capability_ids,
        validation=ValidationRecord(status=status),
        enabled=enabled,
        review_notes=[],
    )


def test_activity_automation_defaults_enabled_from_validation_status() -> None:
    assert activity_automation(status="valid").enabled is True
    assert activity_automation(status="incomplete").enabled is False
    assert activity_automation(status="escalated").enabled is False


def test_activity_automation_revision_is_stable_across_binding_order() -> None:
    first = activity_automation(
        bindings=[
            ParameterBinding(name="z_value", value=2, source="plan-derived"),
            ParameterBinding(name="a_value", value=1, source="clinician"),
        ]
    )
    second = activity_automation(
        bindings=[
            ParameterBinding(name="a_value", value=1, source="clinician"),
            ParameterBinding(name="z_value", value=2, source="plan-derived"),
        ]
    )

    assert first.revision == second.revision
    assert len(first.revision) == 12


def test_activity_automation_revision_changes_when_a_binding_changes() -> None:
    first = activity_automation(
        bindings=[ParameterBinding(name="threshold", value=135, source="plan-derived")]
    )
    second = activity_automation(
        bindings=[ParameterBinding(name="threshold", value=140, source="plan-derived")]
    )

    assert first.revision != second.revision


def test_bound_patient_value_stays_out_of_bpmn_xml() -> None:
    sentinel = "patient-specific-value-must-not-enter-the-definition"
    automation = activity_automation(
        bindings=[
            ParameterBinding(
                name="reminder_text",
                value=sentinel,
                source="clinician",
            )
        ]
    )

    assert sentinel not in automation.bpmn_xml


def test_activity_automation_rejects_enabled_incomplete_status() -> None:
    with pytest.raises(ValidationError, match="cannot be enabled"):
        activity_automation(status="incomplete", enabled=True)


def test_activity_automation_rejects_a_stale_revision() -> None:
    value = activity_automation()
    data = value.model_dump(mode="python")
    data["revision"] = "000000000000"

    with pytest.raises(ValidationError, match="canonical IR and bindings"):
        ActivityAutomation.model_validate(data)


def test_activity_automation_id_must_be_a_uuid() -> None:
    data = activity_automation().model_dump(mode="python")
    data["id"] = "automation-1"

    with pytest.raises(ValidationError):
        ActivityAutomation.model_validate(data)


def test_publication_payload_accepts_fhir_task_and_document_reference() -> None:
    activity = activity_automation()
    item = PublishedAutomation(
        automation_id=activity.id,
        activity_ids=["activity-1", "activity-2"],
        revision=activity.revision,
        template_refs=activity.template_refs,
        task={"resourceType": "Task", "id": "task-1"},
        document_reference={
            "resourceType": "DocumentReference",
            "id": "document-1",
        },
    )
    payload = PublicationPayload(
        job_id="job-1",
        careplan_id="careplan-1",
        careplan_server_id="CarePlan/server-1",
        patient_server_id="Patient/server-1",
        approved_at="2026-10-09T12:00:00Z",
        reviewer="clinician-1",
        automations=[item],
    )

    assert payload.automations[0].task["resourceType"] == "Task"
    assert payload.replaces_careplan_id is None


def test_publication_item_requires_correct_fhir_resource_types() -> None:
    activity = activity_automation()
    with pytest.raises(ValidationError, match="FHIR Task"):
        PublishedAutomation(
            automation_id=activity.id,
            activity_ids=["activity-1"],
            revision=activity.revision,
            template_refs=activity.template_refs,
            task={"resourceType": "Observation"},
            document_reference={"resourceType": "DocumentReference"},
        )
