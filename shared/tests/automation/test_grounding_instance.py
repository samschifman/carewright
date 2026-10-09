"""Instance grounding checks against selected templates and external evidence."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError
import pytest

from cpg_contracts.automation.ir import (
    BoundaryEvent,
    Justification,
    Literal_,
    PruneRecord,
    Produces,
    ProcessIR,
    Property,
    StructuralProvenance,
    TemplateRef,
    Trigger,
    UnifyRecord,
)
from cpg_contracts.automation.validators.grounding import (
    Evidence,
    InstanceContext,
    ParameterBinding,
    validate_instance,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(
        (FIXTURES / "home-bp-monitoring.ir.json").read_text(encoding="utf-8")
    )


def bindings_for(ir: ProcessIR, *, max_duration: str = "P28D") -> list[ParameterBinding]:
    defaults = {
        "reporting_interval": "P7D",
        "notify_systolic_threshold": 135,
        "notify_diastolic_threshold": 85,
        "max_reminder_attempts": 3,
        "instructions_text": "Measure twice each morning and evening.",
        "reminder_text": "Please submit this week's home blood pressure readings.",
        "escalation_reason": "No readings after reminders.",
        "notify_reason": "Average above threshold.",
        "max_duration": max_duration,
    }
    result = []
    for parameter in ir.process.acp.parameters:
        value = max_duration if parameter.name == "max_duration" else parameter.default
        if value is None:
            value = defaults.get(parameter.name)
        source_kind = getattr(parameter.source, "kind", None)
        source = (
            "plan-derived"
            if parameter.name == "max_duration"
            else "authored"
            if source_kind == "authored"
            else "cpg-default"
        )
        result.append(ParameterBinding(name=parameter.name, value=value, source=source))
    return result


def make_instance() -> tuple[ProcessIR, ProcessIR, InstanceContext]:
    template = fixture_ir()
    instance = deepcopy(template)
    instance.process.id = "monitoring_instance"
    instance.process.kind = "instance"
    ref = TemplateRef(template_id=template.process.id, version="v1")
    instance.process.acp.template_refs = [ref]
    for element in instance.process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=ref.template_id,
            version=ref.version,
            element_id=element.id,
        )
        if isinstance(element.acp.provenance, StructuralProvenance) and not (
            isinstance(element, BoundaryEvent)
            and element.acp.provenance.derivation_rule == "plan-bound"
        ):
            element.acp.provenance = None
    return instance, template, InstanceContext(bindings=bindings_for(instance))


def simple_template(template_id: str, name: str, task_id: str) -> ProcessIR:
    end_id = f"end_{task_id}"
    timeout_end_id = f"timeout_{task_id}"
    period_id = f"period_{task_id}"
    return ProcessIR.model_validate(
        {
            "ir_version": "1.0",
            "process": {
                "id": template_id,
                "kind": "template",
                "name": name,
                "properties": [],
                "flowElements": [
                    {
                        "type": "startEvent",
                        "id": f"start_{task_id}",
                        "name": f"Start {name}",
                        "acp": {
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "start-event",
                                "supports": [task_id],
                            }
                        },
                    },
                    {
                        "type": "task",
                        "id": task_id,
                        "name": f"{name} step",
                        "taskName": "patient.request_observation",
                        "acp": {
                            "provenance": {
                                "kind": "source",
                                "source_text": "The patient submits readings weekly.",
                            }
                        },
                    },
                    {
                        "type": "endEvent",
                        "id": end_id,
                        "name": f"{name} complete",
                        "acp": {
                            "outcome": "complete",
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "end-event",
                                "supports": [task_id],
                            },
                        },
                    },
                    {
                        "type": "endEvent",
                        "id": timeout_end_id,
                        "name": f"{name} timed out",
                        "acp": {
                            "outcome": "timeout",
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "end-event",
                                "supports": [period_id],
                            },
                        },
                    },
                    {
                        "type": "boundaryEvent",
                        "id": period_id,
                        "name": f"{name} maximum duration elapsed",
                        "attachedToRef": "main",
                        "timerEventDefinition": {
                            "timeDuration": {"param": "max_duration"}
                        },
                        "acp": {
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "plan-bound",
                                "supports": [task_id],
                            }
                        },
                    },
                    {
                        "type": "sequenceFlow",
                        "id": f"flow_start_{task_id}",
                        "sourceRef": f"start_{task_id}",
                        "targetRef": task_id,
                    },
                    {
                        "type": "sequenceFlow",
                        "id": f"flow_done_{task_id}",
                        "sourceRef": task_id,
                        "targetRef": end_id,
                    },
                    {
                        "type": "sequenceFlow",
                        "id": f"flow_timeout_{task_id}",
                        "sourceRef": period_id,
                        "targetRef": timeout_end_id,
                    },
                ],
                "acp": {
                    "triggers": [
                        {"kind": "recommendation", "recommendation_id": f"rec-{template_id}"}
                    ],
                    "parameters": [
                        {
                            "name": "max_duration",
                            "type": "duration",
                            "required": True,
                            "reserved": True,
                        }
                    ],
                    "invariants": ["max_duration >= P7D"],
                    "provenance": {
                        "source_cpg": "SYN-HTN-2026-002",
                        "section": name,
                    },
                },
            },
        }
    )


def make_composed_instance(
    *, max_duration: str = "P90D", justification: bool = True
) -> tuple[ProcessIR, list[ProcessIR], InstanceContext]:
    template_a = simple_template("confirmation", "Confirm readings", "confirm")
    template_b = simple_template("monitoring", "Monitor readings", "monitor")
    ref_a = TemplateRef(template_id="confirmation", version="a1")
    ref_b = TemplateRef(template_id="monitoring", version="b1")
    provenance: dict = {
        "kind": "structural",
        "derivation_rule": "fragment-boundary",
        "supports": ["a__confirm", "b__monitor"],
        "replaces": {"template_id": "confirmation", "element_id": "end_confirm"},
    }
    if justification:
        provenance["justification"] = {
            "kind": "plan-sequence",
            "evidence_id": "ev-sequence",
        }
    instance = ProcessIR.model_validate(
        {
            "ir_version": "1.0",
            "process": {
                "id": "composed_instance",
                "kind": "instance",
                "name": "Confirm readings, then Monitor readings",
                "properties": [],
                "flowElements": [
                    {
                        "type": "startEvent",
                        "id": "a__start_confirm",
                        "name": "Start Confirm readings",
                        "acp": {
                            "template_ref": {
                                "template_id": "confirmation",
                                "version": "a1",
                                "element_id": "start_confirm",
                            },
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "start-event",
                                "supports": ["a__confirm"],
                            },
                        },
                    },
                    {
                        "type": "task",
                        "id": "a__confirm",
                        "name": "Confirm readings step",
                        "taskName": "patient.request_observation",
                        "acp": {
                            "template_ref": {
                                "template_id": "confirmation",
                                "version": "a1",
                                "element_id": "confirm",
                            },
                            "provenance": {
                                "kind": "source",
                                "source_text": "The patient submits readings weekly.",
                            },
                        },
                    },
                    {
                        "type": "task",
                        "id": "b__monitor",
                        "name": "Monitor readings step",
                        "taskName": "patient.request_observation",
                        "acp": {
                            "template_ref": {
                                "template_id": "monitoring",
                                "version": "b1",
                                "element_id": "monitor",
                            },
                            "provenance": {
                                "kind": "source",
                                "source_text": "The patient submits readings weekly.",
                            },
                        },
                    },
                    {
                        "type": "endEvent",
                        "id": "b__end_monitor",
                        "name": "Monitor readings complete",
                        "acp": {
                            "template_ref": {
                                "template_id": "monitoring",
                                "version": "b1",
                                "element_id": "end_monitor",
                            },
                            "outcome": "complete",
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "end-event",
                                "supports": ["b__monitor"],
                            },
                        },
                    },
                    {
                        "type": "endEvent",
                        "id": "b__timeout_monitor",
                        "name": "Monitor readings timed out",
                        "acp": {
                            "template_ref": {
                                "template_id": "monitoring",
                                "version": "b1",
                                "element_id": "timeout_monitor",
                            },
                            "outcome": "timeout",
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "end-event",
                                "supports": ["period_elapsed"],
                            },
                        },
                    },
                    {
                        "type": "boundaryEvent",
                        "id": "period_elapsed",
                        "name": "Monitor readings maximum duration elapsed",
                        "attachedToRef": "main",
                        "timerEventDefinition": {
                            "timeDuration": {"param": "max_duration"}
                        },
                        "acp": {
                            "template_ref": {
                                "template_id": "monitoring",
                                "version": "b1",
                                "element_id": "period_monitor",
                            },
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "plan-bound",
                                "supports": ["b__monitor"],
                            },
                        },
                    },
                    {
                        "type": "sequenceFlow",
                        "id": "a__flow_start_confirm",
                        "sourceRef": "a__start_confirm",
                        "targetRef": "a__confirm",
                        "acp": {
                            "template_ref": {
                                "template_id": "confirmation",
                                "version": "a1",
                                "element_id": "flow_start_confirm",
                            }
                        },
                    },
                    {
                        "type": "sequenceFlow",
                        "id": "a_to_b",
                        "sourceRef": "a__confirm",
                        "targetRef": "b__monitor",
                        "acp": {"provenance": provenance},
                    },
                    {
                        "type": "sequenceFlow",
                        "id": "b__flow_done_monitor",
                        "sourceRef": "b__monitor",
                        "targetRef": "b__end_monitor",
                        "acp": {
                            "template_ref": {
                                "template_id": "monitoring",
                                "version": "b1",
                                "element_id": "flow_done_monitor",
                            }
                        },
                    },
                    {
                        "type": "sequenceFlow",
                        "id": "b__flow_timeout_monitor",
                        "sourceRef": "period_elapsed",
                        "targetRef": "b__timeout_monitor",
                        "acp": {
                            "template_ref": {
                                "template_id": "monitoring",
                                "version": "b1",
                                "element_id": "flow_timeout_monitor",
                            }
                        },
                    },
                ],
                "acp": {
                    "triggers": [
                        {"kind": "recommendation", "recommendation_id": "rec-confirmation"}
                    ],
                    "parameters": [
                        {
                            "name": "max_duration",
                            "type": "duration",
                            "required": True,
                            "reserved": True,
                        }
                    ],
                    "provenance": {"source_cpg": "SYN-HTN-2026-002"},
                    "template_refs": [
                        {"template_id": "confirmation", "version": "a1"},
                        {"template_id": "monitoring", "version": "b1"},
                    ],
                    "consolidated_boundaries": [
                        {"template_id": "confirmation", "element_id": "period_confirm"}
                    ],
                },
            },
        }
    )
    context = InstanceContext(
        bindings=[
            ParameterBinding(
                name="max_duration", value=max_duration, source="plan-derived"
            )
        ],
        evidence={
            "ev-sequence": Evidence(
                kind="plan-sequence",
                payload={
                    "activity_ids": ["act-confirm", "act-monitor"],
                    "sequence_after": "Confirm readings",
                },
            )
        },
        activities=[
            {"id": "act-confirm", "name": "Confirm readings"},
            {
                "id": "act-monitor",
                "name": "Monitor readings",
                "workflow": {"sequence_after": "Confirm readings"},
            },
        ],
    )
    return instance, [template_a, template_b], context


def make_pruned_branch_instance(reason: str) -> tuple[ProcessIR, ProcessIR, InstanceContext]:
    template = ProcessIR.model_validate(
        {
            "ir_version": "1.0",
            "process": {
                "id": "optional-outreach",
                "kind": "template",
                "name": "Optional outreach",
                "properties": [],
                "flowElements": [
                    {
                        "type": "startEvent",
                        "id": "start",
                        "name": "Start",
                        "acp": {
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "start-event",
                                "supports": ["choose_outreach"],
                            }
                        },
                    },
                    {
                        "type": "exclusiveGateway",
                        "id": "choose_outreach",
                        "name": "Include optional outreach?",
                        "gatewayDirection": "Diverging",
                        "default": "f_default",
                        "acp": {
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "exclusive-split",
                                "supports": ["f_optional"],
                            }
                        },
                    },
                    {
                        "type": "task",
                        "id": "optional_task",
                        "name": "Send optional outreach",
                        "taskName": "patient.send_message",
                        "acp": {
                            "provenance": {
                                "kind": "source",
                                "source_text": "An optional outreach may be sent.",
                            }
                        },
                    },
                    {"type": "endEvent", "id": "end_default", "name": "Continue"},
                    {"type": "endEvent", "id": "end_optional", "name": "Outreach complete"},
                    {"type": "endEvent", "id": "end_timeout", "name": "Period complete"},
                    {
                        "type": "boundaryEvent",
                        "id": "period_elapsed",
                        "name": "Maximum duration elapsed",
                        "attachedToRef": "main",
                        "timerEventDefinition": {"timeDuration": {"param": "max_duration"}},
                        "acp": {
                            "provenance": {
                                "kind": "structural",
                                "derivation_rule": "plan-bound",
                                "supports": ["optional_task"],
                            }
                        },
                    },
                    {"type": "sequenceFlow", "id": "f_start", "sourceRef": "start", "targetRef": "choose_outreach"},
                    {
                        "type": "sequenceFlow",
                        "id": "f_optional",
                        "sourceRef": "choose_outreach",
                        "targetRef": "optional_task",
                        "conditionExpression": "include_optional == true",
                        "acp": {
                            "provenance": {
                                "kind": "source",
                                "source_text": "An optional outreach may be sent.",
                            }
                        },
                    },
                    {"type": "sequenceFlow", "id": "f_default", "sourceRef": "choose_outreach", "targetRef": "end_default"},
                    {"type": "sequenceFlow", "id": "f_optional_end", "sourceRef": "optional_task", "targetRef": "end_optional"},
                    {"type": "sequenceFlow", "id": "f_timeout", "sourceRef": "period_elapsed", "targetRef": "end_timeout"},
                ],
                "acp": {
                    "triggers": [{"kind": "recommendation", "recommendation_id": "rec-outreach"}],
                    "parameters": [
                        {
                            "name": "include_optional",
                            "type": "boolean",
                            "required": True,
                            "source": {"kind": "cpg", "source_text": "Optional outreach may be sent."},
                        },
                        {"name": "max_duration", "type": "duration", "required": True, "reserved": True},
                    ],
                    "provenance": {"source_cpg": "SYN-HTN-2026-002"},
                },
            },
        }
    )
    instance = deepcopy(template)
    instance.process.id = "optional-outreach-instance"
    instance.process.kind = "instance"
    removed = {"f_optional", "optional_task", "f_optional_end", "end_optional"}
    instance.process.flowElements = [
        element
        for element in instance.process.flowElements
        if element.id not in removed
    ]
    instance.process.acp.template_refs = [
        TemplateRef(template_id=template.process.id, version="v1")
    ]
    for element in instance.process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=template.process.id,
            version="v1",
            element_id=element.id,
        )
        if isinstance(element.acp.provenance, StructuralProvenance) and element.id != "period_elapsed":
            element.acp.provenance = None
    instance.process.acp.pruned = [
        PruneRecord(
            id="prune-optional-outreach",
            template_id=template.process.id,
            element_ids=sorted(removed),
            reason=reason,
            evidence_id="ev-prune",
        )
    ]
    evidence: Evidence
    if reason == "inapplicable-category":
        evidence = Evidence(
            kind="category-flag",
            payload={"flag": "include_optional", "value": False},
        )
        context = InstanceContext(category_flags={"include_optional": False})
    elif reason == "inapplicable-dmn-output":
        evidence = Evidence(
            kind="dmn-output",
            payload={
                "audit_id": "audit-1",
                "output_name": "include_optional",
                "output_value": False,
            },
        )
        context = InstanceContext(
            dmn_audit_trail=[
                {"id": "audit-1", "outputs": {"include_optional": False}}
            ]
        )
    else:
        evidence = Evidence(
            kind="careplan-feedback", payload="Do not send optional outreach."
        )
        context = InstanceContext(careplan_feedback="Do not send optional outreach.")
    context.bindings = [
        ParameterBinding(name="include_optional", value=False, source="plan-derived"),
        ParameterBinding(name="max_duration", value="P28D", source="plan-derived"),
    ]
    context.evidence = {"ev-prune": evidence}
    return instance, template, context


def codes(findings) -> set[str]:
    return {finding.code for finding in findings}


def test_single_template_instance_passes_all_instance_checks() -> None:
    instance, template, context = make_instance()

    assert validate_instance(instance, [template], context) == []


def test_retained_element_must_resolve_to_its_template_element() -> None:
    instance, template, context = make_instance()
    task = next(item for item in instance.process.flowElements if item.id == "notify")
    task.acp.template_ref.element_id = "missing"

    assert "I1" in codes(validate_instance(instance, [template], context))


def test_retained_element_must_preserve_capability_and_literal_shape() -> None:
    instance, template, context = make_instance()
    task = next(item for item in instance.process.flowElements if item.id == "query_diastolic")
    task.taskName = "patient.request_observation"

    assert "I1" in codes(validate_instance(instance, [template], context))


def test_renamed_template_node_is_rejected() -> None:
    instance, template, context = make_instance()
    task = next(item for item in instance.process.flowElements if item.id == "notify")
    task.id = "renamed_notify"

    assert "I1" in codes(validate_instance(instance, [template], context))


def test_concept_resolution_requires_plan_derived_binding() -> None:
    instance, template, context = make_instance()
    task = next(item for item in instance.process.flowElements if item.id == "request_readings")
    task.inputs["code"] = Literal_(
        literal="http://loinc.org|85354-9", type="code"
    )

    assert "I1" in codes(validate_instance(instance, [template], context))


def test_concept_resolution_accepts_a_plan_derived_code_binding() -> None:
    instance, template, context = make_instance()
    task = next(item for item in instance.process.flowElements if item.id == "request_readings")
    task.inputs["code"] = Literal_(
        literal="http://loinc.org|85354-9", type="code"
    )
    context.bindings.append(
        ParameterBinding(
            name="request_readings__code",
            value="http://loinc.org|85354-9",
            source="plan-derived",
        )
    )

    assert "I1" not in codes(validate_instance(instance, [template], context))


def test_dot_code_binding_is_not_an_exempt_instance_binding() -> None:
    instance, template, context = make_instance()
    context.bindings.append(
        ParameterBinding(
            name="request_readings.code",
            value="http://loinc.org|85354-9",
            source="plan-derived",
        )
    )

    findings = validate_instance(instance, [template], context)

    assert any(
        finding.code == "I4"
        and "request_readings.code" in finding.message
        and "does not name an instance parameter" in finding.message
        for finding in findings
    )


def test_threshold_cannot_be_moved_from_parameter_to_literal() -> None:
    instance, template, context = make_instance()
    flow = next(item for item in instance.process.flowElements if item.id == "f_high")
    flow.conditionExpression = "avg_systolic >= 140 or avg_diastolic >= notify_diastolic_threshold"

    assert "I1" in codes(validate_instance(instance, [template], context))


def test_reminder_timer_cannot_be_changed() -> None:
    from cpg_contracts.automation.ir import ParamRef

    instance, template, context = make_instance()
    timer = next(item for item in instance.process.flowElements if item.id == "wait_interval")
    timer.timerEventDefinition.timeDuration = ParamRef(param="max_duration")

    assert "I1" in codes(validate_instance(instance, [template], context))


def test_structural_instance_provenance_uses_the_closed_rule_set() -> None:
    instance, template, context = make_instance()
    start = next(item for item in instance.process.flowElements if item.id == "start")
    start.acp.provenance = StructuralProvenance(
        kind="structural", derivation_rule="exclusive-split", supports=["f_start"]
    )

    assert "I2" in codes(validate_instance(instance, [template], context))


def test_fragment_induced_subgraph_must_preserve_edges() -> None:
    instance, template, context = make_instance()
    flow = next(item for item in instance.process.flowElements if item.id == "f_wait")
    flow.targetRef = "query_diastolic"

    assert "I3" in codes(validate_instance(instance, [template], context))


def test_each_instance_parameter_must_have_one_valid_binding() -> None:
    instance, template, context = make_instance()
    context.bindings.append(
        ParameterBinding(name="max_reminder_attempts", value=99, source="clinician")
    )

    assert "I4" in codes(validate_instance(instance, [template], context))


def test_missing_required_binding_is_incomplete_warning() -> None:
    instance, template, context = make_instance()
    context.bindings = [item for item in context.bindings if item.name != "max_duration"]

    findings = validate_instance(instance, [template], context)

    assert any(
        f.code == "incomplete-binding"
        and f.rung == "L5a"
        and f.severity == "WARNING"
        for f in findings
    )


def test_instance_invariants_are_checked_after_binding() -> None:
    instance, template, context = make_instance()
    binding = next(item for item in context.bindings if item.name == "max_duration")
    binding.value = "P7D"

    assert "I4" in codes(validate_instance(instance, [template], context))


def test_instance_requires_exactly_one_main_boundary() -> None:
    instance, template, context = make_instance()
    instance.process.flowElements = [
        item for item in instance.process.flowElements if item.id != "period_elapsed"
    ]

    assert "I5" in codes(validate_instance(instance, [template], context))


def test_instance_keeps_template_names_verbatim() -> None:
    instance, template, context = make_instance()
    task = next(item for item in instance.process.flowElements if item.id == "notify")
    task.name = "A new notification label"

    assert "I6" in codes(validate_instance(instance, [template], context))


def test_message_text_cannot_be_rewritten_without_clinician_binding() -> None:
    instance, template, context = make_instance()
    binding = next(item for item in context.bindings if item.name == "reminder_text")
    binding.value = "New text with extra instructions."

    assert "I6" in codes(validate_instance(instance, [template], context))


def test_clinician_can_change_authored_text_parameter() -> None:
    instance, template, context = make_instance()
    binding = next(item for item in context.bindings if item.name == "reminder_text")
    binding.value = "Please send a reminder."
    binding.source = "clinician"

    assert "I6" not in codes(validate_instance(instance, [template], context))


def test_prune_must_not_remove_a_default_flow() -> None:
    instance, template, context = make_instance()
    instance.process.acp.pruned = [
        PruneRecord(
            id="prune_default",
            template_id=template.process.id,
            element_ids=["f_none"],
            reason="inapplicable-category",
            evidence_id="ev-category",
        )
    ]
    context.category_flags = {"has_readings": False}
    context.evidence = {
        "ev-category": Evidence(kind="category-flag", payload={"flag": "has_readings"})
    }

    assert "I8" in codes(validate_instance(instance, [template], context))


def test_prune_must_preserve_clinical_escalation_obligations() -> None:
    instance, template, context = make_instance()
    instance.process.acp.pruned = [
        PruneRecord(
            id="prune_escalation",
            template_id=template.process.id,
            element_ids=["escalate"],
            reason="clinician-directed",
            evidence_id="ev-feedback",
        )
    ]
    context.careplan_feedback = "Remove this escalation."
    context.evidence = {
        "ev-feedback": Evidence(kind="careplan-feedback", payload="Remove this escalation.")
    }

    assert "I8" in codes(validate_instance(instance, [template], context))


def test_prune_evidence_must_exist_and_match_its_reason() -> None:
    instance, template, context = make_instance()
    instance.process.acp.pruned = [
        PruneRecord(
            id="prune_flow",
            template_id=template.process.id,
            element_ids=["f_some"],
            reason="inapplicable-dmn-output",
            evidence_id="missing",
        )
    ]

    assert "I8" in codes(validate_instance(instance, [template], context))


@pytest.mark.parametrize(
    "reason",
    ["inapplicable-dmn-output", "inapplicable-category", "clinician-directed"],
)
def test_complete_nondefault_branch_can_be_pruned_with_matching_evidence(reason: str) -> None:
    instance, template, context = make_pruned_branch_instance(reason)

    assert validate_instance(instance, [template], context) == []


def test_outgoing_timer_flow_cannot_be_pruned() -> None:
    instance, template, context = make_instance()
    instance.process.acp.pruned = [
        PruneRecord(
            id="prune_timer_flow",
            template_id=template.process.id,
            element_ids=["f_query"],
            reason="clinician-directed",
            evidence_id="ev-feedback",
        )
    ]
    context.careplan_feedback = "Remove the timer flow."
    context.evidence = {
        "ev-feedback": Evidence(kind="careplan-feedback", payload="Remove the timer flow.")
    }

    findings = validate_instance(instance, [template], context)
    assert any("timer-event outgoing flow" in finding.message for finding in findings)


def test_plan_bound_boundary_cannot_be_pruned() -> None:
    instance, template, context = make_instance()
    instance.process.acp.pruned = [
        PruneRecord(
            id="prune_boundary",
            template_id=template.process.id,
            element_ids=["period_elapsed"],
            reason="clinician-directed",
            evidence_id="ev-feedback",
        )
    ]
    context.careplan_feedback = "Remove the time limit."
    context.evidence = {
        "ev-feedback": Evidence(kind="careplan-feedback", payload="Remove the time limit.")
    }

    findings = validate_instance(instance, [template], context)
    assert any("plan-bound boundary" in finding.message for finding in findings)


def test_prune_record_requires_a_reason() -> None:
    with pytest.raises(ValidationError):
        PruneRecord.model_validate(
            {
                "id": "bad_prune",
                "template_id": "home-bp-monitoring",
                "element_ids": ["f_some"],
                "evidence_id": "ev-1",
            }
        )


def test_two_fragments_without_justification_fail() -> None:
    instance, templates, context = make_composed_instance(justification=False)

    assert "I9" in codes(validate_instance(instance, templates, context))


def test_plan_sequence_justification_passes_for_ordered_activities() -> None:
    instance, templates, context = make_composed_instance()

    assert validate_instance(instance, templates, context) == []


def test_plan_sequence_justification_fails_for_reversed_activities() -> None:
    instance, templates, context = make_composed_instance()
    context.evidence["ev-sequence"].payload["activity_ids"] = [
        "act-monitor",
        "act-confirm",
    ]

    assert "I9" in codes(validate_instance(instance, templates, context))


def test_trigger_chain_justification_checks_template_outputs_and_triggers() -> None:
    instance, templates, context = make_composed_instance()
    template_a, template_b = templates
    template_a.process.acp.produces = [
        Produces.model_validate(
            {
                "outcome": "complete",
                "trigger": {"kind": "recommendation", "recommendation_id": "rec-monitoring"},
            }
        )
    ]
    template_b.process.acp.triggers = [
        Trigger.model_validate(
            {"kind": "recommendation", "recommendation_id": "rec-monitoring"}
        )
    ]
    link = next(item for item in instance.process.flowElements if item.id == "a_to_b")
    link.acp.provenance.justification = Justification(
        kind="trigger-chain", evidence_id="ev-trigger"
    )
    context.evidence["ev-trigger"] = Evidence(kind="trigger-chain", payload={})

    assert validate_instance(instance, templates, context) == []


def test_clinician_directed_composition_requires_feedback_quote() -> None:
    instance, templates, context = make_composed_instance()
    link = next(item for item in instance.process.flowElements if item.id == "a_to_b")
    link.acp.provenance.justification = Justification(
        kind="clinician-directed", evidence_id="ev-feedback"
    )
    context.careplan_feedback = "Join these activities after review."
    context.evidence["ev-feedback"] = Evidence(
        kind="careplan-feedback", payload={"quote": "Join these activities"}
    )

    assert validate_instance(instance, templates, context) == []


def test_boundary_consolidation_must_list_removed_template_boundary() -> None:
    instance, templates, context = make_composed_instance()
    instance.process.acp.consolidated_boundaries = []

    assert "I10" in codes(validate_instance(instance, templates, context))


def test_composed_max_duration_must_satisfy_every_template_invariant() -> None:
    instance, templates, context = make_composed_instance(max_duration="P1D")

    assert "I10" in codes(validate_instance(instance, templates, context))


def test_unified_property_names_require_a_unify_record() -> None:
    instance, templates, context = make_composed_instance()
    for template in templates:
        template.process.properties = [Property(name="shared_count", type="integer")]
    instance.process.properties = [Property(name="shared_count", type="integer")]

    assert "I9" in codes(validate_instance(instance, templates, context))


def test_unified_property_requires_matching_fragment_members() -> None:
    instance, templates, context = make_composed_instance()
    for template in templates:
        template.process.properties = [Property(name="shared_count", type="integer")]
    instance.process.properties = [Property(name="shared_count", type="integer")]
    instance.process.acp.unify = [
        UnifyRecord(name="shared_count", members=["a__shared_count", "b__shared_count"])
    ]

    assert validate_instance(instance, templates, context) == []


def test_unified_property_requires_matching_type_and_unit() -> None:
    instance, templates, context = make_composed_instance()
    templates[0].process.properties = [Property(name="shared_count", type="integer")]
    templates[1].process.properties = [Property(name="shared_count", type="decimal")]
    instance.process.properties = [Property(name="shared_count", type="integer")]
    instance.process.acp.unify = [
        UnifyRecord(name="shared_count", members=["a__shared_count", "b__shared_count"])
    ]

    assert "I9" in codes(validate_instance(instance, templates, context))


def test_composed_fragment_uses_one_prefix_for_all_its_elements() -> None:
    instance, templates, context = make_composed_instance()
    task = next(item for item in instance.process.flowElements if item.id == "a__confirm")
    task.id = "other__confirm"
    for flow in instance.process.flowElements:
        if flow.type == "sequenceFlow":
            flow.sourceRef = "other__confirm" if flow.sourceRef == "a__confirm" else flow.sourceRef
            flow.targetRef = "other__confirm" if flow.targetRef == "a__confirm" else flow.targetRef

    assert "I9" in codes(validate_instance(instance, templates, context))


def test_public_instance_validator_is_traced_when_mlflow_is_installed() -> None:
    pytest.importorskip("mlflow")
    assert getattr(validate_instance, "__mlflow_traced__", False)
