"""Contract tests for the BPMN-shaped process IR."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cpg_contracts.automation.ir import (
    AcpElement,
    BoundaryEvent,
    Justification,
    ProcessIR,
    SourceRef,
    Task,
    TimerEventDefinition,
    automation_template_id,
    canonical_json,
    ir_version,
)


FIXTURE = Path(__file__).parents[1] / "fixtures/automation/home-bp-monitoring.ir.json"


def fixture_data() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def invalid(mutator, message: str | None = None) -> None:
    data = fixture_data()
    mutator(data)
    with pytest.raises(ValidationError) as exc_info:
        ProcessIR.model_validate(data)
    if message:
        assert message in str(exc_info.value)


def flow(data: dict[str, Any], element_id: str) -> dict[str, Any]:
    return next(
        item for item in data["process"]["flowElements"] if item["id"] == element_id
    )


def rename_element(data: dict[str, Any], old: str, new: str) -> None:
    flow(data, old)["id"] = new
    for item in data["process"]["flowElements"]:
        for field in ("sourceRef", "targetRef", "attachedToRef"):
            if item.get(field) == old:
                item[field] = new


def test_appendix_b_fixture_round_trips_through_canonical_json() -> None:
    ir = ProcessIR.model_validate_json(FIXTURE.read_text())
    parsed = ProcessIR.model_validate_json(canonical_json(ir))
    assert canonical_json(parsed) == canonical_json(ir)
    assert [element.id for element in parsed.process.flowElements] == sorted(
        element.id for element in parsed.process.flowElements
    )
    assert len(ir.process.flowElements) == 31


def test_canonical_json_and_hash_ignore_object_key_order() -> None:
    def reverse_keys(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: reverse_keys(child) for key, child in reversed(list(value.items()))}
        if isinstance(value, list):
            return [reverse_keys(child) for child in value]
        return value

    ir = ProcessIR.model_validate(fixture_data())
    reordered = ProcessIR.model_validate(reverse_keys(fixture_data()))
    assert canonical_json(ir) == canonical_json(reordered)
    assert ir_version(ir) == ir_version(reordered)
    assert len(ir_version(ir)) == 12


def test_canonical_json_and_hash_ignore_flow_element_order() -> None:
    data = fixture_data()
    reordered_data = copy.deepcopy(data)
    reordered_data["process"]["flowElements"].reverse()
    ir = ProcessIR.model_validate(data)
    reordered = ProcessIR.model_validate(reordered_data)

    assert canonical_json(ir) == canonical_json(reordered)
    assert ir_version(ir) == ir_version(reordered)


def test_template_id_uses_the_decision_model_slug_rule() -> None:
    assert automation_template_id("Home BP Monitoring") == "home-bp-monitoring"
    assert automation_template_id("!!!") == "unknown"


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda d: d["process"]["flowElements"].remove(flow(d, "start")),
            "exactly one startEvent",
        ),
        (
            lambda d: d["process"]["flowElements"].append(
                {"type": "startEvent", "id": "start_extra", "name": "Extra start"}
            ),
            "exactly one startEvent",
        ),
        (
            lambda d: d["process"]["flowElements"].remove(flow(d, "end_escalated"))
            or d["process"]["flowElements"].remove(flow(d, "end_period")),
            "at least one endEvent",
        ),
        (
            lambda d: d["process"]["flowElements"].append(
                {"type": "endEvent", "id": "end_escalated", "name": "Duplicate id"}
            ),
            "duplicate element id",
        ),
        (
            lambda d: d["process"]["flowElements"].append(
                {
                    "type": "sequenceFlow",
                    "id": "f_implicit_join",
                    "sourceRef": "query_systolic",
                    "targetRef": "query_diastolic",
                }
            ),
            "implicit join",
        ),
        (
            lambda d: flow(d, "f_start").update(sourceRef="missing_node"),
            "sourceRef 'missing_node' does not resolve",
        ),
        (
            lambda d: flow(d, "f_start").update(targetRef="missing_node"),
            "targetRef 'missing_node' does not resolve",
        ),
        (
            lambda d: flow(d, "period_elapsed").update(attachedToRef="missing_node"),
            "attachedToRef 'missing_node' does not resolve",
        ),
        (
            lambda d: flow(d, "gw_received").update(default=None),
            "requires exactly one default flow",
        ),
        (
            lambda d: flow(d, "gw_received").update(default=["f_some", "f_none"]),
            "Input should be a valid string",
        ),
        (
            lambda d: flow(d, "gw_received").update(default="f_loop"),
            "must resolve to its outgoing flow",
        ),
        (
            lambda d: flow(d, "f_some").update(conditionExpression=None),
            "requires conditionExpression",
        ),
        (
            lambda d: flow(d, "f_none").update(conditionExpression="false"),
            "default flow 'f_none' must not have a conditionExpression",
        ),
        (
            lambda d: flow(d, "gw_received").update(name=""),
            "element 'gw_received' (exclusiveGateway) requires a name",
        ),
        (
            lambda d: d["process"]["acp"]["parameters"].remove(
                next(p for p in d["process"]["acp"]["parameters"] if p["name"] == "max_duration")
            ),
            "requires exactly one reserved parameter 'max_duration'",
        ),
        (
            lambda d: [
                d["process"]["flowElements"].remove(flow(d, element_id))
                for element_id in ("period_elapsed", "f_period")
            ],
            "requires exactly one boundaryEvent attached to 'main'",
        ),
        (
            lambda d: d["process"]["flowElements"].append(
                {**copy.deepcopy(flow(d, "period_elapsed")), "id": "period_elapsed_2"}
            ),
            "requires exactly one boundaryEvent attached to 'main'",
        ),
        (
            lambda d: d["process"]["acp"].update(bindings=[]),
            "Extra inputs are not permitted",
        ),
        (
            lambda d: flow(d, "request_readings")["inputs"].update(
                instructions={"literal": "please measure", "type": "string"}
            ),
            "string literals are not allowed",
        ),
        (
            lambda d: flow(d, "request_readings")["inputs"].update(
                due={"literal": "P1W", "type": "duration"}
            ),
            "duration literals must use day/hour form",
        ),
        (
            lambda d: flow(d, "wait_interval")["timerEventDefinition"].update(
                timeDuration={"literal": "P1W", "type": "duration"}
            ),
            "duration literals must use day/hour form",
        ),
        (
            lambda d: d["process"]["properties"][1].pop("unit"),
            "quantity property 'avg_systolic' requires unit",
        ),
        (
            lambda d: flow(d, "period_elapsed").update(attachedToRef="start"),
            "boundaryEvent 'period_elapsed'",
        ),
        (
            lambda d: flow(d, "period_elapsed")["timerEventDefinition"].update(
                timeDuration={"param": "reporting_interval"}
            ),
            "must use timeDuration {param: 'max_duration'}",
        ),
        (
            lambda d: flow(d, "period_elapsed")["acp"]["provenance"].update(
                derivation_rule="end-event"
            ),
            "requires structural provenance 'plan-bound'",
        ),
        (
            lambda d: d["process"]["acp"]["parameters"][0].update(source=None),
            "default requires source",
        ),
        (
            lambda d: d["process"]["acp"]["parameters"][0].update(
                required=False, reserved=True
            ),
            "cannot have a default",
        ),
        (
            lambda d: flow(d, "period_elapsed")["timerEventDefinition"].update(
                timeCycle={"param": "reporting_interval"}
            ),
            "exactly one of timeDuration or timeCycle",
        ),
    ],
)
def test_invalid_structural_variants_are_rejected(mutator, message: str) -> None:
    invalid(mutator, message)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda d: rename_element(d, "request_readings", "confirm.request_readings"),
            "must match ^[a-z][a-z0-9_]*$",
        ),
        (
            lambda d: rename_element(d, "request_readings", "Default_flow"),
            "must match ^[a-z][a-z0-9_]*$",
        ),
        (
            lambda d: d["process"]["properties"].append(
                {"name": "default", "type": "integer"}
            ),
            "property name 'default' is a Java keyword or literal",
        ),
        (
            lambda d: rename_element(d, "request_readings", "init"),
            "element id 'init' is compiler-reserved",
        ),
        (
            lambda d: rename_element(d, "f_start", "f_init"),
            "element id 'f_init' is compiler-reserved",
        ),
        (
            lambda d: rename_element(d, "f_start", "f_main"),
            "element id 'f_main' is compiler-reserved",
        ),
        (
            lambda d: rename_element(d, "f_start", "f_after"),
            "element id 'f_after' is compiler-reserved",
        ),
        (
            lambda d: rename_element(d, "f_start", "f_completed"),
            "element id 'f_completed' is compiler-reserved",
        ),
        (
            lambda d: rename_element(d, "request_readings", "main_inner"),
            "uses compiler-reserved prefix 'main_'",
        ),
        (
            lambda d: rename_element(d, "request_readings", "request_counter_suffix"),
            "contains compiler-reserved sequence '_counter'",
        ),
        (
            lambda d: rename_element(d, "request_readings", "request_reset_suffix"),
            "contains compiler-reserved sequence '_reset'",
        ),
        (
            lambda d: rename_element(d, "request_readings", "request_outcome_suffix"),
            "contains compiler-reserved sequence '_outcome'",
        ),
        (
            lambda d: rename_element(d, "request_readings", "request_route_suffix"),
            "contains compiler-reserved sequence '_route'",
        ),
        (
            lambda d: d["process"]["properties"].append(
                {"name": "outcome", "type": "string"}
            ),
            "property name 'outcome' is compiler-reserved",
        ),
    ],
)
def test_invalid_identifiers_are_rejected(mutator, message: str) -> None:
    invalid(mutator, message)


def test_prefixed_fragment_identifier_is_accepted_verbatim() -> None:
    data = fixture_data()
    flow(data, "request_readings")["id"] = "confirm__request_readings"
    flow(data, "f_loop")["targetRef"] = "confirm__request_readings"
    flow(data, "f_wait")["sourceRef"] = "confirm__request_readings"
    ir = ProcessIR.model_validate(data)
    task = next(
        item for item in ir.process.flowElements if item.id == "confirm__request_readings"
    )
    assert isinstance(task, Task)
    assert task.id == "confirm__request_readings"


def test_duplicate_boundary_names_are_allowed_at_l0() -> None:
    data = fixture_data()
    duplicate = copy.deepcopy(flow(data, "period_elapsed"))
    duplicate["id"] = "period_elapsed_2"
    duplicate["attachedToRef"] = "query_systolic"
    data["process"]["flowElements"].append(duplicate)
    ir = ProcessIR.model_validate(data)
    boundaries = [
        item for item in ir.process.flowElements if isinstance(item, BoundaryEvent)
    ]
    assert [event.name for event in boundaries] == [
        "Max duration elapsed",
        "Max duration elapsed",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "cpg"},
        {"kind": "reviewer", "note": "edited"},
        {"kind": "policy"},
    ],
)
def test_source_ref_requires_fields_for_kind(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SourceRef.model_validate(payload)


def test_timer_definition_rejects_neither_choice() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        TimerEventDefinition.model_validate({})


def test_plan_sequence_justification_matches_composition_shape() -> None:
    justification = Justification.model_validate(
        {
            "kind": "plan-sequence",
            "evidence_id": "ev-seq-1",
        }
    )
    assert justification.evidence_id == "ev-seq-1"


def test_justification_requires_evidence_id() -> None:
    with pytest.raises(ValidationError, match="evidence_id"):
        Justification.model_validate({"kind": "plan-sequence"})


def test_acp_element_defaults_are_safe_and_empty() -> None:
    assert AcpElement().structural_constant is False
