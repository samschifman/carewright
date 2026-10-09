"""Compiler expansion tests for the closed BPMN macro set."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler.kogito_profile import (
    normalize_duration,
    task_label,
)
from cpg_contracts.automation.compiler.expanded import XFlow, XNode, XProcess, XProperty
from cpg_contracts.automation.compiler.macros import (
    element_payload,
    expand,
    expand_counters,
    expand_main,
    template_payload,
    timer_expression,
)
from cpg_contracts.automation.expressions import ExpressionError
from cpg_contracts.automation.ir import AcpElement, ProcessIR, Task


FIXTURE = Path(__file__).parents[1] / "fixtures/automation/home-bp-monitoring.ir.json"


def fixture_data() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def fixture_ir(data: dict[str, Any] | None = None) -> ProcessIR:
    return ProcessIR.model_validate(data if data is not None else fixture_data())


def find_node(process, element_id: str):
    pending = list(process.nodes)
    found = []
    while pending:
        node = pending.pop()
        if node.id == element_id:
            found.append(node)
        pending.extend(node.children)
    assert len(found) == 1, f"expected one node {element_id!r}, found {len(found)}"
    return found[0]


def all_flows(process):
    pending = [*process.nodes]
    yield from process.flows
    while pending:
        node = pending.pop()
        yield from node.flows
        pending.extend(node.children)


def expanded(data: dict[str, Any] | None = None):
    return expand(fixture_ir(data), load_catalog(), {})


def test_counter_without_resets_initializes_and_increments() -> None:
    process = expanded()

    init = find_node(process, "init")
    counter = find_node(process, "remind_counter")
    assert init.script == (
        'kcontext.setVariable("reminder_attempts", 0); '
        'kcontext.setVariable("outcome", "");'
    )
    assert counter.script == (
        'kcontext.setVariable("reminder_attempts", '
        '((Integer) kcontext.getVariable("reminder_attempts")) + 1);'
    )
    assert (
        next(flow for flow in all_flows(process) if flow.id == "remind_counter_in").source_ref
        == "remind"
    )
    assert (
        next(flow for flow in all_flows(process) if flow.id == "f_attempts").source_ref
        == "remind_counter"
    )
    assert counter.name == "Count reminder attempts"


def test_resets_on_a_flow_insert_a_script_after_the_condition() -> None:
    process = expanded()
    reset = find_node(process, "f_some_reset")
    flows = {flow.id: flow for flow in all_flows(process)}

    assert reset.name == "Reset reminder attempts"
    assert reset.script == 'kcontext.setVariable("reminder_attempts", 0);'
    assert flows["f_some"].target_ref == "f_some_reset"
    assert flows["f_some"].condition_expression is not None
    assert flows["f_some_reset_out"].source_ref == "f_some_reset"
    assert flows["f_some_reset_out"].target_ref == "gw_threshold"


def test_main_wraps_one_and_two_outcomes() -> None:
    one_outcome = expanded()
    one_gateway = find_node(one_outcome, "gw_outcome")
    one_routes = [
        flow
        for flow in one_outcome.flows
        if flow.source_ref == "gw_outcome" and flow.target_ref != "end_completed"
    ]
    assert one_gateway.attributes["default"] == "f_completed"
    assert len(one_routes) == 1
    assert one_routes[0].target_ref == "end_escalated"
    assert one_routes[0].condition_is_java is True
    assert one_routes[0].condition_expression == 'return "escalated".equals(outcome);'
    assert find_node(one_outcome, "main").type == "subProcess"
    assert find_node(one_outcome, "main_end_escalated").type == "endEvent"
    assert find_node(one_outcome, "end_escalated_outcome").type == "scriptTask"
    child_flows = {flow.id: flow for flow in find_node(one_outcome, "main").flows}
    assert child_flows["f_end_escalated"].target_ref == "end_escalated_outcome"
    assert child_flows["end_escalated_outcome_out"].target_ref == "main_end_escalated"
    assert (
        next(flow for flow in one_outcome.flows if flow.id == "end_escalated_route").target_ref
        == "end_escalated"
    )

    data = fixture_data()
    data["process"]["flowElements"].append(
        {
            "type": "endEvent",
            "id": "end_closed",
            "name": "Closed",
            "acp": {
                "outcome": "closed",
                "provenance": {
                    "kind": "structural",
                    "derivation_rule": "end-event",
                    "supports": ["escalate"],
                },
            },
        }
    )
    data["process"]["flowElements"].append(
        {
            "type": "sequenceFlow",
            "id": "f_closed",
            "sourceRef": "gw_attempts",
            "targetRef": "end_closed",
            "conditionExpression": "reminder_attempts > 10",
        }
    )
    two_outcomes = expanded(data)
    routes = [
        flow
        for flow in two_outcomes.flows
        if flow.source_ref == "gw_outcome" and flow.target_ref != "end_completed"
    ]
    assert {flow.target_ref for flow in routes} == {"end_escalated", "end_closed"}
    assert all(flow.condition_is_java for flow in routes)


def test_main_expansion_requires_the_ir_boundary_instead_of_synthesizing_it() -> None:
    process = expanded()
    process.nodes = [node for node in process.nodes if node.id != "period_elapsed"]
    process.flows = [flow for flow in process.flows if flow.id != "f_period"]

    with pytest.raises(ValueError, match="exactly one boundary attached to 'main'"):
        expand_main(process)


def test_generated_id_collision_raises_instead_of_renaming() -> None:
    ir = fixture_ir()
    process = XProcess(
        id="collision_probe",
        name="Collision probe",
        definitions_id="collision_probe_defs",
        target_namespace="urn:collision-probe",
        version="1",
        catalog_version="1.0",
        profile_version="kogito-10.2",
        ir=ir,
        properties=[
            XProperty(
                id="reminder_attempts",
                name="reminder_attempts",
                type="integer",
                structure_ref="Integer",
            )
        ],
        nodes=[
            XNode(type="startEvent", id="start"),
            XNode(type="task", id="probe", counter="reminder_attempts"),
            XNode(type="task", id="probe_counter"),
        ],
        flows=[XFlow(id="f_probe", source_ref="probe", target_ref="start")],
    )

    with pytest.raises(ValueError, match="generated BPMN id 'probe_counter' collides"):
        expand_counters(process)


def test_custom_task_io_contains_exactly_ir_inputs_and_mapped_outputs() -> None:
    process = expanded()
    task = find_node(process, "query_diastolic")
    inputs = {io.name for io in task.io if io.direction == "input"}
    outputs = {io.name for io in task.io if io.direction == "output"}

    assert inputs == {"code", "window", "unit", "TaskName"}
    assert outputs == {"average_value"}


def test_timer_literals_normalize_weeks_and_reject_months() -> None:
    assert normalize_duration("P1W") == "P7D"
    assert normalize_duration("P2W") == "P14D"
    assert timer_expression({"param": "reporting_interval"}) == "#{reporting_interval}"
    assert timer_expression({"literal": "P1W", "type": "duration"}) == "P7D"
    with pytest.raises(ExpressionError, match="months and years"):
        normalize_duration("P1M")
    with pytest.raises(ExpressionError, match="fractional week durations"):
        normalize_duration("P1.5W")


def test_quantity_properties_preserve_their_acp_unit() -> None:
    process = expanded()
    systolic = next(prop for prop in process.properties if prop.id == "avg_systolic")

    assert systolic.type == "quantity"
    assert systolic.structure_ref == "Double"
    assert systolic.unit == "mm[Hg]"


def test_repeated_capabilities_keep_work_name_and_derive_unique_task_names() -> None:
    process = expanded()
    systolic = find_node(process, "query_systolic")
    diastolic = find_node(process, "query_diastolic")

    assert systolic.work_name == diastolic.work_name == "ehr.query_observations"
    systolic_task_name = next(io for io in systolic.io if io.name == "TaskName")
    diastolic_task_name = next(io for io in diastolic.io if io.name == "TaskName")
    assert systolic_task_name.literal == task_label("ehr.query_observations", "query_systolic")
    assert diastolic_task_name.literal == task_label("ehr.query_observations", "query_diastolic")
    assert systolic_task_name.literal != diastolic_task_name.literal


def test_user_task_task_name_is_its_element_id() -> None:
    data = fixture_data()
    user_task = next(
        element
        for element in data["process"]["flowElements"]
        if element["id"] == "request_readings"
    )
    user_task.pop("taskName", None)
    user_task.pop("inputs", None)
    user_task["type"] = "userTask"
    user_task["groupId"] = "patients"
    process = expanded(data)
    node = find_node(process, "request_readings")

    assert node.type == "userTask"
    assert next(io for io in node.io if io.name == "TaskName").literal == "request_readings"
    assert next(io for io in node.io if io.name == "Skippable").literal == "false"
    assert next(io for io in node.io if io.name == "GroupId").literal == "patients"


def test_prefixed_property_names_are_preserved_verbatim() -> None:
    data = fixture_data()
    for prop in data["process"]["properties"]:
        if prop["name"] == "reading_count":
            prop["name"] = "confirm__reading_count"
    for element in data["process"]["flowElements"]:
        if element["id"] == "query_systolic":
            element["outputs"]["count"] = "confirm__reading_count"
        if element["id"] == "f_some":
            element["conditionExpression"] = (
                "defined(confirm__reading_count) and confirm__reading_count > 0"
            )
    process = expanded(data)

    prop = next(prop for prop in process.properties if prop.id == "confirm__reading_count")
    assert prop.name == "confirm__reading_count"
    count_io = next(io for io in find_node(process, "query_systolic").io if io.name == "count")
    assert count_io.target_ref == "confirm__reading_count"


def test_dmn_expansion_binds_namespace_and_model_inputs() -> None:
    data = fixture_data()
    dmn_task = next(
        element
        for element in data["process"]["flowElements"]
        if element["id"] == "query_systolic"
    )
    dmn_task.pop("taskName", None)
    dmn_task["type"] = "businessRuleTask"
    dmn_task["dmnModel"] = "bp-risk"
    dmn_task["inputs"] = {"patient_age": {"property": "reading_count"}}
    dmn_task["outputs"] = {"risk": "reading_count"}
    process = expand(fixture_ir(data), load_catalog(), {"bp-risk": "urn:bp-risk"})
    node = find_node(process, "query_systolic")

    assert node.implementation == "http://www.jboss.org/drools/dmn"
    assert {io.name: io.literal for io in node.io if io.direction == "input"} == {
        "namespace": "urn:bp-risk",
        "model": "bp-risk",
        "patient_age": None,
    }
    assert next(io for io in node.io if io.name == "risk").target_ref == "reading_count"


def test_element_payload_is_stable_compact_json() -> None:
    task = next(
        element
        for element in fixture_ir().process.flowElements
        if isinstance(element, Task) and element.id == "request_readings"
    )
    payload = element_payload(task)

    assert json.loads(payload) == task.acp.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
    assert "structural_constant" not in json.loads(payload)
    assert payload == json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_conditional_flow_payload_includes_source_expression() -> None:
    flow = next(
        element
        for element in fixture_ir().process.flowElements
        if element.id == "f_some"
    )

    assert json.loads(element_payload(flow))["conditionExpression"] == flow.conditionExpression


def test_empty_element_payload_is_empty_object() -> None:
    from cpg_contracts.automation.ir import StartEvent

    assert element_payload(StartEvent(type="startEvent", id="start", name="Start")) == "{}"
    assert (
        element_payload(
            StartEvent(
                type="startEvent",
                id="start",
                name="Start",
                acp=AcpElement(resets=[]),
            )
        )
        == "{}"
    )


def test_template_payload_carries_process_identity_and_full_acp() -> None:
    ir = fixture_ir()
    payload = template_payload(ir, load_catalog())

    assert payload["kind"] == ir.process.kind
    assert payload["description"] == ir.process.description
    assert payload["triggers"] == ir.process.acp.model_dump(mode="json", exclude_none=True)["triggers"]


def test_expansion_does_not_mutate_the_input_ir() -> None:
    ir = fixture_ir()
    before = copy.deepcopy(ir.model_dump(mode="json"))

    expand(ir, load_catalog(), {})

    assert ir.model_dump(mode="json") == before
