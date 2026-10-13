"""Exact FHIR shape tests for automation definitions and instances."""

from base64 import b64decode
from pathlib import Path
from uuid import uuid4

from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.instances import ActivityAutomation, ParameterBinding
from cpg_contracts.automation.ir import ProcessIR, TemplateRef
from cpg_contracts.automation.templates import AutomationTemplate, ValidationRecord
from acp_writer.planning_brief import ActivityType, PlanActivity, PlanningBrief
from acp_writer.services.ai_transparency import ACP_EXT_BASE, apply_approval_transition
from acp_writer.services.bff import _extract_view_from_bundle
from acp_writer.services.reviewer import ReviewerContext
from acp_writer.validators.fhir_bundle_builder import build_fhir_bundle
from acp_writer.validators.fhir_syntax import validate_fhir_bundle


GOLDEN = (
    Path(__file__).parent / "fixtures/home-bp-monitoring.template.json"
)


def _automation(*, enabled: bool = True) -> ActivityAutomation:
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    ir = template.ir.model_copy(deep=True)
    ir.process.id = "instance_home_bp"
    ir.process.kind = "instance"
    ir.process.acp.template_refs = [TemplateRef(
        template_id=template.summary.id,
        version=template.summary.version,
        source_cpg=template.summary.source_cpg,
    )]
    bpmn = compile(ir)
    bindings = [
        ParameterBinding(name="reporting_interval", value="P1W", source="cpg-default"),
        ParameterBinding(name="repeat_due", value="PT5M", source="cpg-default"),
        ParameterBinding(name="notify_systolic_threshold", value=135, source="clinician", unit="mm[Hg]"),
        ParameterBinding(name="max_reminder_attempts", value=3, source="cpg-default"),
        ParameterBinding(name="max_duration", value="P90D", source="plan-derived"),
        ParameterBinding(name="reminder_text", value="Submit home readings.", source="authored"),
        ParameterBinding(name="request_readings__code", value="http://loinc.org|85354-9", source="plan-derived"),
        ParameterBinding(name="query_systolic__code", value="http://loinc.org|8480-6", source="plan-derived"),
    ]
    return ActivityAutomation(
        id=str(uuid4()),
        template_refs=[TemplateRef(
            template_id=template.summary.id,
            version=template.summary.version,
            source_cpg=template.summary.source_cpg,
        )],
        template_snapshots=[template.ir],
        ir=ir,
        bpmn_xml=bpmn,
        bindings=bindings,
        capabilities_used=sorted({
            element.taskName for element in ir.process.flowElements
            if getattr(element, "type", None) == "task"
        }),
        validation=ValidationRecord(status="valid", rungs_passed=["L0", "L1", "L2", "L3", "L4", "L5a", "L6", "L7"]),
        enabled=enabled,
    )


def _brief(activity_type=ActivityType.MONITORING, *, enabled=True):
    activity = PlanActivity(
        id="activity-home-bp",
        type=activity_type,
        description="Monitor home blood pressure readings.",
        source_cpg="SYN-HTN-2026-002",
        source_recommendation_id="rec-home-bp",
        automations=[_automation(enabled=enabled)],
    )
    return PlanningBrief(
        patient_reference="Patient/example",
        applicable_cpgs=["SYN-HTN-2026-002"],
        goals=[],
        activities=[activity],
    )


def _resources(bundle):
    return [entry["resource"] for entry in bundle["entry"]]


def test_enabled_automation_has_exact_document_reference_task_extension_and_provenance_shapes():
    bundle = build_fhir_bundle(_brief(), model_id="test-model", prompts={
        "activity-home-bp:home-bp-monitoring:synthesis:1": "synthesis prompt",
    }, careplan_id="stable-careplan-123")
    resources = _resources(bundle)
    careplan = next(item for item in resources if item["resourceType"] == "CarePlan")
    docref = next(item for item in resources if item["resourceType"] == "DocumentReference" and item.get("identifier", [{}])[0].get("system", "").endswith("automation-revision"))
    task = next(item for item in resources if item["resourceType"] == "Task")
    activity = careplan["activity"][0]
    automation_extension = activity["extension"][0]

    assert careplan["identifier"] == [{
        "system": "https://github.com/samschifman/cpg-to-acp/fhir/CodeSystem/careplan-id",
        "value": "stable-careplan-123",
    }]
    careplan_entry = next(
        entry for entry in bundle["entry"]
        if entry["resource"].get("resourceType") == "CarePlan"
    )
    assert careplan_entry["request"]["ifNoneExist"] == (
        "identifier=https://github.com/samschifman/cpg-to-acp/fhir/CodeSystem/"
        "careplan-id|stable-careplan-123"
    )

    assert activity["id"] == "activity-home-bp"
    assert automation_extension["url"] == f"{ACP_EXT_BASE}/activity-automation"
    refs = {item["url"]: item for item in automation_extension["extension"]}
    assert refs["definition"]["valueReference"]["reference"] == f"urn:uuid:{docref['id']}"
    assert refs["instance"]["valueReference"]["reference"] == f"urn:uuid:{task['id']}"
    assert refs["status"]["valueCode"] == "enabled"

    assert docref["status"] == "current"
    assert docref["subject"] == task["for"]
    assert docref["context"]["related"] == [
        {"reference": f"urn:uuid:{careplan['id']}"},
        {"reference": f"urn:uuid:{task['id']}"},
    ]
    attachment = docref["content"][0]["attachment"]
    assert attachment["contentType"] == "application/xml"
    assert b64decode(attachment["data"]).decode() == _brief().activities[0].automations[0].bpmn_xml
    assert docref["meta"]["security"][0]["code"] == "AIAST"

    assert task["status"] == "draft"
    assert task["intent"] == "plan"
    assert task["focus"]["reference"] == activity["reference"]["reference"]
    assert task["basedOn"] == [{"reference": f"urn:uuid:{careplan['id']}"}]
    assert task["meta"]["security"][0]["code"] == "AIAST"
    inputs = task["input"]
    assert inputs[0]["valueReference"]["reference"] == f"urn:uuid:{docref['id']}"
    values = {item["type"].get("text"): item for item in inputs[1:]}
    assert values["reporting_interval"]["valueDuration"]["value"] == 7
    assert values["repeat_due"]["valueDuration"] == {
        "value": 5,
        "unit": "minute",
        "system": "http://unitsofmeasure.org",
        "code": "min",
    }
    assert values["notify_systolic_threshold"]["valueQuantity"]["code"] == "mm[Hg]"
    assert values["max_reminder_attempts"]["valueInteger"] == 3
    assert values["reminder_text"]["valueString"] == "Submit home readings."
    assert values["request_readings__code"]["valueCodeableConcept"]["coding"][0]["code"] == "85354-9"

    automation_provenance = [
        item for item in resources
        if item["resourceType"] == "Provenance"
        and any(target.get("reference") == f"urn:uuid:{task['id']}" for target in item.get("target", []))
    ]
    assert len(automation_provenance) == 1
    target_paths = [
        ext["valueString"]
        for target in automation_provenance[0]["target"]
        for ext in target.get("extension", [])
        if ext.get("url") == "http://hl7.org/fhir/StructureDefinition/targetPath"
    ]
    assert target_paths == ["CarePlan.activity[0]"]
    assert validate_fhir_bundle(bundle) == []

    modified = apply_approval_transition(bundle, ReviewerContext(display="Dr Example"))
    assert task["status"] == "requested"
    assert f"urn:uuid:{task['id']}" in modified


def test_dangling_automation_definition_reference_fails_syntax_validation():
    bundle = build_fhir_bundle(_brief())
    careplan = next(
        item["resource"] for item in bundle["entry"]
        if item["resource"].get("resourceType") == "CarePlan"
    )
    careplan["activity"][0]["extension"][0]["extension"][0]["valueReference"]["reference"] = (
        "urn:uuid:missing-definition"
    )
    assert any(
        "Unresolved automation reference" in error and "missing-definition" in error
        for error in validate_fhir_bundle(bundle)
    )


def test_disabled_automation_is_not_written():
    bundle = build_fhir_bundle(_brief(enabled=False))
    resources = _resources(bundle)
    careplan = next(item for item in resources if item["resourceType"] == "CarePlan")
    assert "extension" not in careplan["activity"][0]
    assert not any(item["resourceType"] in {"Task", "DocumentReference"} for item in resources)


def test_persisted_bundle_reconstructs_activity_automation_for_the_ui():
    brief = _brief()
    source_automation = brief.activities[0].automations[0]
    bundle = build_fhir_bundle(brief, careplan_id="stable-careplan-123")
    _goals, activities, _conflicts = _extract_view_from_bundle(bundle)

    activity = next(item for item in activities if item["id"] == "activity-home-bp")
    automation = activity["automations"][0]
    assert automation["id"] == source_automation.id
    assert automation["activityIds"] == ["activity-home-bp"]
    assert automation["enabled"] is True
    assert automation["templateRefs"] == [{
        "templateId": "home-bp-monitoring",
        "version": source_automation.template_refs[0].version,
        "sourceCpg": "SYN-HTN-2026-002",
    }]
    assert automation["bpmnXml"] == source_automation.bpmn_xml
    bindings = {item["name"]: item for item in automation["bindings"]}
    assert bindings["reporting_interval"]["value"] == 7
    assert bindings["reporting_interval"]["unit"] == "d"
    assert bindings["reporting_interval"]["source"] == "cpg-default"


def test_inline_activity_does_not_add_task_focus():
    bundle = build_fhir_bundle(_brief(ActivityType.EDUCATIONAL))
    task = next(item for item in _resources(bundle) if item["resourceType"] == "Task")
    assert "focus" not in task
