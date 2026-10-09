"""L4 checks for capability and DMN bindings in automation definitions.

``UCUM_UNITS`` is a cache of clinical unit codes this project uses, not a
complete UCUM terminology. Well-formed codes outside that cache produce a
warning rather than a rejection; HAPI validates ``Quantity.code`` authoritatively
at the FHIR boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
import re

from lxml import etree

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.catalog import (
    RESERVED_TASK_NAMES,
    Capability,
    CapabilityIO,
    Catalog,
)
from cpg_contracts.automation.compiler.kogito_profile import task_label
from cpg_contracts.automation.ir import (
    BusinessRuleTask,
    ConceptRef,
    Literal_,
    ParamRef,
    ProcessIR,
    PropertyRef,
    Task,
    UserTask,
)
from cpg_contracts.automation.validators.results import Finding, Severity
from cpg_contracts.decisions import DecisionModelSummary


BPMN_MODEL_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
DROOLS_NS = "http://www.jboss.org/drools"
_BPMN = f"{{{BPMN_MODEL_NS}}}"
_DROOLS_TASK_NAME = f"{{{DROOLS_NS}}}taskName"
UCUM_UNITS = frozenset(
    {
        "mm[Hg]",
        "%",
        "mg/dL",
        "mmol/L",
        "umol/L",
        "g/dL",
        "g/L",
        "kg",
        "g",
        "cm",
        "m",
        "kg/m2",
        "Cel",
        "[degF]",
        "/min",
        "{beats}/min",
        "mL/min",
        "mL/min/{1.73_m2}",
        "mmol/mol",
        "[IU]/L",
        "U/L",
        "ng/mL",
        "ug/L",
        "1",
        "d",
        "h",
        "min",
        "s",
    }
)
_UCUM_ATOM = r"(?:[A-Za-z]+(?:\[[A-Za-z0-9_.%+\-]+\])?|\[[A-Za-z0-9_.%+\-]+\]|%|1|\{[A-Za-z0-9_.%+\-]+\})"
_UCUM_TERM = rf"{_UCUM_ATOM}(?:\{{[A-Za-z0-9_.%+\-]+\}})?(?:[+-]?\d+)?"
UCUM_SYNTAX = re.compile(rf"^/?{_UCUM_TERM}(?:[./]{_UCUM_TERM})*$")


def _finding(
    code: str,
    message: str,
    *,
    element_id: str | None = None,
    severity: Severity = "ERROR",
) -> Finding:
    return Finding(
        rung="L4",
        severity=severity,
        code=code,
        message=message,
        element_id=element_id,
    )


def _unit_findings(value: str, *, element_id: str | None) -> list[Finding]:
    if not UCUM_SYNTAX.fullmatch(value):
        return [
            _finding(
                "unit-syntax",
                f"unit {value!r} is not well-formed under the supported UCUM syntax",
                element_id=element_id,
            )
        ]
    if value not in UCUM_UNITS:
        return [
            _finding(
                "unit-unknown",
                f"unit {value!r} is well-formed but is not in the project's curated UCUM code cache",
                element_id=element_id,
                severity="WARNING",
            )
        ]
    return []


def _value_type(
    value: object,
    parameters: Mapping[str, object],
    properties: Mapping[str, object],
) -> tuple[str | None, str | None, object | None]:
    if isinstance(value, ParamRef):
        parameter = parameters.get(value.param)
        return (
            getattr(parameter, "type", None),
            getattr(parameter, "unit", None),
            parameter,
        )
    if isinstance(value, PropertyRef):
        prop = properties.get(value.property)
        return getattr(prop, "type", None), getattr(prop, "unit", None), prop
    if isinstance(value, ConceptRef):
        return "code", None, None
    if isinstance(value, Literal_):
        return value.type, None, value
    if isinstance(value, dict):
        if "param" in value:
            parameter = parameters.get(value["param"])
            return (
                getattr(parameter, "type", None),
                getattr(parameter, "unit", None),
                parameter,
            )
        if "property" in value:
            prop = properties.get(value["property"])
            return getattr(prop, "type", None), getattr(prop, "unit", None), prop
        if "concept" in value:
            return "code", None, None
        if "literal" in value:
            return value.get("type"), None, value
    return None, None, None


def _input_type_matches(
    value: object,
    capability_io: CapabilityIO,
    parameters: Mapping[str, object],
    properties: Mapping[str, object],
) -> bool:
    actual_type, _, source = _value_type(value, parameters, properties)
    if actual_type == capability_io.type:
        return True
    if capability_io.type == "enum" and isinstance(value, ParamRef):
        return actual_type == "string" and getattr(
            getattr(source, "constraints", None), "enum", None
        ) is not None
    if capability_io.type == "enum" and isinstance(value, dict) and "param" in value:
        return actual_type == "string" and getattr(
            getattr(source, "constraints", None), "enum", None
        ) is not None
    return False


def _check_capability_task(
    task: Task,
    capability: Capability,
    properties: Mapping[str, object],
    parameters: Mapping[str, object],
) -> list[Finding]:
    findings: list[Finding] = []
    identifier = task.id
    inputs = {item.name: item for item in capability.inputs}
    outputs = {item.name: item for item in capability.outputs}

    for name, item in inputs.items():
        if item.required and name not in task.inputs:
            findings.append(
                _finding(
                    "missing-required-input",
                    f"task {identifier!r} omits required input {name!r} for {capability.id!r}",
                    element_id=identifier,
                )
            )
    for name in task.inputs.keys() - inputs.keys():
        findings.append(
            _finding(
                "unknown-input",
                f"task {identifier!r} supplies unknown input {name!r} for {capability.id!r}",
                element_id=identifier,
            )
        )

    unit_input_name = next(
        (item.name for item in capability.inputs if item.type == "unit"),
        None,
    )
    unit_value: str | None = None
    unit_syntax_valid = False
    for name, value in task.inputs.items():
        item = inputs.get(name)
        if item is None:
            continue
        actual_type, _, source = _value_type(value, parameters, properties)
        if not _input_type_matches(value, item, parameters, properties):
            findings.append(
                _finding(
                    "input-type",
                    f"task {identifier!r} input {name!r} has type {actual_type!r}; "
                    f"capability {capability.id!r} requires {item.type!r}",
                    element_id=identifier,
                )
            )

        if item.free_text and not (
            isinstance(value, ParamRef)
            and actual_type == "string"
        ):
            findings.append(
                _finding(
                    "free-text-input",
                    f"free-text input {name!r} on task {identifier!r} must come from a string parameter",
                    element_id=identifier,
                )
            )

        if item.type == "enum" and item.values is not None:
            if isinstance(value, Literal_) and value.type == "enum":
                if value.literal not in item.values:
                    findings.append(
                        _finding(
                            "enum-value",
                            f"task {identifier!r} input {name!r} uses a value outside the catalog enum",
                            element_id=identifier,
                        )
                    )
            elif isinstance(value, ParamRef):
                enum_values = getattr(
                    getattr(source, "constraints", None), "enum", None
                )
                if not enum_values or not set(enum_values) <= set(item.values):
                    findings.append(
                        _finding(
                            "enum-value",
                            f"task {identifier!r} enum parameter {name!r} must constrain values to the catalog enum",
                            element_id=identifier,
                        )
                    )

        if item.type == "unit":
            literal = value.literal if isinstance(value, Literal_) else None
            if not (
                isinstance(value, Literal_)
                and value.type == "unit"
                and isinstance(literal, str)
            ):
                findings.append(
                    _finding(
                        "unit-literal",
                        f"task {identifier!r} unit input {name!r} must be a UCUM literal",
                        element_id=identifier,
                    )
                )
            else:
                unit_value = literal
                unit_syntax_valid = bool(UCUM_SYNTAX.fullmatch(literal))
                findings.extend(_unit_findings(literal, element_id=identifier))

    for name, property_name in task.outputs.items():
        capability_output = outputs.get(name)
        if capability_output is None:
            findings.append(
                _finding(
                    "unknown-output",
                    f"task {identifier!r} maps unknown output {name!r} for {capability.id!r}",
                    element_id=identifier,
                )
            )
            continue
        prop = properties.get(property_name)
        if prop is None:
            findings.append(
                _finding(
                    "output-property",
                    f"task {identifier!r} output {name!r} targets undeclared property {property_name!r}",
                    element_id=identifier,
                )
            )
            continue
        property_type = getattr(prop, "type", None)
        if property_type != capability_output.type:
            findings.append(
                _finding(
                    "output-type",
                    f"task {identifier!r} output {name!r} has catalog type {capability_output.type!r}, "
                    f"but target property {property_name!r} has type {property_type!r}",
                    element_id=identifier,
                )
            )
        if capability_output.type == "quantity":
            if unit_input_name is None or unit_input_name not in task.inputs:
                findings.append(
                    _finding(
                        "unit-mismatch",
                        f"quantity output {name!r} on task {identifier!r} requires a unit input",
                        element_id=identifier,
                    )
                )
            elif unit_syntax_valid and getattr(prop, "unit", None) != unit_value:
                findings.append(
                    _finding(
                        "unit-mismatch",
                        f"quantity output {name!r} on task {identifier!r} uses {unit_value!r}, "
                        f"but target property {property_name!r} declares {getattr(prop, 'unit', None)!r}",
                        element_id=identifier,
                    )
                )
    return findings


def _check_property_targets(
    element: BusinessRuleTask | UserTask,
    properties: Mapping[str, object],
) -> list[Finding]:
    return [
        _finding(
            "output-property",
            f"{element.type} {element.id!r} output {name!r} targets undeclared property {target!r}",
            element_id=element.id,
        )
        for name, target in element.outputs.items()
        if target not in properties
    ]


def _check_dmn_task(
    task: BusinessRuleTask,
    summaries: Mapping[str, DecisionModelSummary] | None,
) -> list[Finding]:
    if summaries is None:
        return []
    summary = summaries.get(task.dmnModel)
    if summary is None:
        return [
            _finding(
                "dmn-model",
                f"businessRuleTask {task.id!r} references unresolved DMN model {task.dmnModel!r}",
                element_id=task.id,
            )
        ]

    findings: list[Finding] = []
    if summary.id != task.dmnModel:
        findings.append(
            _finding(
                "dmn-model",
                f"businessRuleTask {task.id!r} resolves {task.dmnModel!r} to summary id {summary.id!r}",
                element_id=task.id,
            )
        )
    model_inputs = {variable.name for variable in summary.inputs}
    supplied_inputs = set(task.inputs)
    for name in sorted(model_inputs - supplied_inputs):
        findings.append(
            _finding(
                "dmn-input",
                f"businessRuleTask {task.id!r} omits DMN input {name!r}",
                element_id=task.id,
            )
        )
    for name in sorted(supplied_inputs - model_inputs):
        findings.append(
            _finding(
                "dmn-input",
                f"businessRuleTask {task.id!r} supplies unknown DMN input {name!r}",
                element_id=task.id,
            )
        )
    model_outputs = {variable.name for variable in summary.outputs}
    for name in sorted(set(task.outputs) - model_outputs):
        findings.append(
            _finding(
                "dmn-output",
                f"businessRuleTask {task.id!r} maps unknown DMN output {name!r}",
                element_id=task.id,
            )
        )
    return findings


@trace(name="bpmn.l4.catalog")
def validate_catalog(
    ir: ProcessIR,
    catalog: Catalog,
    dmn_summaries: dict[str, DecisionModelSummary] | None = None,
) -> list[Finding]:
    """Check IR task bindings against a capability catalog and known DMNs."""
    process = ir.process
    properties = {prop.name: prop for prop in process.properties}
    parameters = {param.name: param for param in process.acp.parameters}
    findings: list[Finding] = []

    for prop in process.properties:
        if prop.unit is not None:
            findings.extend(_unit_findings(prop.unit, element_id=prop.name))
    for parameter in process.acp.parameters:
        if parameter.unit is not None:
            findings.extend(_unit_findings(parameter.unit, element_id=parameter.name))

    for element in process.flowElements:
        if isinstance(element, Task):
            if element.taskName in RESERVED_TASK_NAMES:
                findings.append(
                    _finding(
                        "reserved-task-name",
                        f"task {element.id!r} uses Kogito-reserved work-item name {element.taskName!r}",
                        element_id=element.id,
                    )
                )
            try:
                capability = catalog.get(element.taskName)
            except KeyError:
                findings.append(
                    _finding(
                        "unknown-capability",
                        f"task {element.id!r} references unknown capability {element.taskName!r}",
                        element_id=element.id,
                    )
                )
                continue
            findings.extend(
                _check_capability_task(
                    element,
                    capability,
                    properties,
                    parameters,
                )
            )
        elif isinstance(element, BusinessRuleTask):
            findings.extend(_check_property_targets(element, properties))
            findings.extend(_check_dmn_task(element, dmn_summaries))
        elif isinstance(element, UserTask):
            findings.extend(_check_property_targets(element, properties))

    return findings


def _taskname_literal(
    task: etree._Element,
    data_input: etree._Element,
) -> tuple[str | None, str | None]:
    input_id = data_input.get("id")
    if not input_id:
        return None, "TaskName dataInput has no id"
    associations = task.xpath(
        "./b:dataInputAssociation[b:targetRef=$input_id]",
        namespaces={"b": BPMN_MODEL_NS},
        input_id=input_id,
    )
    if len(associations) != 1:
        return None, "TaskName input must have exactly one dataInputAssociation"
    association = associations[0]
    if association.find(_BPMN + "sourceRef") is not None:
        return None, "TaskName input must be assigned a literal"
    assignments = association.findall(_BPMN + "assignment")
    if len(assignments) != 1:
        return None, "TaskName input must have exactly one literal assignment"
    sources = assignments[0].findall(_BPMN + "from")
    if len(sources) != 1:
        return None, "TaskName input assignment must contain one from expression"
    return (sources[0].text or "").strip(), None


@trace(name="bpmn.l4.catalog_xml")
def validate_catalog_xml(xml: str, catalog: Catalog) -> list[Finding]:
    """Check BPMN custom-task names and their generated TaskName literals."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(xml.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        return [_finding("xml-parse", f"BPMN XML could not be parsed: {exc}")]
    if root.tag != _BPMN + "definitions":
        return [_finding("bpmn-root", "BPMN document root must be bpmn2:definitions")]

    processes = root.iter(_BPMN + "process")
    findings: list[Finding] = []
    process_count = 0
    for process in processes:
        process_count += 1
        literals: dict[str, str] = {}
        for task in process.iter():
            if task.tag not in {_BPMN + "task", _BPMN + "userTask"}:
                continue
            identifier = task.get("id")
            task_type = etree.QName(task).localname
            work_name = task.get(_DROOLS_TASK_NAME) if task.tag == _BPMN + "task" else None
            if task.tag == _BPMN + "task":
                if work_name in RESERVED_TASK_NAMES:
                    findings.append(
                        _finding(
                            "reserved-task-name",
                            f"task {identifier!r} uses Kogito-reserved work-item name {work_name!r}",
                            element_id=identifier,
                        )
                    )
                try:
                    catalog.get(work_name or "")
                except KeyError:
                    findings.append(
                        _finding(
                            "unknown-capability",
                            f"task {identifier!r} has unknown drools:taskName {work_name!r}",
                            element_id=identifier,
                        )
                    )

            io_spec = task.find(_BPMN + "ioSpecification")
            data_inputs = (
                io_spec.findall(_BPMN + "dataInput") if io_spec is not None else []
            )
            taskname_inputs = [item for item in data_inputs if item.get("name") == "TaskName"]
            if len(taskname_inputs) != 1:
                findings.append(
                    _finding(
                        "task-name-input",
                        f"{task_type} {identifier!r} must have exactly one TaskName dataInput",
                        element_id=identifier,
                    )
                )
                continue

            literal, error = _taskname_literal(task, taskname_inputs[0])
            if error is not None:
                findings.append(
                    _finding(
                        "task-name-input",
                        f"{task_type} {identifier!r}: {error}",
                        element_id=identifier,
                    )
                )
                continue
            assert literal is not None
            previous = literals.get(literal)
            if previous is not None:
                findings.append(
                    _finding(
                        "task-name-unique",
                        f"{task_type} {identifier!r} shares TaskName literal with element {previous!r}",
                        element_id=identifier,
                    )
                )
            else:
                literals[literal] = identifier or "(missing id)"

            expected = (
                task_label(work_name, identifier or "")
                if work_name is not None
                else identifier
            )
            if literal != expected:
                findings.append(
                    _finding(
                        "task-name-label",
                        f"{task_type} {identifier!r} TaskName literal {literal!r} must equal {expected!r}",
                        element_id=identifier,
                    )
                )
    if process_count == 0:
        findings.append(_finding("process-required", "BPMN definitions must contain a process"))
    return findings
