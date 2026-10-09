"""Rebuild the supported automation IR from its BPMN representation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

from lxml import etree
from pydantic import ValidationError

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.catalog import Catalog, load_catalog
from cpg_contracts.automation.compiler.kogito_profile import (
    ACP_NS,
    BPMN_MODEL_NS,
    PROFILE_VERSION,
    CONDITION_LANGUAGE,
    DMN_IMPLEMENTATION,
    DROOLS_NS,
    GROUP_IDS,
    ITEM_DEFINITION_IDS,
    SCRIPT_FORMAT,
    STRUCTURE_REF,
    task_label,
)
from cpg_contracts.automation.expressions import ExpressionError, TypeInfo, to_java
from cpg_contracts.automation.ir import (
    AcpElement,
    AcpProcess,
    BoundaryEvent,
    BusinessRuleTask,
    EndEvent,
    ExclusiveGateway,
    IntermediateCatchEvent,
    Process,
    ProcessIR,
    Property,
    SequenceFlow,
    StartEvent,
    Task,
    TimerEventDefinition,
    UserTask,
    ir_version,
)


class ParseError(ValueError):
    """Raised when BPMN cannot be losslessly interpreted as the supported IR."""


_BPMN = f"{{{BPMN_MODEL_NS}}}"
_ACP = f"{{{ACP_NS}}}"
_DROOLS = f"{{{DROOLS_NS}}}"
_IGNORED_DIAGRAM_NAMESPACES = frozenset(
    {
        "http://www.omg.org/spec/BPMN/20100524/DI",
        "http://www.omg.org/spec/DD/20100524/DC",
        "http://www.omg.org/spec/DD/20100524/DI",
    }
)
_FIXED_GENERATED_NODES = frozenset(
    {"init", "main", "main_start", "gw_outcome", "end_completed"}
)
_FIXED_GENERATED_FLOWS = frozenset({"f_init", "f_main", "f_after", "f_completed"})
_NODE_TAGS = frozenset(
    {
        "startEvent",
        "endEvent",
        "task",
        "userTask",
        "businessRuleTask",
        "exclusiveGateway",
        "intermediateCatchEvent",
        "boundaryEvent",
        "scriptTask",
        "subProcess",
    }
)
_ALLOWED_TAGS = _NODE_TAGS | frozenset(
    {
        "definitions",
        "itemDefinition",
        "process",
        "extensionElements",
        "property",
        "sequenceFlow",
        "incoming",
        "outgoing",
        "ioSpecification",
        "dataInput",
        "dataOutput",
        "inputSet",
        "outputSet",
        "dataInputRefs",
        "dataOutputRefs",
        "dataInputAssociation",
        "dataOutputAssociation",
        "sourceRef",
        "targetRef",
        "assignment",
        "from",
        "to",
        "timerEventDefinition",
        "timeDuration",
        "timeCycle",
        "script",
        "conditionExpression",
    }
)
_INT_RE = re.compile(r"^-?\d+$")


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def _children(element: etree._Element, local: str) -> list[etree._Element]:
    return [child for child in element if child.tag == _BPMN + local]


def _is_diagram_subtree(element: etree._Element) -> bool:
    current: etree._Element | None = element
    while current is not None:
        if isinstance(current.tag, str) and etree.QName(current).namespace in _IGNORED_DIAGRAM_NAMESPACES:
            return True
        current = current.getparent()
    return False


def _one_child(
    element: etree._Element,
    local: str,
    *,
    required: bool = True,
) -> etree._Element | None:
    found = _children(element, local)
    if len(found) > 1 or (required and len(found) != 1):
        raise ParseError(
            f"expected {'one' if required else 'at most one'} bpmn2:{local} "
            f"under bpmn2:{_local(element)}, found {len(found)}"
        )
    return found[0] if found else None


def _check_children(element: etree._Element, allowed: set[str] | frozenset[str]) -> None:
    unexpected = []
    for child in element:
        if isinstance(child.tag, str) and etree.QName(child).namespace in _IGNORED_DIAGRAM_NAMESPACES:
            continue
        if (
            not isinstance(child.tag, str)
            or not child.tag.startswith(_BPMN)
            or _local(child) not in allowed
        ):
            unexpected.append(_local(child))
    if unexpected:
        raise ParseError(
            f"unexpected child element(s) under bpmn2:{_local(element)}: "
            f"{', '.join(unexpected)}"
        )


def _metadata_json(element: etree._Element, block_name: str) -> dict[str, Any]:
    extension = _one_child(element, "extensionElements")
    assert extension is not None
    blocks = [child for child in extension if child.tag == _ACP + block_name]
    if len(blocks) != 1:
        raise ParseError(
            f"bpmn2:{_local(element)} {element.get('id')!r} requires exactly one "
            f"acp:{block_name} block"
        )
    if len(extension) != 1:
        raise ParseError(
            f"unexpected extension element on bpmn2:{_local(element)} {element.get('id')!r}"
        )
    try:
        payload = json.loads(blocks[0].text or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ParseError(f"invalid acp:{block_name} JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError(f"acp:{block_name} payload must be a JSON object")
    return payload


def _element_metadata(
    element: etree._Element,
    *,
    is_flow: bool = False,
) -> tuple[AcpElement, str | None]:
    payload = _metadata_json(element, "element")
    source_condition = payload.pop("conditionExpression", None)
    if not is_flow and source_condition is not None:
        raise ParseError(
            f"acp:element conditionExpression is only allowed on sequenceFlow "
            f"{element.get('id')!r}"
        )
    try:
        acp = AcpElement.model_validate(payload)
    except ValidationError as exc:
        raise ParseError(f"invalid acp:element for {element.get('id')!r}: {exc}") from exc
    return acp, source_condition


def _template_metadata(process_element: etree._Element) -> tuple[dict[str, Any], AcpProcess]:
    extension = _one_child(process_element, "extensionElements")
    assert extension is not None
    template_elements = [child for child in extension if child.tag == _ACP + "template"]
    catalog_elements = [
        child
        for child in extension
        if child.tag == _DROOLS + "metaData" and child.get("name") == "acp.catalogVersion"
    ]
    if len(template_elements) != 1 or len(catalog_elements) != 1:
        raise ParseError("process requires one acp:template and one acp.catalogVersion")
    if len(extension) != 2:
        raise ParseError("unexpected process extension element")
    try:
        payload = json.loads(template_elements[0].text or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ParseError(f"invalid acp:template JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError("acp:template payload must be a JSON object")

    metadata_keys = {
        "template_id",
        "version",
        "kind",
        "description",
        "ir_version",
        "catalog_version",
        "profile",
    }
    acp_keys = set(AcpProcess.model_fields)
    if set(payload) != metadata_keys | acp_keys:
        missing = sorted((metadata_keys | acp_keys) - set(payload))
        extra = sorted(set(payload) - (metadata_keys | acp_keys))
        raise ParseError(
            f"acp:template keys differ from the supported shape; missing={missing}, extra={extra}"
        )
    if payload["catalog_version"] != (catalog_elements[0].text or ""):
        raise ParseError("acp:template catalog_version differs from acp.catalogVersion")
    if payload["profile"] != PROFILE_VERSION:
        raise ParseError(f"unsupported compiler profile {payload['profile']!r}")
    try:
        acp = AcpProcess.model_validate({key: payload[key] for key in acp_keys})
    except ValidationError as exc:
        raise ParseError(f"invalid acp:template process metadata: {exc}") from exc
    return payload, acp


@dataclass(frozen=True, slots=True)
class _Binding:
    source_ref: str | None
    literal: str | None


def _io_bindings(element: etree._Element) -> tuple[dict[str, _Binding], dict[str, str]]:
    io_spec = _one_child(element, "ioSpecification")
    assert io_spec is not None
    _check_children(io_spec, {"dataInput", "dataOutput", "inputSet", "outputSet"})
    inputs = _children(io_spec, "dataInput")
    outputs = _children(io_spec, "dataOutput")

    def collect(items: list[etree._Element], name: str) -> dict[str, etree._Element]:
        result: dict[str, etree._Element] = {}
        for item in items:
            item_name = item.get("name")
            item_id = item.get("id")
            if not item_name or not item_id or item_name in result:
                raise ParseError(f"invalid or duplicate {name} on {element.get('id')!r}")
            result[item_name] = item
        return result

    input_by_name = collect(inputs, "dataInput")
    output_by_name = collect(outputs, "dataOutput")
    input_by_id = {item.get("id"): name for name, item in input_by_name.items()}
    output_by_id = {item.get("id"): name for name, item in output_by_name.items()}

    input_sets = _children(io_spec, "inputSet")
    output_sets = _children(io_spec, "outputSet")
    if len(input_sets) != 1 or len(output_sets) != 1:
        raise ParseError(f"ioSpecification on {element.get('id')!r} needs one inputSet and outputSet")
    if set((item.text or "") for item in _children(input_sets[0], "dataInputRefs")) != set(input_by_id):
        raise ParseError(f"inputSet on {element.get('id')!r} does not match dataInputs")
    if set((item.text or "") for item in _children(output_sets[0], "dataOutputRefs")) != set(output_by_id):
        raise ParseError(f"outputSet on {element.get('id')!r} does not match dataOutputs")

    input_associations: dict[str, _Binding] = {}
    for association in _children(element, "dataInputAssociation"):
        _check_children(association, {"sourceRef", "targetRef", "assignment"})
        targets = _children(association, "targetRef")
        sources = _children(association, "sourceRef")
        assignments = _children(association, "assignment")
        if len(targets) != 1 or len(sources) > 1 or len(assignments) > 1:
            raise ParseError(f"invalid dataInputAssociation on {element.get('id')!r}")
        target_id = targets[0].text or ""
        if target_id not in input_by_id or input_by_id[target_id] in input_associations:
            raise ParseError(f"dataInputAssociation has unknown or duplicate target {target_id!r}")
        source_ref = (sources[0].text or "") if sources else None
        literal: str | None = None
        if source_ref is not None:
            if assignments:
                raise ParseError("dataInputAssociation cannot contain both sourceRef and assignment")
        else:
            if len(assignments) != 1:
                raise ParseError("literal dataInputAssociation requires one assignment")
            from_items = _children(assignments[0], "from")
            to_items = _children(assignments[0], "to")
            if len(from_items) != 1 or len(to_items) != 1 or (to_items[0].text or "") != target_id:
                raise ParseError("dataInputAssociation assignment does not target its dataInput")
            literal = from_items[0].text or ""
        input_associations[input_by_id[target_id]] = _Binding(source_ref, literal)
    if set(input_associations) != set(input_by_name):
        missing = sorted(set(input_by_name) - set(input_associations))
        raise ParseError(f"missing input associations on {element.get('id')!r}: {missing}")

    output_associations: dict[str, str] = {}
    for association in _children(element, "dataOutputAssociation"):
        _check_children(association, {"sourceRef", "targetRef"})
        sources = _children(association, "sourceRef")
        targets = _children(association, "targetRef")
        if len(sources) != 1 or len(targets) != 1:
            raise ParseError(f"invalid dataOutputAssociation on {element.get('id')!r}")
        source_id = sources[0].text or ""
        target_id = targets[0].text or ""
        if source_id not in output_by_id or output_by_id[source_id] in output_associations:
            raise ParseError(f"dataOutputAssociation has unknown or duplicate source {source_id!r}")
        output_associations[output_by_id[source_id]] = target_id
    if set(output_associations) != set(output_by_name):
        missing = sorted(set(output_by_name) - set(output_associations))
        raise ParseError(f"missing output associations on {element.get('id')!r}: {missing}")
    return input_associations, output_associations


def _value_ref(
    binding: _Binding,
    value_type: str | None,
    parameters: set[str],
    properties: set[str],
) -> Any:
    if binding.source_ref is not None:
        name = binding.source_ref
        if name in parameters:
            return {"param": name}
        if name in properties:
            return {"property": name}
        raise ParseError(f"data association references unknown process value {name!r}")
    if binding.literal is None or value_type is None:
        raise ParseError("literal input has no recoverable IR type")
    value: Any = binding.literal
    if value_type == "code" and value.startswith("concept:"):
        return {"concept": value[len("concept:") :]}
    if value_type == "boolean":
        if value not in {"true", "false"}:
            raise ParseError(f"invalid boolean literal {value!r}")
        value = value == "true"
    elif value_type == "integer":
        try:
            value = int(value)
        except ValueError as exc:
            raise ParseError(f"invalid integer literal {value!r}") from exc
    elif value_type in {"decimal", "quantity"}:
        try:
            value = int(value) if _INT_RE.fullmatch(value) else float(value)
        except ValueError as exc:
            raise ParseError(f"invalid numeric literal {value!r}") from exc
        if isinstance(value, float) and not math.isfinite(value):
            raise ParseError(f"invalid numeric literal {binding.literal!r}")
    elif value_type == "string":
        raise ParseError("string literals are not permitted as ValueRef inputs")
    try:
        return {"literal": value, "type": value_type}
    except ValidationError as exc:
        raise ParseError(f"invalid {value_type} literal {binding.literal!r}: {exc}") from exc


def _parse_timer(element: etree._Element) -> TimerEventDefinition:
    definition = _one_child(element, "timerEventDefinition")
    assert definition is not None
    _check_children(definition, {"timeDuration", "timeCycle"})
    durations = _children(definition, "timeDuration")
    cycles = _children(definition, "timeCycle")
    if len(durations) + len(cycles) != 1:
        raise ParseError(f"timer {element.get('id')!r} requires exactly one timer expression")
    target = durations[0] if durations else cycles[0]
    expression = target.text or ""
    match = re.fullmatch(r"#\{([a-z][a-z0-9_]*)\}", expression)
    value_ref = (
        {"param": match.group(1)}
        if match
        else {"literal": expression, "type": "duration"}
    )
    try:
        if durations:
            return TimerEventDefinition(timeDuration=value_ref)
        return TimerEventDefinition(timeCycle=value_ref)
    except ValidationError as exc:
        raise ParseError(f"invalid timer expression {expression!r}: {exc}") from exc


def _parse_ir_node(
    element: etree._Element,
    *,
    element_id: str | None = None,
    parameters: set[str],
    properties: set[str],
    catalog: Catalog,
) -> Any:
    local = _local(element)
    identifier = element_id or element.get("id") or ""
    name = element.get("name", "")
    acp, source_condition = _element_metadata(element)
    if source_condition is not None:
        raise ParseError(f"non-flow element {identifier!r} has a source condition")

    if local == "startEvent":
        _check_children(element, {"extensionElements", "incoming", "outgoing"})
        return StartEvent(type="startEvent", id=identifier, name=name, acp=acp)
    if local == "endEvent":
        _check_children(element, {"extensionElements", "incoming", "outgoing"})
        return EndEvent(type="endEvent", id=identifier, name=name, acp=acp)
    if local == "exclusiveGateway":
        _check_children(element, {"extensionElements", "incoming", "outgoing"})
        return ExclusiveGateway(
            type="exclusiveGateway",
            id=identifier,
            name=name,
            gatewayDirection=element.get("gatewayDirection"),
            default=element.get("default"),
            acp=acp,
        )
    if local == "boundaryEvent":
        _check_children(element, {"extensionElements", "incoming", "outgoing", "timerEventDefinition"})
        cancel_activity = element.get("cancelActivity", "true")
        if cancel_activity not in {"true", "false"}:
            raise ParseError(f"invalid cancelActivity on boundaryEvent {identifier!r}")
        return BoundaryEvent(
            type="boundaryEvent",
            id=identifier,
            name=name,
            attachedToRef=element.get("attachedToRef"),
            cancelActivity=cancel_activity == "true",
            timerEventDefinition=_parse_timer(element),
            acp=acp,
        )
    if local == "intermediateCatchEvent":
        _check_children(element, {"extensionElements", "incoming", "outgoing", "timerEventDefinition"})
        return IntermediateCatchEvent(
            type="intermediateCatchEvent",
            id=identifier,
            name=name,
            timerEventDefinition=_parse_timer(element),
            acp=acp,
        )
    if local not in {"task", "userTask", "businessRuleTask"}:
        raise ParseError(f"unexpected IR flow element type bpmn2:{local}")

    _check_children(
        element,
        {
            "extensionElements",
            "incoming",
            "outgoing",
            "ioSpecification",
            "dataInputAssociation",
            "dataOutputAssociation",
        },
    )
    inputs, outputs = _io_bindings(element)
    if local == "task":
        work_name = element.get(_DROOLS + "taskName")
        if not work_name:
            raise ParseError(f"task {identifier!r} is missing drools:taskName")
        try:
            capability = catalog.get(work_name)
        except (KeyError, ValueError) as exc:
            raise ParseError(f"task {identifier!r} has unknown capability {work_name!r}") from exc
        task_name_binding = inputs.pop("TaskName", None)
        if task_name_binding is None or task_name_binding.source_ref is not None:
            raise ParseError(f"task {identifier!r} requires a literal TaskName input")
        expected_label = task_label(work_name, identifier)
        if task_name_binding.literal != expected_label:
            raise ParseError(
                f"task {identifier!r} TaskName literal {task_name_binding.literal!r} "
                f"does not equal task_label {expected_label!r}"
            )
        task_inputs: dict[str, Any] = {}
        for input_name, binding in inputs.items():
            try:
                spec = next(item for item in capability.inputs if item.name == input_name)
            except StopIteration as exc:
                raise ParseError(
                    f"task {identifier!r} has undeclared input {input_name!r}"
                ) from exc
            task_inputs[input_name] = _value_ref(
                binding, spec.type, parameters, properties
            )
        return Task(
            type="task",
            id=identifier,
            name=name,
            taskName=work_name,
            inputs=task_inputs,
            outputs=outputs,
            acp=acp,
        )

    if local == "userTask":
        task_name = inputs.pop("TaskName", None)
        skippable = inputs.pop("Skippable", None)
        group = inputs.pop("GroupId", None)
        if task_name != _Binding(None, identifier):
            raise ParseError(f"userTask {identifier!r} must set TaskName to its id")
        if skippable != _Binding(None, "false"):
            raise ParseError(f"userTask {identifier!r} must set Skippable to false")
        group_names = {value: key for key, value in GROUP_IDS.items()}
        if group is None or group.source_ref is not None or group.literal not in group_names:
            raise ParseError(f"userTask {identifier!r} has an invalid GroupId")
        if inputs:
            raise ParseError(f"userTask {identifier!r} has unexpected inputs {sorted(inputs)}")
        return UserTask(
            type="userTask",
            id=identifier,
            name=name,
            groupId=group_names[group.literal],
            outputs=outputs,
            acp=acp,
        )

    if element.get("implementation") != DMN_IMPLEMENTATION:
        raise ParseError(f"businessRuleTask {identifier!r} has an unsupported implementation")
    namespace = inputs.pop("namespace", None)
    model = inputs.pop("model", None)
    if namespace is None or namespace.source_ref is not None or not namespace.literal:
        raise ParseError(f"businessRuleTask {identifier!r} requires a namespace input")
    if model is None or model.source_ref is not None or not model.literal:
        raise ParseError(f"businessRuleTask {identifier!r} requires a model input")
    dmn_inputs: dict[str, Any] = {}
    for input_name, binding in inputs.items():
        if binding.source_ref is not None:
            dmn_inputs[input_name] = _value_ref(
                binding, None, parameters, properties
            )
        elif binding.literal is not None and binding.literal.startswith("concept:"):
            dmn_inputs[input_name] = {
                "concept": binding.literal[len("concept:") :]
            }
        else:
            raise ParseError(
                f"businessRuleTask {identifier!r} literal input {input_name!r} "
                "has no recoverable IR type"
            )
    return BusinessRuleTask(
        type="businessRuleTask",
        id=identifier,
        name=name,
        dmnModel=model.literal,
        inputs=dmn_inputs,
        outputs=outputs,
        acp=acp,
    )


def _parse_flow(
    element: etree._Element,
    *,
    type_environment: dict[str, TypeInfo],
    parameters: set[str],
    properties: set[str],
) -> SequenceFlow:
    _check_children(element, {"extensionElements", "conditionExpression"})
    acp, source_expression = _element_metadata(element, is_flow=True)
    condition_element = _one_child(element, "conditionExpression", required=False)
    java_expression = condition_element.text if condition_element is not None else None
    if source_expression is None and java_expression is not None:
        raise ParseError(
            f"sequenceFlow {element.get('id')!r} has Java condition without acp-expr source"
        )
    if source_expression is not None:
        if java_expression is None:
            raise ParseError(
                f"sequenceFlow {element.get('id')!r} has acp-expr source without Java condition"
            )
        if condition_element.get("language") != CONDITION_LANGUAGE:
            raise ParseError(f"sequenceFlow {element.get('id')!r} has unsupported condition language")
        try:
            expected_java = to_java(source_expression, type_environment)
        except ExpressionError as exc:
            raise ParseError(
                f"invalid acp-expr on sequenceFlow {element.get('id')!r}: {exc}"
            ) from exc
        if java_expression != expected_java:
            raise ParseError(
                f"sequenceFlow {element.get('id')!r} Java condition differs from its acp-expr source"
            )
    return SequenceFlow(
        type="sequenceFlow",
        id=element.get("id"),
        name=element.get("name", ""),
        sourceRef=element.get("sourceRef"),
        targetRef=element.get("targetRef"),
        conditionExpression=source_expression,
        acp=acp,
    )


def _parse_properties(
    process_element: etree._Element,
    acp_process: AcpProcess,
    item_definitions: dict[str, str],
) -> list[Property]:
    parameters = {parameter.name: parameter for parameter in acp_process.parameters}
    result: list[Property] = []
    seen: set[str] = set()
    for element in _children(process_element, "property"):
        _check_children(element, {"extensionElements"})
        name = element.get("name")
        if not name or name in seen or element.get("id") != name:
            raise ParseError(f"invalid or duplicate process property {name!r}")
        seen.add(name)
        extension = _one_child(element, "extensionElements")
        assert extension is not None
        metadata = [
            child
            for child in extension
            if child.tag == _DROOLS + "metaData"
        ]
        by_name = {item.get("name"): item.text or "" for item in metadata}
        if len(by_name) != len(metadata) or set(by_name) - {"acp.type", "acp.unit"}:
            raise ParseError(f"invalid property metadata for {name!r}")
        value_type = by_name.get("acp.type")
        if not value_type or element.get("itemSubjectRef") not in item_definitions:
            raise ParseError(f"property {name!r} is missing type metadata or itemDefinition")
        structure_ref = item_definitions[element.get("itemSubjectRef", "")]
        expected_structure = STRUCTURE_REF.get(value_type)
        if expected_structure != structure_ref:
            raise ParseError(f"property {name!r} type does not match its itemDefinition")
        unit = by_name.get("acp.unit")
        if name in parameters:
            parameter = parameters[name]
            if parameter.type != value_type or parameter.unit != unit:
                raise ParseError(f"parameter property {name!r} differs from acp:template")
            continue
        if name == "outcome":
            if value_type != "string" or unit is not None:
                raise ParseError("generated outcome property must have type string")
            continue
        try:
            result.append(Property(name=name, type=value_type, unit=unit))
        except ValidationError as exc:
            raise ParseError(f"invalid process property {name!r}: {exc}") from exc
    missing_parameters = sorted(set(parameters) - seen)
    if missing_parameters:
        raise ParseError(f"parameter properties are missing: {missing_parameters}")
    if "outcome" not in seen:
        raise ParseError("generated outcome property is missing")
    return result


def _is_generated_node_id(identifier: str) -> bool:
    return identifier in _FIXED_GENERATED_NODES or identifier.startswith("main_") or identifier.endswith(
        ("_counter", "_reset", "_outcome")
    )


def _is_generated_flow_id(identifier: str) -> bool:
    return identifier in _FIXED_GENERATED_FLOWS or identifier.endswith(
        ("_counter_in", "_reset_out", "_outcome_out", "_route")
    )


def _restore_flow(
    flow: SequenceFlow,
    *,
    start_id: str,
    reset_targets: dict[str, str],
    counter_scripts: dict[str, str],
    outcome_scripts: dict[str, str],
) -> SequenceFlow:
    source = flow.sourceRef
    target = flow.targetRef
    if source == "main_start":
        source = start_id
    source = counter_scripts.get(source, source)
    if target.endswith("_reset"):
        if flow.id not in reset_targets:
            raise ParseError(f"flow {flow.id!r} targets a reset script without reset output")
        target = reset_targets[flow.id]
    if target in outcome_scripts:
        target = outcome_scripts[target]
    try:
        return flow.model_copy(update={"sourceRef": source, "targetRef": target})
    except ValidationError as exc:
        raise ParseError(f"cannot restore flow {flow.id!r}: {exc}") from exc


@trace(name="bpmn.parse")
def parse(xml: str | bytes, *, catalog: Catalog | None = None) -> ProcessIR:
    """Parse a supported BPMN document and restore its canonical ProcessIR."""
    try:
        raw = xml.encode("utf-8") if isinstance(xml, str) else xml
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
        )
        root = etree.fromstring(raw, parser=parser)
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            if _is_diagram_subtree(element):
                continue
            qname = etree.QName(element)
            supported = (
                qname.namespace == BPMN_MODEL_NS and qname.localname in _ALLOWED_TAGS
            ) or (
                qname.namespace == ACP_NS
                and qname.localname in {"template", "element"}
            ) or (qname.namespace == DROOLS_NS and qname.localname == "metaData")
            if not supported:
                raise ParseError(f"unexpected XML element {{{qname.namespace}}}{qname.localname}")
        if root.tag != _BPMN + "definitions":
            raise ParseError("document root must be bpmn2:definitions")
        _check_children(root, {"itemDefinition", "process"})
        process_elements = [child for child in root if child.tag == _BPMN + "process"]
        if len(process_elements) != 1:
            raise ParseError(f"definitions must contain exactly one process; found {len(process_elements)}")
        process_element = process_elements[0]
        _check_children(
            process_element,
            {
                "extensionElements",
                "property",
                "startEvent",
                "endEvent",
                "task",
                "userTask",
                "businessRuleTask",
                "exclusiveGateway",
                "intermediateCatchEvent",
                "boundaryEvent",
                "scriptTask",
                "subProcess",
                "sequenceFlow",
            },
        )
        template, acp_process = _template_metadata(process_element)
        process_id = process_element.get("id") or ""
        if template["template_id"] != process_id:
            raise ParseError("acp:template template_id differs from bpmn2:process id")
        if process_element.get("name") is None:
            raise ParseError("bpmn2:process is missing name")
        if process_element.get("isExecutable") != "true":
            raise ParseError("bpmn2:process isExecutable must be true")
        if process_element.get(_DROOLS + "version") != template["version"]:
            raise ParseError("drools:version differs from acp:template version")
        if root.get("id") != f"{process_id}_defs":
            raise ParseError("definitions id differs from the compiler-generated process id")
        if process_element.get("processType") != "Public":
            raise ParseError("bpmn2:process processType must be Public")

        item_definitions: dict[str, str] = {}
        for item in [child for child in root if child.tag == _BPMN + "itemDefinition"]:
            item_id = item.get("id")
            structure_ref = item.get("structureRef")
            if not item_id or not structure_ref or item_id in item_definitions:
                raise ParseError("invalid or duplicate itemDefinition")
            item_definitions[item_id] = structure_ref
        properties = _parse_properties(process_element, acp_process, item_definitions)
        parameters = {parameter.name for parameter in acp_process.parameters}
        property_names = {prop.name for prop in properties}
        type_environment = {
            name: TypeInfo(parameter.type, parameter.unit)
            for parameter in acp_process.parameters
            for name in [parameter.name]
        }
        type_environment.update(
            {prop.name: TypeInfo(prop.type, prop.unit) for prop in properties}
        )
        active_catalog = load_catalog() if catalog is None else catalog

        # Every BPMN flow node, including generated macro nodes, has one metadata block.
        for element in process_element.iter():
            if (
                element.tag.startswith(_BPMN)
                and _local(element) in _NODE_TAGS | {"sequenceFlow"}
            ):
                _metadata_json(element, "element")

        subprocesses = _children(process_element, "subProcess")
        if len(subprocesses) != 1 or subprocesses[0].get("id") != "main":
            raise ParseError("process requires exactly one generated main subProcess")
        main = subprocesses[0]
        _check_children(
            main,
            {
                "extensionElements",
                "incoming",
                "outgoing",
                "startEvent",
                "endEvent",
                "task",
                "userTask",
                "businessRuleTask",
                "exclusiveGateway",
                "intermediateCatchEvent",
                "boundaryEvent",
                "scriptTask",
                "sequenceFlow",
            },
        )
        outer_starts = _children(process_element, "startEvent")
        if len(outer_starts) != 1:
            raise ParseError(f"process requires one outer startEvent; found {len(outer_starts)}")
        start = _parse_ir_node(
            outer_starts[0], parameters=parameters, properties=property_names, catalog=active_catalog
        )
        if not isinstance(start, StartEvent):
            raise ParseError("outer process start element did not parse as startEvent")
        start_id = start.id
        parsed_nodes: dict[str, Any] = {start.id: start}
        transformed_ends: dict[str, Any] = {}

        for child in main:
            if not child.tag.startswith(_BPMN):
                continue
            local = _local(child)
            if local not in _NODE_TAGS:
                continue
            identifier = child.get("id") or ""
            if local == "startEvent" and identifier == "main_start":
                continue
            if local == "scriptTask":
                if not _is_generated_node_id(identifier):
                    raise ParseError(f"unexpected scriptTask {identifier!r} inside main")
                _check_children(child, {"extensionElements", "incoming", "outgoing", "script"})
                continue
            if local == "endEvent" and identifier.startswith("main_"):
                original_id = identifier[len("main_") :]
                node = _parse_ir_node(
                    child,
                    element_id=original_id,
                    parameters=parameters,
                    properties=property_names,
                    catalog=active_catalog,
                )
                if not isinstance(node, EndEvent):
                    raise ParseError(f"generated inner end {identifier!r} is not an endEvent")
                transformed_ends[original_id] = node
                parsed_nodes[original_id] = node
                continue
            if _is_generated_node_id(identifier):
                raise ParseError(f"unexpected generated node {identifier!r} in main body")
            node = _parse_ir_node(
                child,
                parameters=parameters,
                properties=property_names,
                catalog=active_catalog,
            )
            if node.id in parsed_nodes:
                raise ParseError(f"duplicate recovered flow element id {node.id!r}")
            parsed_nodes[node.id] = node

        # The top-level end nodes for outcomes are macro artefacts; boundary-only ends are IR.
        for child in _children(process_element, "endEvent"):
            identifier = child.get("id") or ""
            if identifier == "end_completed" or identifier in transformed_ends:
                continue
            if identifier.startswith("main_") or identifier.endswith(
                ("_counter", "_reset", "_outcome")
            ):
                raise ParseError(f"unexpected generated end event {identifier!r}")
            node = _parse_ir_node(
                child,
                parameters=parameters,
                properties=property_names,
                catalog=active_catalog,
            )
            if node.id in parsed_nodes:
                raise ParseError(f"duplicate recovered flow element id {node.id!r}")
            parsed_nodes[node.id] = node

        for child in _children(process_element, "boundaryEvent"):
            node = _parse_ir_node(
                child,
                parameters=parameters,
                properties=property_names,
                catalog=active_catalog,
            )
            if node.id in parsed_nodes:
                raise ParseError(f"duplicate recovered flow element id {node.id!r}")
            parsed_nodes[node.id] = node

        # Only the generated gateway may remain at process scope.
        for child in _children(process_element, "exclusiveGateway"):
            if child.get("id") != "gw_outcome":
                raise ParseError(f"unexpected top-level exclusiveGateway {child.get('id')!r}")
        for local in ("task", "userTask", "businessRuleTask", "intermediateCatchEvent"):
            if _children(process_element, local):
                raise ParseError(f"unexpected top-level bpmn2:{local}")
        for child in _children(process_element, "scriptTask"):
            identifier = child.get("id") or ""
            if not _is_generated_node_id(identifier):
                raise ParseError(f"unexpected top-level scriptTask {identifier!r}")
            _check_children(child, {"extensionElements", "incoming", "outgoing", "script"})

        all_flows = [
            element for element in process_element.iter(_BPMN + "sequenceFlow")
        ]
        by_id = {element.get("id"): element for element in all_flows}
        if len(by_id) != len(all_flows) or None in by_id:
            raise ParseError("duplicate or missing sequenceFlow id")
        counter_scripts: dict[str, str] = {}
        reset_targets: dict[str, str] = {}
        outcome_scripts: dict[str, str] = {}
        for identifier, element in by_id.items():
            source = element.get("sourceRef") or ""
            target = element.get("targetRef") or ""
            if identifier.endswith("_counter_in"):
                if not source or not target or not target.endswith("_counter"):
                    raise ParseError(f"invalid counter-in flow {identifier!r}")
                counter_scripts[target] = source
            elif identifier.endswith("_reset_out"):
                source_id = identifier[: -len("_reset_out")] + "_reset"
                if source != source_id or not target:
                    raise ParseError(f"invalid reset-out flow {identifier!r}")
                reset_targets[identifier[: -len("_reset_out")]] = target
            elif identifier.endswith("_outcome_out"):
                end_id = identifier[: -len("_outcome_out")]
                if source != end_id + "_outcome" or target != "main_" + end_id:
                    raise ParseError(f"invalid outcome-out flow {identifier!r}")
                outcome_scripts[end_id + "_outcome"] = end_id

        parsed_flows: dict[str, SequenceFlow] = {}
        for child in [*_children(main, "sequenceFlow"), *_children(process_element, "sequenceFlow")]:
            identifier = child.get("id") or ""
            if _is_generated_flow_id(identifier):
                continue
            flow = _parse_flow(
                child,
                type_environment=type_environment,
                parameters=parameters,
                properties=property_names,
            )
            restored = _restore_flow(
                flow,
                start_id=start_id,
                reset_targets=reset_targets,
                counter_scripts=counter_scripts,
                outcome_scripts=outcome_scripts,
            )
            if restored.id in parsed_flows:
                raise ParseError(f"duplicate recovered sequenceFlow id {restored.id!r}")
            parsed_flows[restored.id] = restored

        if process_element.get("name") is None:
            raise ParseError("bpmn2:process name is missing")
        process = Process(
            id=process_id,
            name=process_element.get("name", ""),
            description=template["description"],
            isExecutable=True,
            kind=template["kind"],
            properties=properties,
            flowElements=sorted(
                [*parsed_nodes.values(), *parsed_flows.values()], key=lambda item: item.id
            ),
            acp=acp_process,
        )
        result = ProcessIR(ir_version=template["ir_version"], process=process)
        if template["version"] != ir_version(result):
            raise ParseError("acp:template version does not match the reconstructed IR")
        if root.get("targetNamespace") != f"{ACP_NS}/{acp_process.provenance.source_cpg}":
            raise ParseError("definitions targetNamespace differs from process provenance")
        return result
    except ParseError:
        raise
    except (etree.XMLSyntaxError, ValidationError, ExpressionError, TypeError, ValueError, KeyError) as exc:
        raise ParseError(str(exc)) from exc
