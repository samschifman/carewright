"""Tests for deterministic compilation to the supported BPMN profile."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lxml import etree

from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR
from cpg_contracts.automation.validators.preflight import validate_preflight
from cpg_contracts.automation.validators.xsd import validate_xsd


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"
IR_FIXTURE = FIXTURES / "home-bp-monitoring.ir.json"
GOLDEN = FIXTURES / "home-bp-monitoring.bpmn"
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
NS = {"bpmn2": BPMN_NS}


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(IR_FIXTURE.read_text(encoding="utf-8"))


def test_compiler_output_matches_engine_verified_golden_byte_for_byte() -> None:
    xml = compile(fixture_ir())

    assert xml == GOLDEN.read_text(encoding="utf-8")
    assert validate_preflight(xml) == []
    assert validate_xsd(xml) == []


def test_compilation_is_deterministic_and_contains_no_binding_token() -> None:
    ir = fixture_ir()
    first = compile(ir)
    second = compile(ir)
    binding_sentinel = "patient-specific-value-must-not-enter-the-definition"

    assert first == second
    assert binding_sentinel not in first


def test_compilation_ignores_mapping_key_order() -> None:
    def reverse_mapping_order(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: reverse_mapping_order(child)
                for key, child in reversed(list(value.items()))
            }
        if isinstance(value, list):
            return [reverse_mapping_order(child) for child in value]
        return value

    original = fixture_ir()
    reordered = ProcessIR.model_validate(reverse_mapping_order(json.loads(IR_FIXTURE.read_text())))

    assert compile(original) == compile(reordered)


def test_compilation_ignores_flow_element_order() -> None:
    original = fixture_ir()
    reordered_data = json.loads(IR_FIXTURE.read_text())
    reordered_data["process"]["flowElements"].reverse()
    reordered = ProcessIR.model_validate(reordered_data)

    assert compile(original) == compile(reordered)


def test_task_io_contains_only_ir_mappings_plus_taskname() -> None:
    root = etree.fromstring(compile(fixture_ir()).encode("utf-8"))
    tasks = {
        element.get("id"): element
        for element in root.xpath(".//bpmn2:task", namespaces=NS)
    }

    def io_names(task_id: str, direction: str) -> set[str]:
        return set(
            tasks[task_id].xpath(
                f"./bpmn2:ioSpecification/bpmn2:data{direction}/@name",
                namespaces=NS,
            )
        )

    assert io_names("request_readings", "Input") == {
        "code",
        "instructions",
        "due",
        "TaskName",
    }
    assert io_names("request_readings", "Output") == set()
    assert io_names("query_diastolic", "Input") == {
        "code",
        "window",
        "unit",
        "TaskName",
    }
    assert io_names("query_diastolic", "Output") == {"average_value"}

    process_properties = set(
        root.xpath(".//bpmn2:process/bpmn2:property/@name", namespaces=NS)
    )
    assert process_properties == {
        parameter.name for parameter in fixture_ir().process.acp.parameters
    } | {prop.name for prop in fixture_ir().process.properties} | {"outcome"}
    assert "latest_date" not in process_properties


def test_catalog_work_name_and_taskname_literal_are_serialized_separately() -> None:
    root = etree.fromstring(compile(fixture_ir()).encode("utf-8"))
    tasks = {
        element.get("id"): element
        for element in root.xpath(".//bpmn2:task", namespaces=NS)
    }
    task = tasks["request_readings"]
    task_name = task.xpath(
        ".//bpmn2:dataInput[@name='TaskName']/@id", namespaces=NS
    )[0]
    assignment = task.xpath(
        f".//bpmn2:dataInputAssociation[bpmn2:targetRef='{task_name}']/bpmn2:assignment/bpmn2:from/text()",
        namespaces=NS,
    )[0]

    assert task.get("{http://www.jboss.org/drools}taskName") == "patient.request_observation"
    assert assignment == "patient_request_observation__request_readings"


def test_template_payload_is_canonical_json() -> None:
    root = etree.fromstring(compile(fixture_ir()).encode("utf-8"))
    payload = root.xpath(
        ".//bpmn2:process/bpmn2:extensionElements/acp:template/text()",
        namespaces={**NS, "acp": "https://github.com/samschifman/cpg-to-acp/bpmn"},
    )[0]
    decoded = json.loads(payload)

    assert decoded["catalog_version"] == "1.0"
    assert decoded["template_id"] == "home-bp-monitoring"
    assert "bindings" not in decoded
