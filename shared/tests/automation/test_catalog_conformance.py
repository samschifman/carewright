"""Tests for catalog and DMN conformance checks."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from lxml import etree
import pytest

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.ir import (
    BusinessRuleTask,
    ConceptRef,
    Constraints,
    Literal_,
    Parameter,
    ParamRef,
    ProcessIR,
    Property,
    PropertyRef,
)
from cpg_contracts.automation.validators.catalog_conformance import (
    validate_catalog,
    validate_catalog_xml,
)
from cpg_contracts.decisions import DecisionModelSummary, DecisionVariable


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
DROOLS_NS = "http://www.jboss.org/drools"
NS = {"b": BPMN_NS}


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(
        (FIXTURES / "home-bp-monitoring.ir.json").read_text(encoding="utf-8")
    )


def catalog_findings(ir: ProcessIR) -> list:
    return validate_catalog(ir, load_catalog())


def codes(findings: list) -> set[str]:
    return {finding.code for finding in findings}


def task_by_id(ir: ProcessIR, identifier: str):
    return next(item for item in ir.process.flowElements if item.id == identifier)


def test_home_bp_golden_ir_passes_catalog_conformance() -> None:
    assert catalog_findings(fixture_ir()) == []


def test_unknown_capability_is_reported() -> None:
    ir = fixture_ir()
    task_by_id(ir, "request_readings").taskName = "patient.unknown_action"

    assert "unknown-capability" in codes(catalog_findings(ir))


def test_reserved_work_item_name_is_reported() -> None:
    ir = fixture_ir()
    task_by_id(ir, "request_readings").taskName = "Rest"

    assert "reserved-task-name" in codes(catalog_findings(ir))


def test_missing_required_capability_input_is_reported() -> None:
    ir = fixture_ir()
    task_by_id(ir, "request_readings").inputs.pop("due")

    assert "missing-required-input" in codes(catalog_findings(ir))


def test_unknown_capability_input_is_reported() -> None:
    ir = fixture_ir()
    task_by_id(ir, "request_readings").inputs["bogus"] = PropertyRef(
        property="reading_count"
    )

    assert "unknown-input" in codes(catalog_findings(ir))


def test_input_value_type_must_match_capability_type() -> None:
    ir = fixture_ir()
    task_by_id(ir, "request_readings").inputs["due"] = ConceptRef(concept="one week")

    assert "input-type" in codes(catalog_findings(ir))


def test_enum_literal_must_be_in_catalog_values() -> None:
    ir = fixture_ir()
    notify = task_by_id(ir, "notify")
    notify.inputs["urgency"] = Literal_(literal="immediate", type="enum")

    assert "enum-value" in codes(catalog_findings(ir))


def test_dynamic_enum_parameter_must_be_constrained_to_catalog_values() -> None:
    ir = fixture_ir()
    notify = task_by_id(ir, "notify")
    notify.inputs["urgency"] = ParamRef(param="notify_reason")

    assert "enum-value" in codes(catalog_findings(ir))


def test_dynamic_enum_parameter_with_catalog_subset_is_valid() -> None:
    ir = fixture_ir()
    ir.process.acp.parameters.append(
        Parameter(
            name="urgency_choice",
            type="string",
            constraints=Constraints(enum=["routine", "urgent"]),
        )
    )
    task_by_id(ir, "notify").inputs["urgency"] = ParamRef(param="urgency_choice")

    assert catalog_findings(ir) == []


def test_free_text_capability_input_requires_a_string_parameter() -> None:
    ir = fixture_ir()
    task_by_id(ir, "request_readings").inputs["instructions"] = ConceptRef(
        concept="patient instructions"
    )

    assert "free-text-input" in codes(catalog_findings(ir))


def test_malformed_unit_literal_is_an_error() -> None:
    ir = fixture_ir()
    task_by_id(ir, "query_diastolic").inputs["unit"] = Literal_(
        literal="kg//m2", type="unit"
    )

    findings = catalog_findings(ir)

    assert any(f.code == "unit-syntax" and f.severity == "ERROR" for f in findings)


def test_well_formed_uncached_unit_literal_is_a_warning() -> None:
    ir = fixture_ir()
    task_by_id(ir, "query_diastolic").inputs["unit"] = Literal_(
        literal="kg/L", type="unit"
    )
    ir.process.properties = [
        Property(
            name=prop.name,
            type=prop.type,
            unit="kg/L" if prop.name == "avg_diastolic" else prop.unit,
        )
        for prop in ir.process.properties
    ]

    findings = catalog_findings(ir)

    assert findings
    assert all(f.code == "unit-unknown" and f.severity == "WARNING" for f in findings)


def test_quantity_output_property_unit_must_match_capability_unit() -> None:
    ir = fixture_ir()
    task_by_id(ir, "query_diastolic").inputs["unit"] = Literal_(
        literal="kg", type="unit"
    )

    findings = catalog_findings(ir)

    assert any(f.code == "unit-mismatch" and f.severity == "ERROR" for f in findings)


def test_output_name_must_exist_in_capability() -> None:
    ir = fixture_ir()
    task_by_id(ir, "query_diastolic").outputs["not_an_output"] = "avg_diastolic"

    assert "unknown-output" in codes(catalog_findings(ir))


def test_output_target_must_be_a_declared_property() -> None:
    ir = fixture_ir()
    task_by_id(ir, "query_diastolic").outputs["average_value"] = "not_a_property"

    assert "output-property" in codes(catalog_findings(ir))


def test_output_property_type_must_match_capability_output_type() -> None:
    ir = fixture_ir()
    process = ir.process
    process.properties = [
        Property(
            name=prop.name,
            type="integer" if prop.name == "avg_diastolic" else prop.type,
            unit=None if prop.name == "avg_diastolic" else prop.unit,
        )
        for prop in process.properties
    ]

    assert "output-type" in codes(catalog_findings(ir))


def test_well_formed_uncached_unit_is_warned_without_quantity_output() -> None:
    ir = fixture_ir()
    task_by_id(ir, "query_diastolic").inputs["unit"] = Literal_(
        literal="kg/L", type="unit"
    )
    task_by_id(ir, "query_diastolic").outputs.clear()

    findings = catalog_findings(ir)

    assert any(f.code == "unit-unknown" and f.severity == "WARNING" for f in findings)


def test_property_unit_syntax_is_checked() -> None:
    ir = fixture_ir()
    ir.process.properties = [
        Property(
            name=prop.name,
            type=prop.type,
            unit="kg//m2" if prop.name == "avg_diastolic" else prop.unit,
        )
        for prop in ir.process.properties
    ]

    findings = catalog_findings(ir)

    assert any(
        f.code == "unit-syntax" and f.severity == "ERROR" and f.element_id == "avg_diastolic"
        for f in findings
    )


def test_property_unit_outside_cache_is_warned() -> None:
    ir = fixture_ir()
    ir.process.properties = [
        Property(
            name=prop.name,
            type=prop.type,
            unit="kg/L" if prop.name == "avg_diastolic" else prop.unit,
        )
        for prop in ir.process.properties
    ]

    findings = catalog_findings(ir)

    assert any(
        f.code == "unit-unknown" and f.severity == "WARNING" and f.element_id == "avg_diastolic"
        for f in findings
    )


def test_parameter_unit_syntax_is_checked() -> None:
    ir = fixture_ir()
    ir.process.acp.parameters.append(
        Parameter(name="invalid_unit", type="quantity", unit="kg//m2", required=True)
    )

    findings = catalog_findings(ir)

    assert any(
        f.code == "unit-syntax" and f.severity == "ERROR" and f.element_id == "invalid_unit"
        for f in findings
    )


def test_parameter_unit_outside_cache_is_warned() -> None:
    ir = fixture_ir()
    ir.process.acp.parameters.append(
        Parameter(name="uncached_unit", type="quantity", unit="kg/L", required=True)
    )

    findings = catalog_findings(ir)

    assert any(
        f.code == "unit-unknown" and f.severity == "WARNING" and f.element_id == "uncached_unit"
        for f in findings
    )


def dmn_summary() -> DecisionModelSummary:
    return DecisionModelSummary(
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
    )


def ir_with_dmn_task() -> ProcessIR:
    ir = deepcopy(fixture_ir())
    ir.process.properties.extend(
        [
            Property(name="has_kidney_disease", type="boolean"),
            Property(name="lab_order", type="string"),
            Property(name="lab_timing_weeks", type="decimal"),
        ]
    )
    ir.process.flowElements.append(
        BusinessRuleTask(
            type="businessRuleTask",
            id="decide_monitoring",
            name="Decide monitoring plan",
            dmnModel="monitoring-plan",
            inputs={
                "Treatment Action": {"param": "notify_reason"},
                "Has Kidney Disease": {"property": "has_kidney_disease"},
            },
            outputs={
                "Lab Order": "lab_order",
                "Lab Timing Weeks": "lab_timing_weeks",
            },
        )
    )
    return ir


def test_dmn_task_names_match_the_supplied_model_summary() -> None:
    findings = validate_catalog(
        ir_with_dmn_task(),
        load_catalog(),
        {"monitoring-plan": dmn_summary()},
    )

    assert findings == []


def test_dmn_task_model_must_resolve_when_summaries_are_supplied() -> None:
    findings = validate_catalog(ir_with_dmn_task(), load_catalog(), {})

    assert "dmn-model" in codes(findings)


def test_dmn_task_inputs_and_outputs_must_match_model_variables() -> None:
    ir = ir_with_dmn_task()
    dmn = task_by_id(ir, "decide_monitoring")
    dmn.inputs.pop("Has Kidney Disease")
    dmn.outputs["Invented Output"] = "lab_order"

    findings = validate_catalog(ir, load_catalog(), {"monitoring-plan": dmn_summary()})

    assert {"dmn-input", "dmn-output"} <= codes(findings)


def xml_root() -> etree._Element:
    return etree.fromstring((FIXTURES / "home-bp-monitoring.bpmn").read_bytes())


def xml_findings(root: etree._Element) -> list:
    return validate_catalog_xml(
        etree.tostring(root, encoding="unicode"),
        load_catalog(),
    )


def taskname_assignment(root: etree._Element, task_id: str) -> etree._Element:
    task = root.xpath(f".//b:task[@id='{task_id}']", namespaces=NS)[0]
    data_input = task.xpath(
        "./b:ioSpecification/b:dataInput[@name='TaskName']", namespaces=NS
    )[0]
    input_id = data_input.get("id")
    return task.xpath(
        "./b:dataInputAssociation[b:targetRef=$input_id]/b:assignment/b:from",
        namespaces=NS,
        input_id=input_id,
    )[0]


def test_engine_verified_bpmn_golden_passes_xml_catalog_conformance() -> None:
    assert validate_catalog_xml(
        (FIXTURES / "home-bp-monitoring.bpmn").read_text(encoding="utf-8"),
        load_catalog(),
    ) == []


def test_xml_rejects_unknown_catalog_work_name() -> None:
    root = xml_root()
    task = root.xpath(".//b:task[@id='request_readings']", namespaces=NS)[0]
    task.set(f"{{{DROOLS_NS}}}taskName", "patient.unknown_action")

    assert "unknown-capability" in codes(xml_findings(root))


def test_xml_checks_taskname_literal_against_catalog_and_element_id() -> None:
    root = xml_root()
    taskname_assignment(root, "request_readings").text = "patient.request_observation"

    assert "task-name-label" in codes(xml_findings(root))


def test_xml_requires_one_taskname_input() -> None:
    root = xml_root()
    task = root.xpath(".//b:task[@id='request_readings']", namespaces=NS)[0]
    data_input = task.xpath(
        "./b:ioSpecification/b:dataInput[@name='TaskName']", namespaces=NS
    )[0]
    input_id = data_input.get("id")
    input_set = task.find("b:ioSpecification/b:inputSet", namespaces=NS)
    input_ref = input_set.xpath(
        "./b:dataInputRefs[text()=$input_id]", namespaces=NS, input_id=input_id
    )[0]
    input_set.remove(input_ref)
    task.find("b:ioSpecification", namespaces=NS).remove(data_input)
    association = task.xpath(
        "./b:dataInputAssociation[b:targetRef=$input_id]", namespaces=NS, input_id=input_id
    )[0]
    task.remove(association)

    assert "task-name-input" in codes(xml_findings(root))


def test_xml_taskname_literals_are_unique_within_a_process() -> None:
    root = xml_root()
    first = taskname_assignment(root, "request_readings").text
    taskname_assignment(root, "query_diastolic").text = first

    assert "task-name-unique" in codes(xml_findings(root))


def test_xml_taskname_uniqueness_includes_user_tasks() -> None:
    root = xml_root()
    process = root.find("b:process", namespaces=NS)
    existing = taskname_assignment(root, "request_readings").text
    user_task = etree.SubElement(process, f"{{{BPMN_NS}}}userTask", id="review")
    io_spec = etree.SubElement(user_task, f"{{{BPMN_NS}}}ioSpecification")
    etree.SubElement(
        io_spec,
        f"{{{BPMN_NS}}}dataInput",
        id="review_TaskName",
        name="TaskName",
    )
    association = etree.SubElement(user_task, f"{{{BPMN_NS}}}dataInputAssociation")
    etree.SubElement(association, f"{{{BPMN_NS}}}targetRef").text = "review_TaskName"
    assignment = etree.SubElement(association, f"{{{BPMN_NS}}}assignment")
    etree.SubElement(assignment, f"{{{BPMN_NS}}}from").text = existing
    etree.SubElement(assignment, f"{{{BPMN_NS}}}to").text = "review_TaskName"

    assert "task-name-unique" in codes(xml_findings(root))


def test_xml_rejects_kogito_reserved_work_item_name() -> None:
    root = xml_root()
    task = root.xpath(".//b:task[@id='request_readings']", namespaces=NS)[0]
    task.set(f"{{{DROOLS_NS}}}taskName", "Service Task")

    assert "reserved-task-name" in codes(xml_findings(root))


def test_invalid_xml_is_returned_as_an_l4_finding() -> None:
    findings = validate_catalog_xml("<bpmn2:definitions", load_catalog())

    assert len(findings) == 1
    assert findings[0].code == "xml-parse"
    assert findings[0].rung == "L4"


def test_catalog_validators_are_traced_when_mlflow_is_installed() -> None:
    pytest.importorskip("mlflow")

    assert getattr(validate_catalog, "__mlflow_traced__", False)
    assert getattr(validate_catalog_xml, "__mlflow_traced__", False)
