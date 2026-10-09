"""Deterministic BPMN XML serialization for the supported Kogito profile."""

from __future__ import annotations

import json

from lxml import etree

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.compiler.expanded import XFlow, XIo, XNode, XProcess
from cpg_contracts.automation.compiler.kogito_profile import (
    ACP_NS,
    BPMN_MODEL_NS,
    CONDITION_LANGUAGE,
    DROOLS_NS,
    NAMESPACES,
    PACKAGE_NAME,
    XSI_NS,
)
from cpg_contracts.automation.expressions import TypeInfo, to_java


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _bpmn(local_name: str) -> str:
    return _tag(BPMN_MODEL_NS, local_name)


def _drools(local_name: str) -> str:
    return _tag(DROOLS_NS, local_name)


def _acp(local_name: str) -> str:
    return _tag(ACP_NS, local_name)


def _formal_expression(parent: etree._Element, name: str, text: str) -> etree._Element:
    element = etree.SubElement(
        parent,
        _bpmn(name),
        {
            f"{{{XSI_NS}}}type": "bpmn2:tFormalExpression",
        },
    )
    element.text = text
    return element


def _append_element_payload(parent: etree._Element, payload: str | None) -> None:
    extensions = etree.SubElement(parent, _bpmn("extensionElements"))
    metadata = etree.SubElement(extensions, _acp("element"))
    metadata.text = etree.CDATA(payload if payload is not None else "{}")


def _append_io(node_element: etree._Element, node: XNode) -> None:
    if not node.io:
        return
    io_specification = etree.SubElement(node_element, _bpmn("ioSpecification"))
    input_items = [io for io in node.io if io.direction == "input"]
    output_items = [io for io in node.io if io.direction == "output"]
    for io in input_items:
        attributes = {"id": io.id, "name": io.name}
        if io.drools_type is not None:
            attributes[_drools("dtype")] = io.drools_type
        if io.item_subject_ref is not None:
            attributes["itemSubjectRef"] = io.item_subject_ref
        etree.SubElement(io_specification, _bpmn("dataInput"), attributes)
    for io in output_items:
        attributes = {"id": io.id, "name": io.name}
        if io.drools_type is not None:
            attributes[_drools("dtype")] = io.drools_type
        if io.item_subject_ref is not None:
            attributes["itemSubjectRef"] = io.item_subject_ref
        etree.SubElement(io_specification, _bpmn("dataOutput"), attributes)

    input_set = etree.SubElement(io_specification, _bpmn("inputSet"))
    for io in input_items:
        etree.SubElement(input_set, _bpmn("dataInputRefs")).text = io.id
    output_set = etree.SubElement(io_specification, _bpmn("outputSet"))
    for io in output_items:
        etree.SubElement(output_set, _bpmn("dataOutputRefs")).text = io.id

    for io in input_items:
        _append_input_association(node_element, io)
    for io in output_items:
        _append_output_association(node_element, io)


def _append_input_association(parent: etree._Element, io: XIo) -> None:
    association = etree.SubElement(parent, _bpmn("dataInputAssociation"))
    if io.source_ref is not None:
        etree.SubElement(association, _bpmn("sourceRef")).text = io.source_ref
    etree.SubElement(association, _bpmn("targetRef")).text = io.id
    if io.literal is not None:
        assignment = etree.SubElement(association, _bpmn("assignment"))
        _formal_expression(assignment, "from", io.literal)
        _formal_expression(assignment, "to", io.id)
    elif io.source_ref is None:
        raise ValueError(f"input {io.name!r} has neither a source property nor a literal")


def _append_output_association(parent: etree._Element, io: XIo) -> None:
    if io.target_ref is None:
        raise ValueError(f"output {io.name!r} has no mapped target property")
    association = etree.SubElement(parent, _bpmn("dataOutputAssociation"))
    etree.SubElement(association, _bpmn("sourceRef")).text = io.id
    etree.SubElement(association, _bpmn("targetRef")).text = io.target_ref


def _scope_references(node: XNode, flows: list[XFlow]) -> tuple[list[str], list[str]]:
    incoming = [flow.id for flow in flows if flow.target_ref == node.id]
    outgoing = [flow.id for flow in flows if flow.source_ref == node.id]
    return incoming, outgoing


def _append_node(
    parent: etree._Element,
    node: XNode,
    scope_flows: list[XFlow],
    type_environment: dict[str, TypeInfo],
) -> etree._Element:
    attributes: dict[str, str] = {"id": node.id}
    if node.name is not None:
        attributes["name"] = node.name
    for key, value in node.attributes.items():
        attributes[key] = "true" if value is True else "false" if value is False else str(value)
    if node.work_name is not None:
        attributes[_drools("taskName")] = node.work_name
    if node.type == "businessRuleTask" and node.implementation is not None:
        attributes["implementation"] = node.implementation
    if node.script_format is not None:
        attributes["scriptFormat"] = node.script_format

    element = etree.SubElement(parent, _bpmn(node.type), attributes)
    _append_element_payload(element, node.element_payload)
    incoming, outgoing = _scope_references(node, scope_flows)
    for flow_id in incoming:
        etree.SubElement(element, _bpmn("incoming")).text = flow_id
    for flow_id in outgoing:
        etree.SubElement(element, _bpmn("outgoing")).text = flow_id
    _append_io(element, node)

    if node.timer_kind is not None:
        timer_definition = etree.SubElement(element, _bpmn("timerEventDefinition"))
        timer = etree.SubElement(
            timer_definition,
            _bpmn(node.timer_kind),
            {f"{{{XSI_NS}}}type": "bpmn2:tFormalExpression"},
        )
        timer.text = node.timer_expression or ""
    if node.script is not None:
        etree.SubElement(element, _bpmn("script")).text = node.script
    if node.type == "subProcess":
        for child in node.children:
            _append_node(element, child, node.flows, type_environment)
        for flow in node.flows:
            _append_flow(element, flow, type_environment)
    return element


def _append_flow(
    parent: etree._Element,
    flow: XFlow,
    type_environment: dict[str, TypeInfo],
) -> etree._Element:
    attributes = {
        "id": flow.id,
        "sourceRef": flow.source_ref,
        "targetRef": flow.target_ref,
    }
    if flow.name:
        attributes["name"] = flow.name
    element = etree.SubElement(parent, _bpmn("sequenceFlow"), attributes)
    _append_element_payload(element, flow.element_payload)
    if flow.condition_expression is not None:
        if flow.condition_is_java:
            condition = flow.condition_expression
        else:
            condition = to_java(flow.condition_expression, type_environment)
        expression = etree.SubElement(
            element,
            _bpmn("conditionExpression"),
            {
                f"{{{XSI_NS}}}type": "bpmn2:tFormalExpression",
                "language": CONDITION_LANGUAGE,
            },
        )
        expression.text = etree.CDATA(condition)
    return element


@trace
def serialize(process: XProcess) -> str:
    """Serialize one expanded process to deterministic Kogito-profile BPMN XML."""
    root = etree.Element(
        _bpmn("definitions"),
        nsmap=NAMESPACES,
        attrib={
            "id": process.definitions_id,
            "targetNamespace": process.target_namespace,
        },
    )
    for item_id, structure_ref in process.item_definitions:
        etree.SubElement(
            root,
            _bpmn("itemDefinition"),
            {"id": item_id, "structureRef": structure_ref},
        )

    model = etree.SubElement(
        root,
        _bpmn("process"),
        {
            "id": process.id,
            "name": process.name,
            "isExecutable": "true",
            "processType": "Public",
            _drools("packageName"): PACKAGE_NAME,
            _drools("version"): process.version,
        },
    )
    extensions = etree.SubElement(model, _bpmn("extensionElements"))
    catalog_version = etree.SubElement(
        extensions,
        _drools("metaData"),
        {"name": "acp.catalogVersion"},
    )
    catalog_version.text = process.catalog_version
    template = etree.SubElement(extensions, _acp("template"))
    template.text = etree.CDATA(
        json.dumps(
            process.template_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )

    item_by_type = {structure_ref: item_id for item_id, structure_ref in process.item_definitions}
    for prop in process.properties:
        element = etree.SubElement(
            model,
            _bpmn("property"),
            {
                "id": prop.id,
                "itemSubjectRef": item_by_type[prop.structure_ref],
                "name": prop.name,
            },
        )
        property_extensions = etree.SubElement(element, _bpmn("extensionElements"))
        type_metadata = etree.SubElement(
            property_extensions,
            _drools("metaData"),
            {"name": "acp.type"},
        )
        type_metadata.text = prop.type
        if prop.unit is not None:
            unit_metadata = etree.SubElement(
                property_extensions,
                _drools("metaData"),
                {"name": "acp.unit"},
            )
            unit_metadata.text = prop.unit

    type_environment = {
        prop.name: TypeInfo(prop.type, prop.unit) for prop in process.properties
    }
    for node in process.nodes:
        _append_node(model, node, process.flows, type_environment)
    for flow in process.flows:
        _append_flow(model, flow, type_environment)

    etree.indent(root, space="  ")
    xml = etree.tostring(
        root,
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
    ).decode("UTF-8")
    return xml.replace("\r\n", "\n").replace("\r", "\n")
