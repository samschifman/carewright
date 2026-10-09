"""Pure expansion of the supported IR macros into an intermediate BPMN tree."""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from typing import Any

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.catalog import Catalog, CapabilityIO
from cpg_contracts.automation.compiler.expanded import XFlow, XIo, XNode, XProcess, XProperty
from cpg_contracts.automation.compiler.kogito_profile import (
    ACP_NS,
    CONDITION_LANGUAGE,
    DMN_IMPLEMENTATION,
    GROUP_IDS,
    ITEM_DEFINITION_IDS,
    PROFILE_VERSION,
    SCRIPT_FORMAT,
    STRUCTURE_REF,
    normalize_duration,
    task_label,
)
from cpg_contracts.automation.expressions import ExpressionError
from cpg_contracts.automation.ir import (
    AcpElement,
    BoundaryEvent,
    BusinessRuleTask,
    ConceptRef,
    EndEvent,
    ExclusiveGateway,
    FlowElement,
    IntermediateCatchEvent,
    Literal_,
    ParamRef,
    ProcessIR,
    PropertyRef,
    SequenceFlow,
    StartEvent,
    Task,
    UserTask,
    ir_version,
)


_IO_STRING_TYPES = frozenset({"enum", "unit"})
_XML_ID_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _xml_id_component(name: str) -> str:
    """Make an external BPMN I/O label safe inside a generated XML id."""
    return _XML_ID_COMPONENT_RE.sub("_", name)


def _encode_literal(value: bool | int | float | str, literal_type: str) -> str:
    if literal_type == "duration":
        return normalize_duration(str(value))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _value_binding(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, ParamRef):
        return value.param, None
    if isinstance(value, PropertyRef):
        return value.property, None
    if isinstance(value, ConceptRef):
        return None, f"concept:{value.concept}"
    if isinstance(value, Literal_):
        return None, _encode_literal(value.literal, value.type)
    if isinstance(value, dict):
        if "param" in value:
            return value["param"], None
        if "property" in value:
            return value["property"], None
        if "concept" in value:
            return None, f"concept:{value['concept']}"
        if "literal" in value:
            return None, _encode_literal(value["literal"], value["type"])
    raise ExpressionError(f"unsupported ValueRef {value!r}")


def _drools_type(io_type: str | None) -> str | None:
    if io_type in STRUCTURE_REF:
        return STRUCTURE_REF[io_type]
    if io_type in _IO_STRING_TYPES:
        return "String"
    return None


def _item_subject_ref(io_type: str | None, value: Any | None = None) -> str | None:
    if io_type == "code" and isinstance(value, (ConceptRef, Literal_)):
        return None
    drools_type = _drools_type(io_type)
    if drools_type is None:
        return None
    return ITEM_DEFINITION_IDS[drools_type]


def _data_input(
    element_id: str,
    name: str,
    io_type: str | None,
    value: Any,
) -> XIo:
    source_ref, literal = _value_binding(value)
    return XIo(
        id=f"{element_id}_{name}_input",
        name=name,
        direction="input",
        type=io_type,
        drools_type=_drools_type(io_type),
        item_subject_ref=_item_subject_ref(io_type, value),
        source_ref=source_ref,
        literal=literal,
    )


def _data_output(element_id: str, name: str, io_type: str | None, target: str) -> XIo:
    drools_type = _drools_type(io_type)
    return XIo(
        id=f"{element_id}_{name}_output",
        name=name,
        direction="output",
        type=io_type,
        drools_type=drools_type,
        item_subject_ref=ITEM_DEFINITION_IDS.get(drools_type),
        target_ref=target,
    )


def _special_input(element_id: str, name: str, value: str) -> XIo:
    return XIo(
        id=f"{element_id}_{name}",
        name=name,
        direction="input",
        type="string",
        drools_type="Object",
        literal=value,
    )


def _capability_io(items: list[CapabilityIO], name: str, capability_id: str) -> CapabilityIO:
    for item in items:
        if item.name == name:
            return item
    raise ValueError(f"capability {capability_id!r} has no I/O named {name!r}")


@trace
def element_payload(element: FlowElement) -> str:
    """Serialize all element metadata and any source condition as compact JSON."""
    payload = element.acp.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )

    def without_empty_collections(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cleaned
                for key, item in value.items()
                if (cleaned := without_empty_collections(item)) not in ({}, [])
            }
        if isinstance(value, list):
            return [without_empty_collections(item) for item in value]
        return value

    payload = without_empty_collections(payload)
    if isinstance(element, SequenceFlow) and element.conditionExpression is not None:
        payload["conditionExpression"] = element.conditionExpression
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


@trace
def template_payload(ir: ProcessIR, catalog: Catalog) -> dict[str, Any]:
    """Build the process metadata block needed to recover the complete IR."""
    process = ir.process
    return {
        "template_id": process.id,
        "version": ir_version(ir),
        "kind": process.kind,
        "description": process.description,
        "ir_version": ir.ir_version,
        "catalog_version": catalog.version,
        "profile": PROFILE_VERSION,
        **process.acp.model_dump(mode="json", exclude_none=True),
    }


@trace
def timer_expression(value_ref: Any) -> str:
    """Return a normalized BPMN timer expression from a timer ValueRef."""
    if isinstance(value_ref, ParamRef):
        return f"#{{{value_ref.param}}}"
    if isinstance(value_ref, Literal_):
        if value_ref.type != "duration":
            raise ExpressionError("timer literals must have type 'duration'")
        return normalize_duration(str(value_ref.literal))
    if isinstance(value_ref, dict):
        if "param" in value_ref:
            return f"#{{{value_ref['param']}}}"
        if "literal" in value_ref:
            if value_ref.get("type") != "duration":
                raise ExpressionError("timer literals must have type 'duration'")
            return normalize_duration(str(value_ref["literal"]))
    raise ExpressionError("timers accept only parameter references or duration literals")


def _property_type(properties: list[XProperty], name: str) -> str | None:
    for prop in properties:
        if prop.name == name:
            return prop.type
    return None


def _make_custom_task(task: Task, catalog: Catalog) -> XNode:
    capability = catalog.get(task.taskName)
    io: list[XIo] = []
    for name, value in sorted(task.inputs.items()):
        spec = _capability_io(capability.inputs, name, capability.id)
        io.append(_data_input(task.id, name, spec.type, value))
    io.append(_special_input(task.id, "TaskName", task_label(task.taskName, task.id)))
    for name, target in sorted(task.outputs.items()):
        spec = _capability_io(capability.outputs, name, capability.id)
        io.append(_data_output(task.id, name, spec.type, target))
    return XNode(
        type="task",
        id=task.id,
        name=task.name,
        work_name=task.taskName,
        io=io,
        element_payload=element_payload(task),
        counter=task.acp.counter,
    )


@trace
def expand_io(task: Task | UserTask, catalog: Catalog) -> XNode:
    """Expand task or user-task input/output maps into XIo records."""
    if isinstance(task, Task):
        return _make_custom_task(task, catalog)

    io = [
        _special_input(task.id, "TaskName", task.id),
        _special_input(task.id, "Skippable", "false"),
        _special_input(task.id, "GroupId", GROUP_IDS[task.groupId]),
    ]
    for name, target in sorted(task.outputs.items()):
        io.append(_data_output(task.id, name, None, target))
    return XNode(
        type="userTask",
        id=task.id,
        name=task.name,
        io=io,
        element_payload=element_payload(task),
    )


@trace
def expand_dmn(task: BusinessRuleTask, namespace: str, model: str) -> XNode:
    """Expand DMN task references into standard namespace/model inputs."""
    io = [
        _special_input(task.id, "namespace", namespace),
        _special_input(task.id, "model", model),
    ]
    for name, value in sorted(task.inputs.items()):
        source_ref, literal = _value_binding(value)
        io.append(
            XIo(
                id=f"{task.id}_{_xml_id_component(name)}_input",
                name=name,
                direction="input",
                source_ref=source_ref,
                literal=literal,
            )
        )
    for name, target in sorted(task.outputs.items()):
        io.append(
            XIo(
                id=f"{task.id}_{_xml_id_component(name)}_output",
                name=name,
                direction="output",
                target_ref=target,
            )
        )
    return XNode(
        type="businessRuleTask",
        id=task.id,
        name=task.name,
        attributes={"implementation": DMN_IMPLEMENTATION},
        implementation=DMN_IMPLEMENTATION,
        io=io,
        element_payload=element_payload(task),
    )


def _all_nodes(nodes: list[XNode]):
    pending = list(nodes)
    while pending:
        node = pending.pop(0)
        yield node
        pending[0:0] = node.children


def _all_ids(process: XProcess) -> set[str]:
    ids = {node.id for node in _all_nodes(process.nodes)}
    ids.update(flow.id for flow in process.flows)
    ids.update(prop.id for prop in process.properties)
    for node in _all_nodes(process.nodes):
        ids.update(flow.id for flow in node.flows)
        ids.update(io.id for io in node.io)
    return ids


@trace
def _assert_unique_xml_ids(process: XProcess) -> None:
    identifiers = [process.definitions_id, process.id]
    identifiers.extend(item_id for item_id, _ in process.item_definitions)
    identifiers.extend(prop.id for prop in process.properties)
    for node in _all_nodes(process.nodes):
        identifiers.append(node.id)
        identifiers.extend(io.id for io in node.io)
        identifiers.extend(flow.id for flow in node.flows)
    identifiers.extend(flow.id for flow in process.flows)
    duplicates = sorted(item for item, count in Counter(identifiers).items() if count > 1)
    if duplicates:
        raise ValueError(f"duplicate generated BPMN id(s): {', '.join(duplicates)}")


@trace
def _claim_id(identifier: str, used: set[str]) -> str:
    """Claim a design-table id, failing instead of renaming on collision."""
    if identifier in used:
        raise ValueError(f"generated BPMN id {identifier!r} collides with an existing id")
    used.add(identifier)
    return identifier


def _insert_after(nodes: list[XNode], element_id: str, node: XNode) -> None:
    for index, existing in enumerate(nodes):
        if existing.id == element_id:
            nodes.insert(index + 1, node)
            return
    raise ValueError(f"cannot insert macro node after missing element {element_id!r}")


@trace
def expand_counters(process: XProcess) -> XProcess:
    """Insert counter initialization, increment, and flow-reset script tasks."""
    expanded = copy.deepcopy(process)
    used = _all_ids(expanded)
    properties = {prop.name: prop for prop in expanded.properties}

    counter_nodes: dict[str, XNode] = {}
    counters: list[str] = []
    for node in list(expanded.nodes):
        if node.counter is None:
            continue
        counter = node.counter
        prop = properties.get(counter)
        if prop is None or prop.type != "integer":
            raise ValueError(f"counter {counter!r} on {node.id!r} must name an integer property")
        outgoing = [flow for flow in expanded.flows if flow.source_ref == node.id]
        if len(outgoing) != 1:
            raise ValueError(f"counter task {node.id!r} must have exactly one outgoing flow")
        script_id = _claim_id(f"{node.id}_counter", used)
        counter_node = XNode(
            type="scriptTask",
            id=script_id,
            name=f"Count {counter.replace('_', ' ')}",
            script_format=SCRIPT_FORMAT,
            script=(
                f'kcontext.setVariable("{counter}", '
                f'((Integer) kcontext.getVariable("{counter}")) + 1);'
            ),
        )
        counter_nodes[node.id] = counter_node
        counters.append(counter)
        _insert_after(expanded.nodes, node.id, counter_node)
        outgoing[0].source_ref = script_id

    init_id = _claim_id("init", used)
    init_lines = [f'kcontext.setVariable("{name}", 0);' for name in dict.fromkeys(counters)]
    init_lines.append('kcontext.setVariable("outcome", "");')
    init_node = XNode(
        type="scriptTask",
        id=init_id,
        name="Initialise",
        script_format=SCRIPT_FORMAT,
        script=" ".join(init_lines),
    )
    start = next((node for node in expanded.nodes if node.type == "startEvent"), None)
    if start is None:
        raise ValueError("expanded process must contain a start event")
    _insert_after(expanded.nodes, start.id, init_node)
    start_flows = [flow for flow in expanded.flows if flow.source_ref == start.id]
    if len(start_flows) != 1:
        raise ValueError("expanded process start event must have exactly one outgoing flow")

    count_flows: dict[str, XFlow] = {}
    for task_id, counter_node in counter_nodes.items():
        count_flows[task_id] = XFlow(
            id=_claim_id(f"{task_id}_counter_in", used),
            source_ref=task_id,
            target_ref=counter_node.id,
        )

    reset_flows: dict[str, XFlow] = {}
    for flow in list(expanded.flows):
        if not flow.resets:
            continue
        names = list(dict.fromkeys(flow.resets))
        for name in names:
            prop = properties.get(name)
            if prop is None or prop.type != "integer":
                raise ValueError(f"flow reset {name!r} must name an integer property")
        old_target = flow.target_ref
        reset_id = _claim_id(f"{flow.id}_reset", used)
        reset_node = XNode(
            type="scriptTask",
            id=reset_id,
            name="Reset " + ", ".join(name.replace("_", " ") for name in names),
            script_format=SCRIPT_FORMAT,
            script=" ".join(f'kcontext.setVariable("{name}", 0);' for name in names),
        )
        _insert_after(expanded.nodes, flow.source_ref, reset_node)
        flow.target_ref = reset_id
        flow.resets = []
        reset_flows[flow.id] = XFlow(
            id=_claim_id(f"{flow.id}_reset_out", used),
            source_ref=reset_id,
            target_ref=old_target,
        )

    expanded_flows: list[XFlow] = []
    for flow in expanded.flows:
        count_flow = next(
            (item for task_id, item in count_flows.items() if item.target_ref == flow.source_ref),
            None,
        )
        # The inserted counter is the source of the original task flow.
        if count_flow is not None:
            expanded_flows.append(count_flow)
        expanded_flows.append(flow)
        reset_flow = reset_flows.get(flow.id)
        if reset_flow is not None:
            expanded_flows.append(reset_flow)
    # M4 owns the outer start → init → main path. Keep the original start
    # edge for M4 to re-home at main_start.
    expanded.flows = expanded_flows
    return expanded


def _boundary_end_ids(process: XProcess, boundary_ids: set[str]) -> set[str]:
    incoming: dict[str, list[XFlow]] = {}
    for flow in process.flows:
        incoming.setdefault(flow.target_ref, []).append(flow)
    node_by_id = {node.id: node for node in process.nodes}
    boundary_ends: set[str] = set()
    for flow in process.flows:
        target = node_by_id.get(flow.target_ref)
        if (
            flow.source_ref in boundary_ids
            and target is not None
            and target.type == "endEvent"
            and all(item.source_ref in boundary_ids for item in incoming[target.id])
        ):
            boundary_ends.add(target.id)
    return boundary_ends


@trace
def _flow_from_ir(element: SequenceFlow) -> XFlow:
    return XFlow(
        id=element.id,
        source_ref=element.sourceRef,
        target_ref=element.targetRef,
        name=element.name,
        condition_expression=element.conditionExpression,
        element_payload=element_payload(element),
        resets=list(element.acp.resets or []),
    )


def _node_from_ir(
    element: FlowElement,
    catalog: Catalog,
    dmn_namespaces: dict[str, str],
) -> XNode:
    if isinstance(element, Task):
        return _make_custom_task(element, catalog)
    if isinstance(element, UserTask):
        return expand_io(element, catalog)
    if isinstance(element, BusinessRuleTask):
        try:
            namespace = dmn_namespaces[element.dmnModel]
        except KeyError as exc:
            raise ValueError(
                f"DMN namespace is required for model {element.dmnModel!r}"
            ) from exc
        return expand_dmn(element, namespace, element.dmnModel)
    if isinstance(element, StartEvent):
        return XNode(
            type="startEvent",
            id=element.id,
            name=element.name,
            element_payload=element_payload(element),
        )
    if isinstance(element, EndEvent):
        return XNode(
            type="endEvent",
            id=element.id,
            name=element.name,
            element_payload=element_payload(element),
            outcome=element.acp.outcome,
        )
    if isinstance(element, ExclusiveGateway):
        return XNode(
            type="exclusiveGateway",
            id=element.id,
            name=element.name,
            attributes={
                "gatewayDirection": element.gatewayDirection,
                **({"default": element.default} if element.default is not None else {}),
            },
            element_payload=element_payload(element),
        )
    if isinstance(element, BoundaryEvent):
        timer = element.timerEventDefinition.timeDuration or element.timerEventDefinition.timeCycle
        return XNode(
            type="boundaryEvent",
            id=element.id,
            name=element.name,
            attributes={
                "attachedToRef": element.attachedToRef,
                "cancelActivity": element.cancelActivity,
            },
            timer_kind=(
                "timeDuration"
                if element.timerEventDefinition.timeDuration is not None
                else "timeCycle"
            ),
            timer_expression=timer_expression(timer),
            element_payload=element_payload(element),
        )
    if isinstance(element, IntermediateCatchEvent):
        timer = element.timerEventDefinition.timeDuration or element.timerEventDefinition.timeCycle
        return XNode(
            type="intermediateCatchEvent",
            id=element.id,
            name=element.name,
            timer_kind=(
                "timeDuration"
                if element.timerEventDefinition.timeDuration is not None
                else "timeCycle"
            ),
            timer_expression=timer_expression(timer),
            element_payload=element_payload(element),
        )
    if isinstance(element, SequenceFlow):
        raise TypeError("sequence flows are expanded separately")
    raise TypeError(f"unsupported IR element type {type(element).__name__}")


def _properties(ir: ProcessIR) -> list[XProperty]:
    properties: list[XProperty] = []
    for parameter in ir.process.acp.parameters:
        properties.append(
            XProperty(
                id=parameter.name,
                name=parameter.name,
                type=parameter.type,
                structure_ref=STRUCTURE_REF[parameter.type],
                unit=parameter.unit,
            )
        )
    for prop in ir.process.properties:
        properties.append(
            XProperty(
                id=prop.name,
                name=prop.name,
                type=prop.type,
                structure_ref=STRUCTURE_REF[prop.type],
                unit=prop.unit,
            )
        )
    properties.append(
        XProperty(id="outcome", name="outcome", type="string", structure_ref="String")
    )
    return properties


def _item_definitions(properties: list[XProperty], nodes: list[XNode]) -> list[tuple[str, str]]:
    used = {prop.structure_ref for prop in properties}
    for node in _all_nodes(nodes):
        used.update(
            io.drools_type
            for io in node.io
            if io.item_subject_ref is not None and io.drools_type is not None
        )
    order = ("String", "Integer", "Double", "Boolean")
    return [
        (ITEM_DEFINITION_IDS[drools_type], drools_type)
        for drools_type in order
        if drools_type in used
    ]


@trace
def expand(ir: ProcessIR, catalog: Catalog, dmn_namespaces: dict[str, str]) -> XProcess:
    """Expand an IR and its closed macros into a deterministic XProcess."""
    process = ir.process
    properties = _properties(ir)
    flow_elements = sorted(process.flowElements, key=lambda element: element.id)
    nodes = [
        _node_from_ir(element, catalog, dmn_namespaces)
        for element in flow_elements
        if not isinstance(element, SequenceFlow)
    ]
    flows = [
        _flow_from_ir(element)
        for element in flow_elements
        if isinstance(element, SequenceFlow)
    ]
    process_template_payload = template_payload(ir, catalog)
    expanded = XProcess(
        id=process.id,
        name=process.name,
        definitions_id=f"{process.id}_defs",
        target_namespace=f"{ACP_NS}/{process.acp.provenance.source_cpg}",
        version=ir_version(ir),
        catalog_version=catalog.version,
        profile_version=PROFILE_VERSION,
        ir=ir,
        properties=properties,
        nodes=nodes,
        flows=flows,
        item_definitions=_item_definitions(properties, nodes),
        template_payload=process_template_payload,
    )
    expanded = expand_main(expand_counters(expanded))
    _assert_unique_xml_ids(expanded)
    return expanded


@trace
def expand_main(process: XProcess) -> XProcess:
    """Wrap the body in ``main`` and add outcome and monitoring-period routes."""
    expanded = copy.deepcopy(process)
    used = _all_ids(expanded)
    original_nodes = list(expanded.nodes)
    start = next((node for node in original_nodes if node.type == "startEvent"), None)
    init = next((node for node in original_nodes if node.id == "init"), None)
    if start is None or init is None:
        raise ValueError("expanded process needs its start event and initializer")

    main_boundaries = [
        node
        for node in original_nodes
        if node.type == "boundaryEvent" and node.attributes.get("attachedToRef") == "main"
    ]
    if len(main_boundaries) != 1:
        raise ValueError("expanded process requires exactly one boundary attached to 'main'")

    main_boundary_ids = {node.id for node in main_boundaries}
    boundary_end_ids = _boundary_end_ids(expanded, main_boundary_ids)
    body_nodes = [
        node
        for node in original_nodes
        if node.id not in main_boundary_ids
        and node.id not in boundary_end_ids
        and node.id not in {start.id, init.id}
    ]

    main_start_id = _claim_id("main_start", used)
    main_start = XNode(type="startEvent", id=main_start_id)
    child_nodes: list[XNode] = [main_start]
    outcome_routes: dict[str, XNode] = {}
    end_to_script: dict[str, str] = {}
    end_to_inner: dict[str, str] = {}
    for node in body_nodes:
        if node.type != "endEvent":
            child_nodes.append(node)
            continue
        outcome_script_id = _claim_id(f"{node.id}_outcome", used)
        outcome_value = node.outcome or ""
        outcome_script = XNode(
            type="scriptTask",
            id=outcome_script_id,
            name=f"Outcome: {outcome_value}" if outcome_value else "Outcome",
            script_format=SCRIPT_FORMAT,
            script=f"kcontext.setVariable(\"outcome\", {json.dumps(outcome_value, ensure_ascii=False)});",
            outcome=outcome_value,
        )
        inner_end_id = _claim_id(f"main_{node.id}", used)
        inner_end = XNode(
            type="endEvent",
            id=inner_end_id,
            name=node.name,
            element_payload=node.element_payload,
            outcome=node.outcome,
        )
        child_nodes.extend((outcome_script, inner_end))
        end_to_script[node.id] = outcome_script_id
        end_to_inner[node.id] = inner_end_id
        if node.outcome:
            outcome_routes.setdefault(node.outcome, node)

    child_flows: list[XFlow] = []
    boundary_flows: list[XFlow] = []
    for flow in expanded.flows:
        prefix_end_flow: XFlow | None = None
        if flow.target_ref in end_to_script:
            original_end_id = flow.target_ref
            script_id = end_to_script[original_end_id]
            inner_id = end_to_inner[original_end_id]
            flow.target_ref = script_id
            outcome_flow = XFlow(
                id=_claim_id(f"{original_end_id}_outcome_out", used),
                source_ref=script_id,
                target_ref=inner_id,
            )
        else:
            outcome_flow = None
        if flow.source_ref in main_boundary_ids or flow.target_ref in boundary_end_ids:
            boundary_flows.append(flow)
        else:
            child_flows.append(flow)
            if outcome_flow is not None:
                child_flows.append(outcome_flow)

    for flow in child_flows:
        if flow.source_ref == start.id:
            flow.source_ref = main_start_id

    main_name = "Monitoring"
    main_id = _claim_id("main", used)
    main_node = XNode(
        type="subProcess",
        id=main_id,
        name=main_name,
        children=child_nodes,
        flows=child_flows,
    )

    gateway_id = _claim_id("gw_outcome", used)
    completed_end_id = _claim_id("end_completed", used)
    completed_flow_id = _claim_id("f_completed", used)
    after_flow_id = _claim_id("f_after", used)
    gateway_flows: list[XFlow] = []
    output_ends: list[XNode] = []
    for outcome, original_end in outcome_routes.items():
        end_id = original_end.id
        output_ends.append(
            XNode(type="endEvent", id=end_id, name=original_end.name)
        )
        route_id = _claim_id(f"{end_id}_route", used)
        gateway_flows.append(
            XFlow(
                id=route_id,
                source_ref=gateway_id,
                target_ref=end_id,
                name=outcome,
                condition_expression=f"return {json.dumps(outcome, ensure_ascii=False)}.equals(outcome);",
                condition_is_java=True,
            )
        )
    gateway_flows.append(
        XFlow(
            id=completed_flow_id,
            source_ref=gateway_id,
            target_ref=completed_end_id,
        )
    )
    gateway = XNode(
        type="exclusiveGateway",
        id=gateway_id,
        name="Outcome",
        attributes={"gatewayDirection": "Diverging", "default": completed_flow_id},
    )
    completed_end = XNode(type="endEvent", id=completed_end_id, name="Completed")
    after_flow = XFlow(id=after_flow_id, source_ref=main_id, target_ref=gateway_id)

    start_flow = XFlow(
        id=_claim_id("f_init", used),
        source_ref=start.id,
        target_ref=init.id,
    )
    main_flow = XFlow(
        id=_claim_id("f_main", used),
        source_ref=init.id,
        target_ref=main_id,
    )
    expanded.nodes = [
        start,
        init,
        main_node,
        *main_boundaries,
        gateway,
        *output_ends,
        completed_end,
        *[
            node
            for node in original_nodes
            if node.id in boundary_end_ids and node not in output_ends
        ],
    ]
    expanded.flows = [
        start_flow,
        main_flow,
        after_flow,
        *gateway_flows,
        *boundary_flows,
    ]
    return expanded
