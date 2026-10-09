"""Integration checks for the hypertension automation golden artifacts."""

from __future__ import annotations

import runpy
from pathlib import Path

from lxml import etree

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import (
    BusinessRuleTask,
    IntermediateCatchEvent,
    ProcessIR,
    SequenceFlow,
    Task,
    UserTask,
)
from cpg_contracts.automation.templates import AutomationTemplate
from cpg_contracts.automation.validators.ladder import run_static_ladder


REPO_ROOT = Path(__file__).resolve().parents[2]
AUTOMATION_DIR = REPO_ROOT / "cpg-ingester" / "data" / "golden" / "automation"
CPG_PATH = REPO_ROOT / "cpg-ingester" / "data" / "synthetic-hypertension-cpg-v2.md"
REGENERATE_PATH = AUTOMATION_DIR / "regenerate.py"
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
MONITORING_DMN_NAMESPACE = "https://redhat.com/cpg-to-acp/dmn/monitoring-plan"
EXPECTED_RUNGS = ["L0", "L1", "L2", "L3", "L4", "L5a", "L5b"]
REGENERATOR = runpy.run_path(str(REGENERATE_PATH))


def _golden_paths() -> list[Path]:
    return sorted(AUTOMATION_DIR.glob("*.ir.json"))


def _load_golden(path: Path) -> ProcessIR:
    return ProcessIR.model_validate_json(path.read_text(encoding="utf-8"))


def test_every_golden_passes_the_ladder_and_matches_its_compiled_artifacts() -> None:
    catalog = load_catalog()
    summaries = REGENERATOR["load_dmn_summaries"]()
    namespaces = {
        model_id: summary.namespace
        for model_id, summary in summaries.items()
        if summary.namespace is not None
    }
    section_text = CPG_PATH.read_text(encoding="utf-8")

    for ir_path in _golden_paths():
        ir = _load_golden(ir_path)
        result = run_static_ladder(
            ir,
            catalog=catalog,
            stage="template",
            section_text=section_text,
            dmn_summaries=summaries,
        )
        assert result.ok, f"{ir_path.name}: {result.findings}"
        assert result.rungs_passed == EXPECTED_RUNGS
        assert result.deferred == ir.process.acp.invariants
        assert result.deferred

        xml = compile(ir, catalog=catalog, dmn_namespaces=namespaces)
        xml_path = ir_path.with_name(f"{ir.process.id}.bpmn")
        assert xml == xml_path.read_text(encoding="utf-8")

        template_path = ir_path.with_name(f"{ir.process.id}.template.json")
        template = AutomationTemplate.model_validate_json(
            template_path.read_text(encoding="utf-8")
        )
        assert template.ir == ir
        assert template.bpmn_xml == xml
        assert template.summary.validation.rungs_passed == EXPECTED_RUNGS
        assert template.summary.validation.deferred_invariants == result.deferred
        assert template.summary.linked_recommendation_ids == ["<placeholder-rec-id>"]

        for element in ir.process.flowElements:
            if not isinstance(element, BusinessRuleTask):
                continue
            summary = summaries[element.dmnModel]
            assert set(element.inputs) == {item.name for item in summary.inputs}
            assert set(element.outputs) <= {item.name for item in summary.outputs}


def test_rendered_template_json_and_bpmn_are_current() -> None:
    generated = REGENERATOR["render_outputs"]()
    for filename, content in generated.items():
        assert (AUTOMATION_DIR / filename).read_text(encoding="utf-8") == content


def test_acei_template_has_no_phi_warnings() -> None:
    template = AutomationTemplate.model_validate_json(
        (AUTOMATION_DIR / "acei-lab-follow-up.template.json").read_text(encoding="utf-8")
    )

    assert template.summary.validation.warnings == []


def test_goldens_cover_every_capability_and_both_special_task_types() -> None:
    irs = [_load_golden(path) for path in _golden_paths()]
    capability_ids = {
        element.taskName
        for ir in irs
        for element in ir.process.flowElements
        if isinstance(element, Task)
    }
    assert capability_ids == {item.id for item in load_catalog().capabilities}
    assert any(
        isinstance(element, BusinessRuleTask)
        for ir in irs
        for element in ir.process.flowElements
    )
    assert any(
        isinstance(element, UserTask)
        and element.groupId == "clinicians"
        and element.name == "Review home readings at follow-up visit"
        for ir in irs
        for element in ir.process.flowElements
    )


def test_acei_decision_task_uses_exact_dmn_names_and_xml_safe_ids() -> None:
    ir = _load_golden(AUTOMATION_DIR / "acei-lab-follow-up.ir.json")
    task = next(
        element
        for element in ir.process.flowElements
        if isinstance(element, BusinessRuleTask)
    )
    assert task is ir.process.flowElements[1]
    assert task.dmnModel == "monitoring-plan"
    assert set(task.inputs) == {"Treatment Action", "Has Kidney Disease"}
    assert set(task.outputs) == {"Lab Order", "Lab Timing Weeks"}

    root = etree.fromstring(
        compile(ir, dmn_namespaces={"monitoring-plan": MONITORING_DMN_NAMESPACE}).encode("utf-8")
    )
    namespaces = {"bpmn": BPMN_NS}
    node = root.xpath(".//bpmn:businessRuleTask[@id='run_monitoring_plan']", namespaces=namespaces)[0]
    inputs = {
        item.get("name"): item.get("id")
        for item in node.xpath("./bpmn:ioSpecification/bpmn:dataInput", namespaces=namespaces)
    }
    outputs = {
        item.get("name"): item.get("id")
        for item in node.xpath("./bpmn:ioSpecification/bpmn:dataOutput", namespaces=namespaces)
    }
    assert inputs["Treatment Action"] == "run_monitoring_plan_Treatment_Action_input"
    assert inputs["Has Kidney Disease"] == "run_monitoring_plan_Has_Kidney_Disease_input"
    assert outputs["Lab Order"] == "run_monitoring_plan_Lab_Order_output"
    assert outputs["Lab Timing Weeks"] == "run_monitoring_plan_Lab_Timing_Weeks_output"
    namespace_id = inputs["namespace"]
    namespace_literal = root.xpath(
        ".//bpmn:dataInputAssociation[bpmn:targetRef=$identifier]/bpmn:assignment/bpmn:from/text()",
        namespaces=namespaces,
        identifier=namespace_id,
    )[0]
    assert namespace_literal == MONITORING_DMN_NAMESPACE


def test_acei_uses_plan_parameters_and_routes_null_dmn_timing_to_not_required() -> None:
    ir = _load_golden(AUTOMATION_DIR / "acei-lab-follow-up.ir.json")
    process = ir.process
    elements = {element.id: element for element in process.flowElements}
    parameters = {parameter.name: parameter for parameter in process.acp.parameters}
    decision = elements["run_monitoring_plan"]

    assert {parameter.name for parameter in process.acp.parameters} >= {
        "treatment_action",
        "has_kidney_disease",
    }
    for name in ("treatment_action", "has_kidney_disease"):
        assert parameters[name].required
        assert parameters[name].default is None
        assert name not in {property_.name for property_ in process.properties}
        assert decision.inputs[
            "Treatment Action" if name == "treatment_action" else "Has Kidney Disease"
        ].param == name
    assert [
        (trigger.model_id, trigger.output_name, trigger.output_value)
        for trigger in process.acp.triggers
    ] == [("treatment-recommendation", "Action", "Start medication")]

    gateway = elements["gw_lab_needed"]
    assert gateway.default == "f_lab_not_required"
    flows = {
        element.id: element
        for element in process.flowElements
        if isinstance(element, SequenceFlow)
    }
    assert flows["f_lab_required"].sourceRef == "gw_lab_needed"
    assert flows["f_lab_required"].targetRef == "wait_lab_timing"
    assert flows["f_lab_required"].conditionExpression == "defined(lab_timing_weeks)"
    assert flows["f_lab_not_required"].targetRef == "end_not_required"
    assert elements["end_not_required"].acp.outcome == "not_required"
    assert "request_followup_visit" not in elements
    assert flows["f_notify_end"].sourceRef == "notify_clinician"
    assert flows["f_notify_end"].targetRef == "end_notified"
    assert elements["notify_clinician"].acp.provenance.source_text == (
        "All patients starting ACE inhibitor therapy require a Basic Metabolic "
        "Panel (BMP) to monitor renal function and electrolytes."
    )


def test_follow_up_templates_use_only_their_clinical_dmn_intervals() -> None:
    summaries = REGENERATOR["load_dmn_summaries"]()
    follow_up = summaries["treatment-recommendation"]
    assert "Follow Up Weeks" in {item.name for item in follow_up.outputs}

    expected_values = {"follow-up-visit": [2, 4], "lifestyle-reassessment": [8, 12]}
    for process_id, expected in expected_values.items():
        ir = _load_golden(AUTOMATION_DIR / f"{process_id}.ir.json")
        triggers = [trigger for trigger in ir.process.acp.triggers if trigger.kind == "dmn-output"]
        assert triggers
        assert [
            (trigger.model_id, trigger.output_name, trigger.output_value)
            for trigger in triggers
        ] == [
            ("treatment-recommendation", "Follow Up Weeks", value)
            for value in expected
        ]


def test_medication_follow_up_requests_then_checks_without_waiting() -> None:
    ir = _load_golden(AUTOMATION_DIR / "follow-up-visit.ir.json")
    process = ir.process
    elements = {element.id: element for element in process.flowElements}
    flows = {
        element.id: element
        for element in process.flowElements
        if isinstance(element, SequenceFlow)
    }

    assert not any(isinstance(element, IntermediateCatchEvent) for element in process.flowElements)
    assert flows["f_start"].targetRef == "request_visit"
    assert flows["f_request_query"].sourceRef == "request_visit"
    assert flows["f_request_query"].targetRef == "query_visit_order"
    assert flows["f_query_gateway"].sourceRef == "query_visit_order"
    assert flows["f_query_gateway"].targetRef == "gw_visit_found"
    assert flows["f_visit_found"].targetRef == "end_complete"
    assert flows["f_visit_missing"].targetRef == "notify_clinician"
    assert flows["f_notify_end"].targetRef == "end_notified"
    medication_followup_source = (
        "For patients starting or changing medication, the seven-day home "
        "average should be reviewed two to four weeks after the change, in "
        "addition to the laboratory follow-up in Table 2."
    )
    for element_id in (
        "request_visit",
        "query_visit_order",
        "f_visit_found",
        "f_request_query",
        "notify_clinician",
    ):
        assert elements[element_id].acp.provenance.source_text == medication_followup_source


def test_lifestyle_visit_is_scheduled_before_wait_and_clinician_review() -> None:
    ir = _load_golden(AUTOMATION_DIR / "lifestyle-reassessment.ir.json")
    process = ir.process
    elements = {element.id: element for element in process.flowElements}
    flows = {
        element.id: element
        for element in process.flowElements
        if isinstance(element, SequenceFlow)
    }
    parameters = {parameter.name: parameter for parameter in process.acp.parameters}

    interval = parameters["reassessment_interval"]
    assert interval.required
    assert interval.default is None
    assert interval.source is None
    assert elements["request_reassessment_visit"].inputs["within"].param == "reassessment_interval"
    assert elements["wait_reassessment"].timerEventDefinition.timeDuration.param == "reassessment_interval"
    assert flows["f_send_request"].sourceRef == "send_education"
    assert flows["f_send_request"].targetRef == "request_reassessment_visit"
    assert flows["f_request_wait"].sourceRef == "request_reassessment_visit"
    assert flows["f_request_wait"].targetRef == "wait_reassessment"
    assert flows["f_wait_review"].sourceRef == "wait_reassessment"
    assert flows["f_wait_review"].targetRef == "review_readings"
    assert flows["f_review_end"].sourceRef == "review_readings"
    assert flows["f_review_end"].targetRef == "end_complete"

    message = parameters["education_message"].default.lower()
    assert all(term in message for term in ("dash", "150 minutes", "body weight", "alcohol", "smoking"))
    assert elements["send_education"].inputs["kind"].literal == "education"
