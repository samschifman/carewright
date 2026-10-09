"""L5 grounding checks.

Template rule codes: T1 source citations; T2 parameter defaults and sources;
T3 structural derivation; T4 literals and expressions; T5 structural provenance
on behaviour-changing elements. Instance rule codes: I1 template fidelity; I2
structural provenance; I3 fragment integrity; I4 bindings and invariants; I5
the main boundary; I6 verbatim names and text; I8 pruning evidence; I9
composition justification and fragment scoping; I10 boundary consolidation.
I7 is represented by the suite's negative examples for these instance rules.

These checks prove traceability and structural consistency, not that a cited
passage clinically supports the behaviour. Clinical support remains a human
review task.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math
import re
from typing import Any

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.catalog import Catalog, load_catalog
from cpg_contracts.automation.instances import Evidence, ParameterBinding
from cpg_contracts.automation.expressions import (
    BinaryNode,
    DefinedNode,
    Expr,
    ExpressionError,
    IdentifierNode,
    LiteralNode,
    TypeInfo,
    UnaryNode,
    check_invariants,
    identifiers,
    literals,
    parse,
    parse_duration,
    typecheck,
)
from cpg_contracts.automation.ir import (
    BoundaryEvent,
    BusinessRuleTask,
    ConceptRef,
    EndEvent,
    ExclusiveGateway,
    FlowElement,
    IntermediateCatchEvent,
    Literal_,
    ParamRef,
    Parameter,
    Process,
    ProcessIR,
    Property,
    PropertyRef,
    PruneRecord,
    SequenceFlow,
    SourceProvenance,
    StartEvent,
    StructuralProvenance,
    Task,
    TemplateRef,
    UserTask,
    ValueRef,
)
from cpg_contracts.automation.validators.catalog_conformance import (
    UCUM_SYNTAX,
    UCUM_UNITS,
)
from cpg_contracts.automation.validators.results import Finding, Severity
from cpg_contracts.automation.validators.textmatch import contains, normalize


_TEMPLATE_STRUCTURAL_RULES = frozenset(
    {
        "start-event",
        "end-event",
        "sequence",
        "exclusive-split",
        "exclusive-join",
        "default-flow",
        "plan-bound",
    }
)
_INSTANCE_STRUCTURAL_RULES = frozenset(
    {
        "fragment-boundary",
        "fragment-merge",
        "exclusive-join",
        "start-event",
        "end-event",
        "parameter-binding",
        "plan-bound",
    }
)
_BEHAVIOUR_ELEMENTS = (
    Task,
    BusinessRuleTask,
    UserTask,
    IntermediateCatchEvent,
    BoundaryEvent,
)
_NUMERIC_TYPES = frozenset({"integer", "decimal", "quantity"})
_SIMPLE_NUMBER_WORDS = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
}
_UNIT_TEXT = {
    "mm[Hg]": ("mmhg", "mm hg", "millimeter of mercury", "millimeters of mercury"),
    "Cel": ("celsius", "degrees celsius"),
    "[degF]": ("fahrenheit", "degrees fahrenheit"),
    "kg/m2": ("kg/m2", "kg per m2", "kg/m squared"),
}


@dataclass(slots=True)
class InstanceContext:
    """Patient-plan facts kept outside the automation definition and BPMN XML."""

    bindings: list[ParameterBinding] = field(default_factory=list)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    dmn_audit_trail: list[dict[str, Any]] = field(default_factory=list)
    category_flags: dict[str, bool] = field(default_factory=dict)
    careplan_feedback: str = ""
    activities: list[dict[str, Any]] = field(default_factory=list)


def _finding(
    code: str,
    message: str,
    *,
    element_id: str | None = None,
    severity: Severity = "ERROR",
    rung: str = "L5a",
) -> Finding:
    return Finding(
        rung=rung,
        severity=severity,
        code=code,
        message=message,
        element_id=element_id,
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _process(ir: ProcessIR | Process) -> Process:
    return ir.process if isinstance(ir, ProcessIR) else ir


def _section_values(section_text: str | Mapping[str, str]) -> list[str]:
    if isinstance(section_text, str):
        return [section_text]
    return [value for value in section_text.values() if isinstance(value, str)]


def _citation_fragments(source_text: str) -> list[str]:
    return [
        fragment.strip()
        for fragment in re.split(r"(?:\.{3,}|…)", source_text)
        if fragment.strip()
    ] or [source_text]


def _source_for(element: FlowElement) -> str | None:
    provenance = element.acp.provenance
    return provenance.source_text if isinstance(provenance, SourceProvenance) else None


def _parameter_by_name(process: Process) -> dict[str, Parameter]:
    return {parameter.name: parameter for parameter in process.acp.parameters}


def _property_by_name(process: Process) -> dict[str, Property]:
    return {prop.name: prop for prop in process.properties}


def _source_has_cpg(source: Any) -> bool:
    current = source
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if _field(current, "kind") == "cpg" and _field(current, "source_text"):
            return True
        current = _field(current, "previous")
    return False


def _numeric_aliases(value: Any) -> list[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return [str(value)]
    aliases = [str(value)]
    if isinstance(value, float) and value.is_integer():
        aliases.append(str(int(value)))
    if isinstance(value, int) and value in _SIMPLE_NUMBER_WORDS:
        aliases.append(_SIMPLE_NUMBER_WORDS[value])
    return aliases


def _duration_alias_groups(value: Any) -> list[list[str]]:
    if not isinstance(value, str):
        return [[str(value)]]
    aliases = [[value]]
    try:
        duration = parse_duration(value)
    except ExpressionError:
        return aliases
    seconds = int(duration.total_seconds())
    if seconds <= 0:
        return aliases

    day_seconds = 24 * 60 * 60
    week_seconds = 7 * day_seconds
    if seconds % week_seconds == 0:
        count = seconds // week_seconds
        noun = "week" if count == 1 else "weeks"
        aliases.append([str(count), noun])
        if count in _SIMPLE_NUMBER_WORDS:
            aliases.append([_SIMPLE_NUMBER_WORDS[count], noun])
        if count == 1:
            aliases.extend([["weekly"], ["every", "week"]])
    elif seconds % day_seconds == 0:
        count = seconds // day_seconds
        noun = "day" if count == 1 else "days"
        aliases.append([str(count), noun])
        if count in _SIMPLE_NUMBER_WORDS:
            aliases.append([_SIMPLE_NUMBER_WORDS[count], noun])
        if count == 1:
            aliases.extend([["daily"], ["every", "day"]])
    elif seconds % 3600 == 0:
        count = seconds // 3600
        noun = "hour" if count == 1 else "hours"
        aliases.append([str(count), noun])
        if count in _SIMPLE_NUMBER_WORDS:
            aliases.append([_SIMPLE_NUMBER_WORDS[count], noun])
    return aliases


def _contains_any(
    text: str,
    candidates: Sequence[str | Sequence[str]],
    abbreviations: Mapping[str, str] | None,
) -> bool:
    for candidate in candidates:
        phrase = " ".join(candidate) if not isinstance(candidate, str) else candidate
        if contains(text, phrase, abbreviations=abbreviations):
            return True
    return False


def _parameter_value_grounded(
    parameter: Parameter,
    source_text: str,
    abbreviations: Mapping[str, str] | None,
) -> bool:
    value = parameter.default
    if value is None:
        return True
    if parameter.type == "duration":
        return _contains_any(
            source_text,
            _duration_alias_groups(value),
            abbreviations,
        )
    if parameter.type in _NUMERIC_TYPES:
        if not _contains_any(source_text, _numeric_aliases(value), abbreviations):
            return False
        if parameter.type == "quantity" and parameter.unit is not None:
            unit_aliases = _UNIT_TEXT.get(parameter.unit, (parameter.unit,))
            return _contains_any(source_text, unit_aliases, abbreviations)
        return True
    if parameter.type == "boolean":
        candidates = ("true", "yes") if value is True else ("false", "no")
        return _contains_any(source_text, candidates, abbreviations)
    return contains(source_text, str(value), abbreviations=abbreviations)


def _within_constraints(parameter: Parameter, value: Any) -> bool:
    constraints = parameter.constraints
    if constraints is None:
        return True
    if constraints.min is not None or constraints.max is not None:
        try:
            measured = (
                parse_duration(value).total_seconds()
                if parameter.type == "duration" and isinstance(value, str)
                else float(value)
            )
        except (TypeError, ValueError, ExpressionError):
            return False
        if constraints.min is not None and measured < constraints.min:
            return False
        if constraints.max is not None and measured > constraints.max:
            return False
    if constraints.enum is not None and value not in constraints.enum:
        return False
    if constraints.pattern is not None and not re.fullmatch(constraints.pattern, str(value)):
        return False
    return True


def _expression_env(process: Process) -> dict[str, TypeInfo]:
    env = {
        prop.name: TypeInfo(type=prop.type, unit=prop.unit)
        for prop in process.properties
    }
    env.update(
        {
            parameter.name: TypeInfo(type=parameter.type, unit=parameter.unit)
            for parameter in process.acp.parameters
        }
    )
    return env


def _derived_names(process: Process) -> set[str]:
    derived = {
        parameter.name
        for parameter in process.acp.parameters
        if _field(parameter.source, "kind") in {"cpg", "reviewer"}
    }
    for element in process.flowElements:
        if isinstance(element, Task | BusinessRuleTask | UserTask) and isinstance(
            element.acp.provenance, SourceProvenance
        ):
            derived.update(element.outputs.values())
            if isinstance(element, Task) and element.acp.counter:
                derived.add(element.acp.counter)
    return derived


def _condition_source_derived(
    flow: SequenceFlow,
    env: dict[str, TypeInfo],
    derived_names: set[str],
) -> bool:
    if flow.conditionExpression is None or not isinstance(
        flow.acp.provenance, SourceProvenance
    ):
        return False
    try:
        used = identifiers(parse(flow.conditionExpression))
    except ExpressionError:
        return False
    return bool(used) and used <= derived_names and used <= set(env)


def _check_template_parameters(process: Process) -> list[Finding]:
    findings: list[Finding] = []
    for parameter in process.acp.parameters:
        source = parameter.source
        if parameter.default is not None and source is None:
            findings.append(
                _finding("T2", f"parameter {parameter.name!r} has a default without a source")
            )
        if parameter.default is not None and _field(source, "kind") == "authored" and parameter.type != "string":
            findings.append(
                _finding(
                    "T2",
                    f"authored default on {parameter.name!r} is allowed only for string parameters",
                )
            )
        if _field(source, "kind") == "policy" and not parameter.reserved:
            findings.append(
                _finding(
                    "T2",
                    f"policy source on {parameter.name!r} requires a reserved parameter",
                )
            )
        if _field(source, "kind") == "reviewer" and not _source_has_cpg(
            _field(source, "previous")
        ):
            findings.append(
                _finding(
                    "T2",
                    f"reviewer default on {parameter.name!r} must retain its CPG citation",
                )
            )
        if parameter.default is not None and not _within_constraints(
            parameter, parameter.default
        ):
            findings.append(
                _finding(
                    "T2",
                    f"default for parameter {parameter.name!r} is outside its constraints",
                )
            )
    return findings


def _check_template_structure(
    process: Process,
    catalog: Catalog,
) -> list[Finding]:
    findings: list[Finding] = []
    elements = {element.id: element for element in process.flowElements}
    properties = _property_by_name(process)
    parameters = _parameter_by_name(process)
    env = _expression_env(process)
    derived = _derived_names(process)

    for element in process.flowElements:
        provenance = element.acp.provenance
        requires_structural = isinstance(
            element, (StartEvent, EndEvent, ExclusiveGateway)
        )
        requires_source = isinstance(element, _BEHAVIOUR_ELEMENTS) or (
            isinstance(element, SequenceFlow) and element.conditionExpression is not None
        )
        if requires_structural and not isinstance(provenance, StructuralProvenance):
            findings.append(
                _finding(
                    "T3",
                    f"structural element {element.id!r} requires structural provenance",
                    element_id=element.id,
                )
            )
        if requires_source and not isinstance(provenance, SourceProvenance) and not (
            isinstance(element, BoundaryEvent)
            and isinstance(provenance, StructuralProvenance)
            and provenance.derivation_rule == "plan-bound"
        ):
            findings.append(
                _finding(
                    "T1",
                    f"behaviour-changing element {element.id!r} requires source provenance",
                    element_id=element.id,
                )
            )
        if isinstance(provenance, StructuralProvenance):
            if provenance.derivation_rule not in _TEMPLATE_STRUCTURAL_RULES:
                findings.append(
                    _finding(
                        "T3",
                        f"template element {element.id!r} uses unsupported structural rule {provenance.derivation_rule!r}",
                        element_id=element.id,
                    )
                )
            rule_types = {
                "start-event": (StartEvent,),
                "end-event": (EndEvent,),
                "sequence": (SequenceFlow,),
                "exclusive-split": (ExclusiveGateway,),
                "exclusive-join": (ExclusiveGateway,),
                "default-flow": (SequenceFlow,),
                "plan-bound": (BoundaryEvent,),
            }
            allowed_types = rule_types.get(provenance.derivation_rule)
            if allowed_types is not None and not isinstance(element, allowed_types):
                findings.append(
                    _finding(
                        "T3",
                        f"structural rule {provenance.derivation_rule!r} does not apply to {element.type}",
                        element_id=element.id,
                    )
                )
            if (
                provenance.derivation_rule == "exclusive-split"
                and isinstance(element, ExclusiveGateway)
                and element.gatewayDirection != "Diverging"
            ) or (
                provenance.derivation_rule == "exclusive-join"
                and isinstance(element, ExclusiveGateway)
                and element.gatewayDirection != "Converging"
            ):
                findings.append(
                    _finding(
                        "T3",
                        f"structural rule {provenance.derivation_rule!r} does not match gateway direction",
                        element_id=element.id,
                    )
                )
            if not provenance.supports or any(
                support not in elements for support in provenance.supports
            ):
                findings.append(
                    _finding(
                        "T3",
                        f"structural element {element.id!r} must cite resolvable supports",
                        element_id=element.id,
                    )
                )
        if isinstance(element, BoundaryEvent) and isinstance(
            provenance, StructuralProvenance
        ):
            timer = element.timerEventDefinition.timeDuration
            if (
                provenance.derivation_rule != "plan-bound"
                or element.attachedToRef != "main"
                or not (
                    isinstance(timer, ParamRef) and timer.param == "max_duration"
                )
            ):
                findings.append(
                    _finding(
                        "T3",
                        f"structural boundary {element.id!r} must be plan-bound to max_duration",
                        element_id=element.id,
                    )
                )

        if isinstance(element, ExclusiveGateway) and isinstance(
            provenance, StructuralProvenance
        ) and element.gatewayDirection == "Diverging":
            supported_flows = [
                elements[support]
                for support in provenance.supports
                if support in elements and isinstance(elements[support], SequenceFlow)
            ]
            if not any(
                flow.sourceRef == element.id
                and _condition_source_derived(flow, env, derived)
                for flow in supported_flows
            ):
                findings.append(
                    _finding(
                        "T3",
                        f"diverging gateway {element.id!r} needs a supported source-derived conditional flow",
                        element_id=element.id,
                    )
                )

        if isinstance(element, Task):
            try:
                capability = catalog.get(element.taskName)
            except KeyError:
                capability = None
            if capability is not None:
                input_types = {item.name: item for item in capability.inputs}
                quantity_outputs = {
                    output.name
                    for output in capability.outputs
                    if output.type == "quantity"
                }
                for input_name, value in element.inputs.items():
                    capability_input = input_types.get(input_name)
                    if capability_input is None:
                        continue
                    if capability_input.type == "code" and not isinstance(value, ConceptRef):
                        findings.append(
                            _finding(
                                "T4",
                                f"code input {input_name!r} on {element.id!r} must use a concept reference",
                                element_id=element.id,
                            )
                        )
                    if isinstance(value, ConceptRef) and capability_input.type != "code":
                        findings.append(
                            _finding(
                                "T4",
                                f"concept reference on {element.id!r}.{input_name} is not a code input",
                                element_id=element.id,
                            )
                        )
                    if capability_input.type == "enum" and isinstance(value, Literal_):
                        if value.type != "enum" or (
                            capability_input.values is not None
                            and value.literal not in capability_input.values
                        ):
                            findings.append(
                                _finding(
                                    "T4",
                                    f"enum literal on {element.id!r}.{input_name} is not in the catalog values",
                                    element_id=element.id,
                                )
                            )
                    if capability_input.type == "unit":
                        if not (
                            isinstance(value, Literal_)
                            and value.type == "unit"
                            and isinstance(value.literal, str)
                        ):
                            findings.append(
                                _finding(
                                    "T4",
                                    f"unit input {element.id!r}.{input_name} must be a unit literal",
                                    element_id=element.id,
                                )
                            )
                            continue
                        unit = value.literal
                        if not UCUM_SYNTAX.fullmatch(unit):
                            findings.append(
                                _finding(
                                    "T4",
                                    f"unit {unit!r} on {element.id!r} has invalid UCUM syntax",
                                    element_id=element.id,
                                )
                            )
                        elif unit not in UCUM_UNITS:
                            findings.append(
                                _finding(
                                    "T4",
                                    f"unit {unit!r} on {element.id!r} is outside the curated code cache",
                                    element_id=element.id,
                                    severity="WARNING",
                                )
                            )
                        for output_name in quantity_outputs.intersection(element.outputs):
                            target = properties.get(element.outputs[output_name])
                            if target is None or target.unit != unit:
                                findings.append(
                                    _finding(
                                        "T4",
                                        f"unit {unit!r} on {element.id!r} does not match its quantity output property",
                                        element_id=element.id,
                                    )
                                )

        if isinstance(element, (IntermediateCatchEvent, BoundaryEvent)):
            timer = element.timerEventDefinition.timeDuration or element.timerEventDefinition.timeCycle
            if isinstance(timer, ParamRef) and timer.param not in parameters:
                findings.append(
                    _finding(
                        "T4",
                        f"timer {element.id!r} references unknown parameter {timer.param!r}",
                        element_id=element.id,
                    )
                )

        if isinstance(element, SequenceFlow) and element.conditionExpression:
            try:
                expression = parse(element.conditionExpression)
                if typecheck(expression, env) != "boolean":
                    raise ExpressionError("condition must have boolean type")
                expression_literals = literals(expression)
            except ExpressionError as exc:
                findings.append(
                    _finding(
                        "T4",
                        f"condition on {element.id!r} is not well-typed: {exc}",
                        element_id=element.id,
                    )
                )
                expression_literals = []
            if element.acp.structural_constant and any(
                literal_type in _NUMERIC_TYPES and value not in (0, 1)
                for value, literal_type in expression_literals
            ):
                findings.append(
                    _finding(
                        "T4",
                        f"structural constants on {element.id!r} may only mark 0 or 1",
                        element_id=element.id,
                    )
                )

    for element in process.flowElements:
        provenance = element.acp.provenance
        is_behaviour_flow = isinstance(element, SequenceFlow) and element.conditionExpression is not None
        if (isinstance(element, _BEHAVIOUR_ELEMENTS) or is_behaviour_flow) and isinstance(provenance, StructuralProvenance):
            if not (
                isinstance(element, BoundaryEvent)
                and provenance.derivation_rule == "plan-bound"
            ):
                findings.append(
                    _finding(
                        "T5",
                        f"behaviour-changing element {element.id!r} cannot have structural provenance",
                        element_id=element.id,
                    )
                )

    return findings


def _text_literal_checks(
    process: Process,
    sections: Sequence[str],
    abbreviations: Mapping[str, str] | None,
) -> list[Finding]:
    findings: list[Finding] = []

    def in_sections(candidate: str) -> bool:
        return any(
            contains(section, candidate, abbreviations=abbreviations)
            for section in sections
        )

    def concept_in_text(concept: str, source_text: str) -> bool:
        candidates = [source_text, *sections]
        normalized_concept = normalize(concept, abbreviations).split()
        return any(
            contains(candidate, concept, abbreviations=abbreviations)
            or set(normalized_concept) <= set(normalize(candidate, abbreviations).split())
            for candidate in candidates
        )

    for element in process.flowElements:
        source_text = _source_for(element)
        if source_text is None:
            continue
        if isinstance(element, Task):
            for input_name, value in element.inputs.items():
                if isinstance(value, ConceptRef):
                    if not concept_in_text(value.concept, source_text):
                        findings.append(
                            _finding(
                                "T4",
                                f"concept {value.concept!r} on {element.id!r} is not cited by its element or section",
                                element_id=element.id,
                                rung="L5b",
                            )
                        )
                elif isinstance(value, Literal_):
                    if value.type == "unit":
                        continue
                    if value.type == "enum":
                        cited = contains(
                            source_text, str(value.literal), abbreviations=abbreviations
                        ) or in_sections(str(value.literal))
                    elif value.type in _NUMERIC_TYPES:
                        cited = _contains_any(
                            source_text,
                            _numeric_aliases(value.literal),
                            abbreviations,
                        )
                    elif value.type == "duration":
                        cited = _contains_any(
                            source_text,
                            _duration_alias_groups(value.literal),
                            abbreviations,
                        )
                    else:
                        cited = True
                    if not cited:
                        findings.append(
                            _finding(
                                "T4",
                                f"literal {value.literal!r} on {element.id!r}.{input_name} is absent from its source text",
                                element_id=element.id,
                                rung="L5b",
                            )
                        )
        if isinstance(element, (IntermediateCatchEvent, BoundaryEvent)):
            timer = element.timerEventDefinition.timeDuration or element.timerEventDefinition.timeCycle
            if isinstance(timer, Literal_) and timer.type in {"duration", "quantity", "integer", "decimal"}:
                if not _contains_any(
                    source_text,
                    _duration_alias_groups(timer.literal)
                    if timer.type == "duration"
                    else _numeric_aliases(timer.literal),
                    abbreviations,
                ):
                    findings.append(
                        _finding(
                            "T4",
                            f"timer literal {timer.literal!r} on {element.id!r} is absent from its source text",
                            element_id=element.id,
                            rung="L5b",
                        )
                    )
        if isinstance(element, SequenceFlow) and element.conditionExpression:
            try:
                expression_literals = literals(parse(element.conditionExpression))
            except ExpressionError:
                expression_literals = []
            for value, literal_type in expression_literals:
                if literal_type not in _NUMERIC_TYPES | {"duration"}:
                    continue
                if (
                    element.acp.structural_constant
                    and literal_type in _NUMERIC_TYPES
                    and value in (0, 1)
                ):
                    continue
                candidates = (
                    _duration_alias_groups(value)
                    if literal_type == "duration"
                    else _numeric_aliases(value)
                )
                if not _contains_any(source_text, candidates, abbreviations):
                    findings.append(
                        _finding(
                            "T4",
                            f"condition literal {value!r} on {element.id!r} is absent from its source text",
                            element_id=element.id,
                            rung="L5b",
                        )
                    )
    return findings


@trace(name="bpmn.l5.template_structure")
def validate_template_structure(ir: ProcessIR) -> list[Finding]:
    """Check template structural derivation, non-text literals, and provenance."""
    process = _process(ir)
    findings = _check_template_parameters(process)
    findings.extend(_check_template_structure(process, load_catalog()))
    return findings


@trace(name="bpmn.l5.template_text")
def validate_template_text(
    ir: ProcessIR,
    section_text: str | Mapping[str, str],
    abbreviations: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Check provenance citations, parameter values, and text-dependent literals."""
    process = _process(ir)
    sections = _section_values(section_text)
    findings: list[Finding] = []
    for element in process.flowElements:
        provenance = element.acp.provenance
        if not isinstance(provenance, SourceProvenance):
            continue
        fragments = _citation_fragments(provenance.source_text)
        if not all(
            any(contains(section, fragment, abbreviations=abbreviations) for section in sections)
            for fragment in fragments
        ):
            findings.append(
                _finding(
                    "T1",
                    f"source text for {element.id!r} is not contained in the supplied section text",
                    element_id=element.id,
                    rung="L5b",
                )
            )

    for parameter in process.acp.parameters:
        source = parameter.source
        if parameter.default is None or _field(source, "kind") != "cpg":
            continue
        source_text = _field(source, "source_text", "")
        if not _parameter_value_grounded(parameter, source_text, abbreviations):
            findings.append(
                _finding(
                    "T2",
                    f"default value for parameter {parameter.name!r} is absent from its CPG citation",
                    element_id=parameter.name,
                    rung="L5b",
                )
            )

    findings.extend(_text_literal_checks(process, sections, abbreviations))
    return findings


def _source_name_matches(instance_name: str, template_name: str) -> bool:
    return instance_name == template_name or instance_name.endswith(f"__{template_name}")


def _fragment_prefix(process: Process, template_id: str) -> str | None:
    for element in process.flowElements:
        ref = element.acp.template_ref
        if ref is None or ref.template_id != template_id or ref.element_id is None:
            continue
        suffix = f"__{ref.element_id}"
        if element.id.endswith(suffix):
            return element.id[: -len(suffix)]
    return None


def _mapped_item_name(
    process: Process,
    template_id: str,
    source_name: str,
    *,
    parameter: bool = False,
) -> str | None:
    names = (
        {item.name for item in process.acp.parameters}
        if parameter
        else {item.name for item in process.properties}
    )
    prefix = _fragment_prefix(process, template_id)
    if prefix is not None:
        prefixed = f"{prefix}__{source_name}"
        if prefixed in names:
            return prefixed
    if source_name in names:
        return source_name
    matches = [name for name in names if name.endswith(f"__{source_name}")]
    return matches[0] if len(matches) == 1 else None


def _template_element_map(process: Process) -> dict[str, FlowElement]:
    return {element.id: element for element in process.flowElements}


def _source_element_for(
    instance_element: FlowElement,
    templates: Mapping[str, Process],
) -> tuple[Process | None, FlowElement | None]:
    ref = instance_element.acp.template_ref
    if ref is None:
        return None, None
    template = templates.get(ref.template_id)
    if template is None or ref.element_id is None:
        return template, None
    return template, _template_element_map(template).get(ref.element_id)


def _mapped_expression_signature(
    source: str | None,
    process: Process,
    template_id: str | None = None,
) -> Any:
    if source is None:
        return None
    names = {prop.name for prop in process.properties}
    names.update(parameter.name for parameter in process.acp.parameters)

    def canonical_identifier(identifier: str) -> str:
        if template_id is not None:
            for name in names:
                is_parameter = name in _parameter_by_name(process)
                if _mapped_item_name(
                    process, template_id, name, parameter=is_parameter
                ) == identifier:
                    return name
        matches = [name for name in names if _source_name_matches(identifier, name)]
        return max(matches, key=len) if matches else identifier

    def shape(expression: Expr) -> Any:
        if isinstance(expression, IdentifierNode):
            return ("identifier", canonical_identifier(expression.name))
        if isinstance(expression, LiteralNode):
            return ("literal", expression.literal_type, expression.value)
        if isinstance(expression, DefinedNode):
            return ("defined", canonical_identifier(expression.identifier.name))
        if isinstance(expression, UnaryNode):
            return ("unary", expression.operator, shape(expression.operand))
        if isinstance(expression, BinaryNode):
            return ("binary", expression.operator, shape(expression.left), shape(expression.right))
        return (type(expression).__name__,)

    return shape(parse(source))


def _value_ref_matches(
    source: ValueRef,
    instance: ValueRef,
    template: Process,
    instance_process: Process,
    instance_element: FlowElement,
    context: InstanceContext,
    input_name: str,
) -> bool:
    if isinstance(source, ParamRef):
        ref = instance_element.acp.template_ref
        expected_name = (
            _mapped_item_name(instance_process, ref.template_id, source.param, parameter=True)
            if ref is not None
            else None
        )
        if not isinstance(instance, ParamRef) or instance.param != expected_name:
            return False
        source_parameter = _parameter_by_name(template).get(source.param)
        actual_parameter = _parameter_by_name(instance_process).get(expected_name or "")
        return (
            source_parameter is not None
            and actual_parameter is not None
            and (source_parameter.type, source_parameter.unit)
            == (actual_parameter.type, actual_parameter.unit)
        )
    if isinstance(source, PropertyRef):
        ref = instance_element.acp.template_ref
        expected_name = (
            _mapped_item_name(instance_process, ref.template_id, source.property)
            if ref is not None
            else None
        )
        if not isinstance(instance, PropertyRef) or instance.property != expected_name:
            return False
        source_property = _property_by_name(template).get(source.property)
        actual_property = _property_by_name(instance_process).get(expected_name or "")
        return (
            source_property is not None
            and actual_property is not None
            and (source_property.type, source_property.unit)
            == (actual_property.type, actual_property.unit)
        )
    if isinstance(source, ConceptRef):
        if isinstance(instance, ConceptRef):
            return source.concept == instance.concept
        if isinstance(instance, Literal_) and instance.type == "code":
            accepted_names = {
                f"{instance_element.id}.{input_name}",
                f"{instance_element.id}__{input_name}",
            }
            return any(
                binding.name in accepted_names
                and binding.source == "plan-derived"
                and binding.value == instance.literal
                for binding in context.bindings
            )
        return False
    if isinstance(source, Literal_):
        return (
            isinstance(instance, Literal_)
            and source.type == instance.type
            and source.literal == instance.literal
        )
    return False


def _element_fields_match(
    instance_element: FlowElement,
    source_element: FlowElement,
    template: Process,
    instance_process: Process,
    context: InstanceContext,
) -> bool:
    if type(instance_element) is not type(source_element):
        return False
    if isinstance(instance_element, Task) and isinstance(source_element, Task):
        if instance_element.taskName != source_element.taskName:
            return False
        if instance_element.inputs.keys() != source_element.inputs.keys():
            return False
        for name, source_value in source_element.inputs.items():
            if not _value_ref_matches(
                source_value,
                instance_element.inputs[name],
                template,
                instance_process,
                instance_element,
                context,
                name,
            ):
                return False
        if instance_element.outputs.keys() != source_element.outputs.keys():
            return False
        if any(
            instance_element.outputs[name]
            != _mapped_item_name(instance_process, template.id, value)
            for name, value in source_element.outputs.items()
        ):
            return False
    elif isinstance(instance_element, BusinessRuleTask) and isinstance(
        source_element, BusinessRuleTask
    ):
        if instance_element.dmnModel != source_element.dmnModel:
            return False
        if instance_element.inputs.keys() != source_element.inputs.keys():
            return False
        if instance_element.outputs.keys() != source_element.outputs.keys():
            return False
        for name, source_value in source_element.inputs.items():
            if not _value_ref_matches(
                source_value,
                instance_element.inputs[name],
                template,
                instance_process,
                instance_element,
                context,
                name,
            ):
                return False
        if any(
            instance_element.outputs[name]
            != _mapped_item_name(instance_process, template.id, value)
            for name, value in source_element.outputs.items()
        ):
            return False
    elif isinstance(instance_element, UserTask) and isinstance(source_element, UserTask):
        if instance_element.groupId != source_element.groupId:
            return False
        if instance_element.outputs.keys() != source_element.outputs.keys():
            return False
        if any(
            instance_element.outputs[name]
            != _mapped_item_name(instance_process, template.id, value)
            for name, value in source_element.outputs.items()
        ):
            return False
    elif isinstance(instance_element, SequenceFlow) and isinstance(source_element, SequenceFlow):
        if (instance_element.conditionExpression is None) != (
            source_element.conditionExpression is None
        ):
            return False
        if _mapped_expression_signature(
            instance_element.conditionExpression, instance_process, template.id
        ) != _mapped_expression_signature(
            source_element.conditionExpression, template, template.id
        ):
            return False
        if instance_element.acp.resets is None:
            if source_element.acp.resets is not None:
                return False
        elif source_element.acp.resets is None or {
            _mapped_item_name(instance_process, template.id, name)
            for name in source_element.acp.resets
        } != set(instance_element.acp.resets):
            return False
        for instance_ref, source_ref in (
            (instance_element.sourceRef, source_element.sourceRef),
            (instance_element.targetRef, source_element.targetRef),
        ):
            endpoint = next(
                (item for item in instance_process.flowElements if item.id == instance_ref),
                None,
            )
            if endpoint is None:
                return False
            endpoint_template_ref = endpoint.acp.template_ref
            if (
                endpoint_template_ref is not None
                and endpoint_template_ref.template_id == template.id
                and endpoint_template_ref.element_id != source_ref
            ):
                return False
    elif isinstance(instance_element, ExclusiveGateway) and isinstance(
        source_element, ExclusiveGateway
    ):
        if instance_element.gatewayDirection != source_element.gatewayDirection:
            return False
        if source_element.default is None:
            if instance_element.default is not None:
                return False
        else:
            default_flow = next(
                (
                    item
                    for item in instance_process.flowElements
                    if item.id == instance_element.default
                ),
                None,
            )
            if (
                default_flow is None
                or default_flow.acp.template_ref is None
                or default_flow.acp.template_ref.template_id != template.id
                or default_flow.acp.template_ref.element_id != source_element.default
            ):
                return False
    elif isinstance(instance_element, (IntermediateCatchEvent, BoundaryEvent)):
        if (
            (instance_element.timerEventDefinition.timeDuration is None)
            != (source_element.timerEventDefinition.timeDuration is None)
            or (instance_element.timerEventDefinition.timeCycle is None)
            != (source_element.timerEventDefinition.timeCycle is None)
        ):
            return False
        instance_timer = (
            instance_element.timerEventDefinition.timeDuration
            or instance_element.timerEventDefinition.timeCycle
        )
        source_timer = (
            source_element.timerEventDefinition.timeDuration
            or source_element.timerEventDefinition.timeCycle
        )
        if not _value_ref_matches(
            source_timer,
            instance_timer,
            template,
            instance_process,
            instance_element,
            context,
            "timer",
        ):
            return False
        if isinstance(instance_element, BoundaryEvent) and isinstance(
            source_element, BoundaryEvent
        ):
            if instance_element.cancelActivity != source_element.cancelActivity:
                return False
            if source_element.attachedToRef == "main":
                if instance_element.attachedToRef != "main":
                    return False
            else:
                host = next(
                    (
                        item
                        for item in instance_process.flowElements
                        if item.id == instance_element.attachedToRef
                    ),
                    None,
                )
                if (
                    host is None
                    or host.acp.template_ref is None
                    or host.acp.template_ref.template_id != template.id
                    or host.acp.template_ref.element_id != source_element.attachedToRef
                ):
                    return False
    if isinstance(instance_element, EndEvent) and isinstance(source_element, EndEvent):
        if instance_element.acp.outcome != source_element.acp.outcome:
            return False
    if isinstance(instance_element, Task) and isinstance(source_element, Task):
        if instance_element.acp.counter is not None and source_element.acp.counter is not None:
            expected_counter = _mapped_item_name(
                instance_process, template.id, source_element.acp.counter
            )
            if instance_element.acp.counter != expected_counter:
                return False
        elif instance_element.acp.counter != source_element.acp.counter:
            return False
    if instance_element.acp.structural_constant != source_element.acp.structural_constant:
        return False
    return True


def _template_reference_findings(
    process: Process,
    templates: Mapping[str, Process],
    context: InstanceContext,
) -> tuple[list[Finding], dict[str, dict[str, FlowElement]], dict[str, dict[str, FlowElement]]]:
    findings: list[Finding] = []
    retained: dict[str, dict[str, FlowElement]] = defaultdict(dict)
    source_by_instance: dict[str, dict[str, FlowElement]] = defaultdict(dict)
    template_version_map = {
        reference.template_id: reference.version
        for reference in process.acp.template_refs
    }

    for element in process.flowElements:
        ref = element.acp.template_ref
        if ref is None:
            provenance = element.acp.provenance
            unreferenced_rule_types = {
                "fragment-boundary": (SequenceFlow,),
                "fragment-merge": (SequenceFlow,),
                "exclusive-join": (ExclusiveGateway,),
                "start-event": (StartEvent,),
                "end-event": (EndEvent,),
                "parameter-binding": (SequenceFlow,),
            }
            allowed_types = (
                unreferenced_rule_types.get(provenance.derivation_rule)
                if isinstance(provenance, StructuralProvenance)
                else None
            )
            if allowed_types is not None and isinstance(element, allowed_types):
                continue
            findings.append(
                _finding(
                    "I1",
                    f"retained element {element.id!r} has no template reference",
                    element_id=element.id,
                )
            )
            continue
        template = templates.get(ref.template_id)
        source = _template_element_map(template).get(ref.element_id or "") if template else None
        if template is None or source is None:
            findings.append(
                _finding(
                    "I1",
                    f"element {element.id!r} references an unknown template element",
                    element_id=element.id,
                )
            )
            continue
        if ref.version != template_version_map.get(ref.template_id):
            findings.append(
                _finding(
                    "I1",
                    f"element {element.id!r} uses a template version not selected by the instance",
                    element_id=element.id,
                )
            )
        if type(element) is not type(source):
            findings.append(
                _finding(
                    "I1",
                    f"element {element.id!r} does not preserve template element type",
                    element_id=element.id,
                )
            )
        is_main_boundary = isinstance(element, BoundaryEvent) and element.attachedToRef == "main"
        if not is_main_boundary and not _source_name_matches(element.id, source.id):
            findings.append(
                _finding(
                    "I1",
                    f"element id {element.id!r} is not the template id or a fragment-prefixed id",
                    element_id=element.id,
                )
            )
        if not _element_fields_match(element, source, template, process, context):
            findings.append(
                _finding(
                    "I1",
                    f"element {element.id!r} changes its template type, task binding, I/O, timer, or expression shape",
                    element_id=element.id,
                )
            )
        retained[ref.template_id][source.id] = element
        source_by_instance[ref.template_id][element.id] = source

    return findings, retained, source_by_instance


def _check_instance_data_shapes(
    process: Process,
    templates: Mapping[str, Process],
) -> list[Finding]:
    findings: list[Finding] = []
    selected = {reference.template_id for reference in process.acp.template_refs}
    mapped_properties: set[str] = set()
    mapped_parameters: set[str] = set()
    for template_id in selected:
        template = templates.get(template_id)
        if template is None:
            findings.append(_finding("I1", f"selected template {template_id!r} is unavailable"))
            continue
        for source_property in template.properties:
            name = _mapped_item_name(process, template_id, source_property.name)
            actual = _property_by_name(process).get(name or "")
            if actual is None or (actual.type, actual.unit) != (
                source_property.type,
                source_property.unit,
            ):
                findings.append(
                    _finding(
                        "I1",
                        f"property {source_property.name!r} is missing or changes type/unit in the instance",
                        element_id=name or source_property.name,
                    )
                )
            elif name is not None:
                mapped_properties.add(name)
        for source_parameter in template.acp.parameters:
            name = (
                "max_duration"
                if source_parameter.name == "max_duration"
                else _mapped_item_name(
                    process,
                    template_id,
                    source_parameter.name,
                    parameter=True,
                )
            )
            actual = _parameter_by_name(process).get(name or "")
            if actual is None or (actual.type, actual.unit) != (
                source_parameter.type,
                source_parameter.unit,
            ):
                findings.append(
                    _finding(
                        "I1",
                        f"parameter {source_parameter.name!r} is missing or changes type/unit in the instance",
                        element_id=name or source_parameter.name,
                    )
                )
            elif name is not None:
                mapped_parameters.add(name)

    for prop in process.properties:
        if prop.name not in mapped_properties:
            findings.append(
                _finding("I1", f"instance adds an unmapped property {prop.name!r}", element_id=prop.name)
            )
    for parameter in process.acp.parameters:
        if parameter.name not in mapped_parameters and parameter.name != "max_duration":
            findings.append(
                _finding(
                    "I1",
                    f"instance adds an unmapped parameter {parameter.name!r}",
                    element_id=parameter.name,
                )
            )
    return findings


def _check_instance_provenance(process: Process) -> list[Finding]:
    findings: list[Finding] = []
    for element in process.flowElements:
        provenance = element.acp.provenance
        if isinstance(provenance, StructuralProvenance) and provenance.derivation_rule not in _INSTANCE_STRUCTURAL_RULES:
            findings.append(
                _finding(
                    "I2",
                    f"instance element {element.id!r} uses unsupported structural rule {provenance.derivation_rule!r}",
                    element_id=element.id,
                )
            )
        rule_types = {
            "fragment-boundary": (SequenceFlow,),
            "fragment-merge": (SequenceFlow,),
            "exclusive-join": (ExclusiveGateway,),
            "start-event": (StartEvent,),
            "end-event": (EndEvent,),
            "parameter-binding": (SequenceFlow,),
            "plan-bound": (BoundaryEvent,),
        }
        if isinstance(provenance, StructuralProvenance):
            allowed_types = rule_types.get(provenance.derivation_rule)
            if allowed_types is not None and not isinstance(element, allowed_types):
                findings.append(
                    _finding(
                        "I2",
                        f"structural rule {provenance.derivation_rule!r} does not apply to {element.type}",
                        element_id=element.id,
                    )
                )
            if (
                provenance.derivation_rule == "exclusive-join"
                and isinstance(element, ExclusiveGateway)
                and element.gatewayDirection != "Converging"
            ):
                findings.append(
                    _finding(
                        "I2",
                        f"exclusive-join provenance on {element.id!r} requires a converging gateway",
                        element_id=element.id,
                    )
                )
        if isinstance(element, BoundaryEvent) and isinstance(provenance, StructuralProvenance):
            if provenance.derivation_rule != "plan-bound":
                findings.append(
                    _finding(
                        "I2",
                        f"instance boundary {element.id!r} must retain plan-bound provenance",
                        element_id=element.id,
                    )
                )
    return findings


def _check_fragment_integrity(
    process: Process,
    templates: Mapping[str, Process],
    retained: Mapping[str, Mapping[str, FlowElement]],
) -> list[Finding]:
    findings: list[Finding] = []
    for template_id, retained_map in retained.items():
        template = templates.get(template_id)
        if template is None:
            continue
        template_nodes = {
            element.id
            for element in template.flowElements
            if not isinstance(element, SequenceFlow)
        }
        retained_node_ids = {
            source_id
            for source_id in retained_map
            if source_id in template_nodes
        }
        expected_flows = {
            flow.id: flow
            for flow in template.flowElements
            if isinstance(flow, SequenceFlow)
            and flow.sourceRef in retained_node_ids
            and flow.targetRef in retained_node_ids
        }
        actual_flows = {
            element.acp.template_ref.element_id: element
            for element in process.flowElements
            if isinstance(element, SequenceFlow)
            and element.acp.template_ref is not None
            and element.acp.template_ref.template_id == template_id
            and element.acp.template_ref.element_id is not None
        }
        for source_flow_id, source_flow in expected_flows.items():
            actual = actual_flows.get(source_flow_id)
            if actual is None:
                findings.append(
                    _finding(
                        "I3",
                        f"template edge {source_flow_id!r} between retained elements is missing",
                        element_id=source_flow_id,
                    )
                )
                continue
            source_instance = retained_map.get(source_flow.sourceRef)
            target_instance = retained_map.get(source_flow.targetRef)
            if (
                source_instance is None
                or target_instance is None
                or actual.sourceRef != source_instance.id
                or actual.targetRef != target_instance.id
                or _mapped_expression_signature(
                    actual.conditionExpression, process, template_id
                )
                != _mapped_expression_signature(
                    source_flow.conditionExpression, template, template_id
                )
            ):
                findings.append(
                    _finding(
                        "I3",
                        f"template edge {source_flow_id!r} changed inside its fragment",
                        element_id=actual.id,
                    )
                )
        for source_flow_id, actual in actual_flows.items():
            if source_flow_id not in expected_flows:
                findings.append(
                    _finding(
                        "I3",
                        f"instance adds an internal edge {source_flow_id!r} to a fragment",
                        element_id=actual.id,
                    )
                )
        for element in process.flowElements:
            if not isinstance(element, SequenceFlow):
                continue
            if isinstance(element.acp.provenance, StructuralProvenance) and element.acp.provenance.derivation_rule == "fragment-boundary":
                continue
            source = next(
                (item for item in process.flowElements if item.id == element.sourceRef),
                None,
            )
            target = next(
                (item for item in process.flowElements if item.id == element.targetRef),
                None,
            )
            source_ref = source.acp.template_ref if source else None
            target_ref = target.acp.template_ref if target else None
            flow_ref = element.acp.template_ref
            if (
                source_ref is not None
                and target_ref is not None
                and source_ref.template_id == template_id
                and target_ref.template_id == template_id
                and (
                    flow_ref is None
                    or flow_ref.template_id != template_id
                    or flow_ref.element_id not in expected_flows
                )
            ):
                findings.append(
                    _finding(
                        "I3",
                        f"instance adds an ungrounded internal edge {element.id!r} to a fragment",
                        element_id=element.id,
                    )
                )
        for source_id, source_element in _template_element_map(template).items():
            actual = retained_map.get(source_id)
            if actual is None or not isinstance(source_element, BoundaryEvent):
                continue
            if source_element.attachedToRef == "main":
                expected_host = "main"
            else:
                mapped_host = retained_map.get(source_element.attachedToRef)
                expected_host = mapped_host.id if mapped_host else None
            if actual.attachedToRef != expected_host:
                findings.append(
                    _finding(
                        "I3",
                        f"boundary event {actual.id!r} changed its template attachment",
                        element_id=actual.id,
                    )
                )
    return findings


def _binding_index(context: InstanceContext) -> tuple[dict[str, list[Any]], list[Finding]]:
    indexed: dict[str, list[Any]] = defaultdict(list)
    for binding in context.bindings:
        indexed[str(_field(binding, "name", ""))].append(binding)
    return indexed, []


def _binding_runtime_value(parameter: Parameter, binding: Any) -> Any:
    value = _field(binding, "value")
    if parameter.type == "duration" and isinstance(value, str):
        try:
            return parse_duration(value)
        except ExpressionError:
            return value
    return value


def _binding_type_matches(parameter: Parameter, value: Any) -> bool:
    if parameter.type == "duration":
        if not isinstance(value, str):
            return False
        try:
            parse_duration(value)
        except ExpressionError:
            return False
        return True
    if parameter.type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if parameter.type in {"decimal", "quantity"}:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if parameter.type == "boolean":
        return isinstance(value, bool)
    if parameter.type == "string":
        return isinstance(value, str)
    return False


def _check_bindings_and_invariants(
    process: Process,
    context: InstanceContext,
) -> list[Finding]:
    findings: list[Finding] = []
    indexed, _ = _binding_index(context)
    known_parameters = {parameter.name for parameter in process.acp.parameters}
    allowed_sources = {"cpg-default", "clinician", "plan-derived", "reviewer", "authored"}
    for binding_name, bindings in indexed.items():
        if binding_name not in known_parameters and not binding_name.endswith("__code"):
            findings.append(
                _finding("I4", f"binding {binding_name!r} does not name an instance parameter")
            )
        if len(bindings) > 1 and binding_name in known_parameters:
            findings.append(
                _finding("I4", f"parameter {binding_name!r} is bound more than once")
            )

    values: dict[str, Any] = {}
    for parameter in process.acp.parameters:
        bindings = indexed.get(parameter.name, [])
        if len(bindings) != 1:
            if not bindings and parameter.required:
                findings.append(
                    _finding(
                        "incomplete-binding",
                        f"required parameter {parameter.name!r} is unbound; the instance is incomplete",
                        element_id=parameter.name,
                        severity="WARNING",
                    )
                )
            elif not bindings:
                findings.append(
                    _finding("I4", f"parameter {parameter.name!r} must have exactly one binding", element_id=parameter.name)
                )
            continue
        binding = bindings[0]
        source = _field(binding, "source")
        value = _field(binding, "value")
        if source not in allowed_sources:
            findings.append(
                _finding("I4", f"parameter {parameter.name!r} has unknown binding source {source!r}", element_id=parameter.name)
            )
        if not _binding_type_matches(parameter, value):
            findings.append(
                _finding(
                    "I4",
                    f"binding for {parameter.name!r} does not match type {parameter.type!r}",
                    element_id=parameter.name,
                )
            )
        if parameter.type == "quantity" and _field(binding, "unit") not in (None, parameter.unit):
            findings.append(
                _finding("I4", f"quantity binding for {parameter.name!r} has a different unit", element_id=parameter.name)
            )
        if not _within_constraints(parameter, value):
            findings.append(
                _finding("I4", f"binding for {parameter.name!r} is outside its constraints", element_id=parameter.name)
            )
        values[parameter.name] = _binding_runtime_value(parameter, binding)

    if process.acp.invariants:
        try:
            invariant_findings, _ = check_invariants(
                process.acp.invariants,
                _expression_env(process),
                values,
            )
        except (ExpressionError, TypeError, ValueError) as exc:
            findings.append(_finding("I4", f"instance invariant could not be checked: {exc}"))
        else:
            for finding in invariant_findings:
                findings.append(
                    _finding("I4", finding.message, element_id=finding.element_id)
                )
    return findings


def _check_main_boundary(process: Process) -> list[Finding]:
    boundaries = [
        element
        for element in process.flowElements
        if isinstance(element, BoundaryEvent) and element.attachedToRef == "main"
    ]
    if len(boundaries) != 1:
        return [
            _finding(
                "I5",
                f"instance requires exactly one main boundary; found {len(boundaries)}",
            )
        ]
    timer = boundaries[0].timerEventDefinition.timeDuration
    provenance = boundaries[0].acp.provenance
    if not (
        isinstance(timer, ParamRef)
        and timer.param == "max_duration"
        and isinstance(provenance, StructuralProvenance)
        and provenance.derivation_rule == "plan-bound"
    ):
        return [
            _finding(
                "I5",
                "the main boundary must be plan-bound to the max_duration parameter",
                element_id=boundaries[0].id,
            )
        ]
    return []


def _check_verbatim_text(
    process: Process,
    templates: Mapping[str, Process],
    context: InstanceContext,
) -> list[Finding]:
    findings: list[Finding] = []
    selected_names: list[str] = []
    clinician_bound = {
        str(_field(binding, "name"))
        for binding in context.bindings
        if _field(binding, "source") == "clinician"
    }
    instance_parameters = _parameter_by_name(process)
    for element in process.flowElements:
        ref = element.acp.template_ref
        if ref is None:
            continue
        template = templates.get(ref.template_id)
        source = _template_element_map(template).get(ref.element_id or "") if template else None
        if source is None:
            continue
        if ref.template_id not in selected_names:
            selected_names.append(ref.template_id)
        if element.name != source.name:
            findings.append(
                _finding("I6", f"element {element.id!r} changes its template name", element_id=element.id)
            )
    selected_processes = [templates[name] for name in selected_names if name in templates]
    if selected_processes:
        expected_name = (
            selected_processes[0].name
            if len(selected_processes) == 1
            else ", then ".join(item.name for item in selected_processes)
        )
        if process.name != expected_name:
            findings.append(_finding("I6", "instance process name does not follow template names verbatim"))
        if len(selected_processes) == 1 and process.description != selected_processes[0].description:
            findings.append(_finding("I6", "instance process description differs from its template"))

    for template_id in selected_names:
        template = templates.get(template_id)
        if template is None:
            continue
        for source_parameter in template.acp.parameters:
            mapped_name = (
                "max_duration"
                if source_parameter.name == "max_duration"
                else _mapped_item_name(
                    process,
                    template_id,
                    source_parameter.name,
                    parameter=True,
                )
            )
            instance_parameter = instance_parameters.get(mapped_name or "")
            if instance_parameter is None:
                continue
            if instance_parameter.description != source_parameter.description:
                findings.append(
                    _finding(
                        "I6",
                        f"parameter {instance_parameter.name!r} changes its template description",
                        element_id=instance_parameter.name,
                    )
                )
            changed_by_clinician = (
                instance_parameter.name in clinician_bound
                or _field(
                    next(
                        (
                            binding
                            for binding in context.bindings
                            if _field(binding, "name") == instance_parameter.name
                        ),
                        None,
                    ),
                    "source",
                )
                == "clinician"
            )
            if instance_parameter.default != source_parameter.default and not (
                changed_by_clinician
                and _field(instance_parameter.source, "kind") == "authored"
            ):
                findings.append(
                    _finding(
                        "I6",
                        f"parameter {instance_parameter.name!r} changes its template default without a clinician binding",
                        element_id=instance_parameter.name,
                    )
                )
            if (
                source_parameter.type == "string"
                and _field(source_parameter.source, "kind") == "authored"
            ):
                binding = next(
                    (
                        item
                        for item in context.bindings
                        if _field(item, "name") == instance_parameter.name
                    ),
                    None,
                )
                if binding is not None and _field(binding, "value") != source_parameter.default:
                    if _field(binding, "source") != "clinician":
                        findings.append(
                            _finding(
                                "I6",
                                f"authored text binding for {instance_parameter.name!r} differs from the template",
                                element_id=instance_parameter.name,
                            )
                        )
    return findings


def _branch_elements(template: Process, gateway: ExclusiveGateway, first_flow: SequenceFlow) -> set[str]:
    nodes = {element.id: element for element in template.flowElements if not isinstance(element, SequenceFlow)}
    outgoing: dict[str, list[SequenceFlow]] = defaultdict(list)
    for element in template.flowElements:
        if isinstance(element, SequenceFlow):
            outgoing[element.sourceRef].append(element)
    branch = {first_flow.id}
    pending = [first_flow.targetRef]
    seen_nodes: set[str] = set()
    while pending:
        current_id = pending.pop()
        if current_id in seen_nodes or current_id == gateway.id:
            continue
        node = nodes.get(current_id)
        if node is None:
            continue
        if isinstance(node, ExclusiveGateway) and node.gatewayDirection == "Converging":
            continue
        seen_nodes.add(current_id)
        branch.add(current_id)
        for flow in outgoing.get(current_id, []):
            branch.add(flow.id)
            if flow.targetRef not in seen_nodes:
                pending.append(flow.targetRef)
    return branch


def _evidence(context: InstanceContext, evidence_id: str) -> Any | None:
    return context.evidence.get(evidence_id)


def _check_prune_evidence(
    record: PruneRecord,
    template: Process,
    context: InstanceContext,
) -> list[str]:
    errors: list[str] = []
    evidence = _evidence(context, record.evidence_id)
    if evidence is None:
        return [f"prune {record.id!r} references missing evidence {record.evidence_id!r}"]
    payload = _field(evidence, "payload")
    evidence_kind = _field(evidence, "kind")
    elements = _template_element_map(template)
    pruned = set(record.element_ids)
    if any(item not in elements for item in pruned):
        errors.append(f"prune {record.id!r} names an unknown template element")

    if record.reason == "inapplicable-dmn-output":
        audit_id = _field(payload, "audit_id", _field(payload, "entry_id"))
        output_name = _field(payload, "output_name")
        output_value = _field(payload, "output_value")
        candidates = [
            entry
            for entry in context.dmn_audit_trail
            if audit_id is None or _field(entry, "id") == audit_id
        ]
        match = False
        for entry in candidates:
            outputs = _field(entry, "outputs", {})
            if output_name is not None and isinstance(outputs, Mapping):
                match = output_name in outputs and outputs[output_name] == output_value
            else:
                match = output_value is not None and _field(entry, "output_value") == output_value
            if match:
                break
        if evidence_kind not in {"dmn-output", "dmn-audit"} or not match:
            errors.append(f"prune {record.id!r} has no matching DMN output audit evidence")
    elif record.reason == "inapplicable-category":
        flag = _field(payload, "flag", _field(payload, "category"))
        condition_names: set[str] = set()
        for element in template.flowElements:
            if isinstance(element, SequenceFlow) and element.id in pruned and element.conditionExpression:
                try:
                    condition_names.update(identifiers(parse(element.conditionExpression)))
                except ExpressionError:
                    pass
        if (
            evidence_kind not in {"category-flag", "category"}
            or flag not in context.category_flags
            or flag not in condition_names
            or (
                _field(payload, "value") is not None
                and _field(payload, "value") != context.category_flags.get(flag)
            )
        ):
            errors.append(f"prune {record.id!r} has no matching category-flag evidence")
    elif record.reason == "clinician-directed":
        quote = payload if isinstance(payload, str) else _field(payload, "quote", _field(payload, "text", ""))
        if (
            evidence_kind not in {"careplan-feedback", "clinician-directed"}
            or not isinstance(quote, str)
            or not quote
            or normalize(quote) not in normalize(context.careplan_feedback)
        ):
            errors.append(f"prune {record.id!r} has no matching clinician feedback evidence")

    for element_id in pruned:
        element = elements.get(element_id)
        if isinstance(element, Task) and element.taskName in {
            "clinician.notify",
            "care_team.escalate",
        }:
            errors.append(f"prune {record.id!r} removes required clinical task {element_id!r}")
        if isinstance(element, UserTask):
            errors.append(f"prune {record.id!r} removes required user task {element_id!r}")
        if isinstance(element, BoundaryEvent) and isinstance(
            element.acp.provenance, StructuralProvenance
        ) and element.acp.provenance.derivation_rule == "plan-bound":
            errors.append(f"prune {record.id!r} removes plan-bound boundary {element_id!r}")
        if isinstance(element, SequenceFlow):
            source = elements.get(element.sourceRef)
            if isinstance(source, IntermediateCatchEvent):
                errors.append(f"prune {record.id!r} removes a timer-event outgoing flow")

    matching_branch = False
    for gateway in (
        element
        for element in template.flowElements
        if isinstance(element, ExclusiveGateway) and element.gatewayDirection == "Diverging"
    ):
        for flow in (
            element
            for element in template.flowElements
            if isinstance(element, SequenceFlow) and element.sourceRef == gateway.id
        ):
            if flow.id == gateway.default:
                if flow.id in pruned:
                    errors.append(f"prune {record.id!r} removes default flow {flow.id!r}")
                continue
            branch = _branch_elements(template, gateway, flow)
            if branch == pruned:
                matching_branch = True
    if not matching_branch:
        errors.append(f"prune {record.id!r} does not remove one complete non-default branch")
    return errors


def _activity_sequence_matches(context: InstanceContext, payload: Any) -> bool:
    activity_ids = _field(payload, "activity_ids", [])
    if not isinstance(activity_ids, Sequence) or isinstance(activity_ids, str) or len(activity_ids) != 2:
        return False
    activities = {
        str(_field(activity, "id")): activity
        for activity in context.activities
        if _field(activity, "id") is not None
    }
    before = activities.get(str(activity_ids[0]))
    after = activities.get(str(activity_ids[1]))
    if before is None or after is None:
        return False
    before_name = str(_field(before, "name", ""))
    before_id = str(_field(before, "id", ""))
    workflow = _field(after, "workflow", {})
    sequence_after = _field(after, "sequence_after", _field(workflow, "sequence_after"))
    expected = _field(payload, "sequence_after", before_name)
    return sequence_after in {before_name, before_id} and expected == sequence_after


def _trigger_chain_matches(
    flow: SequenceFlow,
    templates: Mapping[str, Process],
    instance_process: Process,
) -> bool:
    provenance = flow.acp.provenance
    if not isinstance(provenance, StructuralProvenance):
        return False
    replaces = provenance.replaces or {}
    source_template_id = replaces.get("template_id")
    source_element_id = replaces.get("element_id")
    target = next(
        (element for element in instance_process.flowElements if element.id == flow.targetRef),
        None,
    )
    target_ref = target.acp.template_ref if target else None
    if source_template_id not in templates or target_ref is None:
        return False
    source_template = templates[source_template_id]
    target_template = templates.get(target_ref.template_id)
    if target_template is None:
        return False
    source_end = _template_element_map(source_template).get(source_element_id)
    if not isinstance(source_end, EndEvent) or source_end.acp.outcome is None:
        return False
    produced = [
        item.trigger
        for item in source_template.acp.produces
        if item.outcome == source_end.acp.outcome
    ]
    wanted = [trigger.model_dump(mode="json", exclude_none=True) for trigger in produced]
    return any(
        trigger.model_dump(mode="json", exclude_none=True) in wanted
        for trigger in target_template.acp.triggers
    )


def _check_justification(
    flow: SequenceFlow,
    templates: Mapping[str, Process],
    context: InstanceContext,
    instance_process: Process,
) -> list[str]:
    provenance = flow.acp.provenance
    if not isinstance(provenance, StructuralProvenance) or provenance.derivation_rule != "fragment-boundary":
        return []
    errors: list[str] = []
    source = next(
        (element for element in instance_process.flowElements if element.id == flow.sourceRef),
        None,
    )
    target = next(
        (element for element in instance_process.flowElements if element.id == flow.targetRef),
        None,
    )
    source_ref = source.acp.template_ref if source else None
    target_ref = target.acp.template_ref if target else None
    if source_ref is None or target_ref is None or source_ref.template_id == target_ref.template_id:
        errors.append(f"fragment boundary {flow.id!r} must connect two different template fragments")
    justification = provenance.justification
    if justification is None:
        return errors + [f"fragment boundary {flow.id!r} requires a justification"]
    evidence = _evidence(context, justification.evidence_id)
    if evidence is None:
        return errors + [f"fragment boundary {flow.id!r} references missing evidence"]
    payload = _field(evidence, "payload")
    if justification.kind == "plan-sequence":
        if _field(evidence, "kind") != "plan-sequence" or not _activity_sequence_matches(context, payload):
            errors.append(f"fragment boundary {flow.id!r} has invalid plan-sequence evidence")
    elif justification.kind == "clinician-directed":
        quote = payload if isinstance(payload, str) else _field(payload, "quote", _field(payload, "text", ""))
        if (
            _field(evidence, "kind") not in {"careplan-feedback", "clinician-directed"}
            or not isinstance(quote, str)
            or not quote
            or normalize(quote) not in normalize(context.careplan_feedback)
        ):
            errors.append(f"fragment boundary {flow.id!r} has invalid clinician-feedback evidence")
    elif justification.kind == "trigger-chain":
        if _field(evidence, "kind") != "trigger-chain" or not _trigger_chain_matches(
            flow, templates, instance_process
        ):
            errors.append(f"fragment boundary {flow.id!r} has no matching trigger chain")
    return errors


def _check_fragment_scoping(
    process: Process,
    templates: Mapping[str, Process],
) -> list[Finding]:
    findings: list[Finding] = []
    property_owners: dict[str, list[tuple[str, str, str, str | None, str | None]]] = defaultdict(list)
    parameter_owners: dict[str, list[tuple[str, str, str, str | None, str | None]]] = defaultdict(list)
    active_template_ids = {item.template_id for item in process.acp.template_refs}
    composed = len(active_template_ids) > 1
    prefixes = {
        template_id: _fragment_prefix(process, template_id)
        for template_id in active_template_ids
    }
    for element in process.flowElements:
        ref = element.acp.template_ref
        if not composed or ref is None or isinstance(element, BoundaryEvent) and element.attachedToRef == "main":
            continue
        prefix = prefixes.get(ref.template_id)
        if prefix is None or element.id != f"{prefix}__{ref.element_id}":
            findings.append(
                _finding(
                    "I9",
                    f"composed element {element.id!r} does not use its fragment's consistent prefix",
                    element_id=element.id,
                )
            )
    for template_id in active_template_ids:
        template = templates.get(template_id)
        if template is None:
            continue
        for source_prop in template.properties:
            mapped_name = _mapped_item_name(process, template_id, source_prop.name)
            instance_prop = _property_by_name(process).get(mapped_name or "")
            if instance_prop is not None:
                prefix = prefixes.get(template_id)
                member = f"{prefix}__{source_prop.name}" if prefix else source_prop.name
                property_owners[instance_prop.name].append(
                    (template_id, source_prop.name, member, source_prop.type, source_prop.unit)
                )
        for source_param in template.acp.parameters:
            mapped_name = (
                "max_duration"
                if source_param.name == "max_duration"
                else _mapped_item_name(
                    process,
                    template_id,
                    source_param.name,
                    parameter=True,
                )
            )
            instance_param = _parameter_by_name(process).get(mapped_name or "")
            if instance_param is not None:
                prefix = prefixes.get(template_id)
                member = f"{prefix}__{source_param.name}" if prefix else source_param.name
                parameter_owners[instance_param.name].append(
                    (template_id, source_param.name, member, source_param.type, source_param.unit)
                )

    unify_by_name: dict[str, list[Any]] = defaultdict(list)
    for record in process.acp.unify:
        unify_by_name[record.name].append(record)
    expected_unify_names: set[str] = set()
    for item_kind, groups in (("property", property_owners), ("parameter", parameter_owners)):
        for instance_name, owners in groups.items():
            template_ids = {owner[0] for owner in owners}
            if instance_name == "max_duration" and item_kind == "parameter":
                continue
            if len(template_ids) > 1:
                expected_unify_names.add(instance_name)
                specs = {(owner[3], owner[4]) for owner in owners}
                expected_members = {owner[2] for owner in owners}
                records = unify_by_name.get(instance_name, [])
                if len(specs) != 1:
                    findings.append(
                        _finding(
                            "I9",
                            f"shared {item_kind} {instance_name!r} has incompatible types or units",
                            element_id=instance_name,
                        )
                    )
                if len(records) != 1 or set(records[0].members) != expected_members or len(records[0].members) != len(expected_members):
                    findings.append(
                        _finding(
                            "I9",
                            f"shared {item_kind} {instance_name!r} needs one unify record with every fragment member",
                            element_id=instance_name,
                        )
                    )
            elif composed and any(owner[1] == instance_name for owner in owners):
                findings.append(
                    _finding(
                        "I9",
                        f"{item_kind} {instance_name!r} must be fragment-prefixed unless shared across templates",
                        element_id=instance_name,
                    )
                )
    for name in unify_by_name:
        if name not in expected_unify_names:
            findings.append(
                _finding("I9", f"unify record {name!r} does not describe a shared item", element_id=name)
            )
    return findings


def _check_composition(
    process: Process,
    templates: Mapping[str, Process],
    context: InstanceContext,
) -> list[Finding]:
    findings: list[Finding] = []
    active_ids = {
        element.acp.template_ref.template_id
        for element in process.flowElements
        if element.acp.template_ref is not None
    }
    boundaries = [
        element
        for element in process.flowElements
        if isinstance(element, SequenceFlow)
        and isinstance(element.acp.provenance, StructuralProvenance)
        and element.acp.provenance.derivation_rule == "fragment-boundary"
    ]
    for flow in process.flowElements:
        if not isinstance(flow, SequenceFlow):
            continue
        source = next((item for item in process.flowElements if item.id == flow.sourceRef), None)
        target = next((item for item in process.flowElements if item.id == flow.targetRef), None)
        source_ref = source.acp.template_ref if source else None
        target_ref = target.acp.template_ref if target else None
        crosses_fragments = (
            source_ref is not None
            and target_ref is not None
            and source_ref.template_id != target_ref.template_id
        )
        is_fragment_boundary = flow in boundaries
        if crosses_fragments and not is_fragment_boundary:
            findings.append(
                _finding("I9", f"cross-template flow {flow.id!r} lacks fragment-boundary provenance", element_id=flow.id)
            )
        for error in _check_justification(flow, templates, context, process):
            findings.append(_finding("I9", error, element_id=flow.id))
    if len(active_ids) > 1 and not boundaries:
        findings.append(_finding("I9", "multiple template fragments require a justified fragment boundary"))
    findings.extend(_check_fragment_scoping(process, templates))
    return findings


def _check_pruning(
    process: Process,
    templates: Mapping[str, Process],
    context: InstanceContext,
) -> list[Finding]:
    findings: list[Finding] = []
    retained = {
        (element.acp.template_ref.template_id, element.acp.template_ref.element_id)
        for element in process.flowElements
        if element.acp.template_ref is not None
    }
    for record in process.acp.pruned:
        template = templates.get(record.template_id)
        if template is None:
            findings.append(
                _finding("I8", f"prune {record.id!r} references an unknown template")
            )
            continue
        for error in _check_prune_evidence(record, template, context):
            findings.append(_finding("I8", error, element_id=record.id))
        for element_id in record.element_ids:
            if (record.template_id, element_id) in retained:
                findings.append(
                    _finding(
                        "I8",
                        f"pruned template element {element_id!r} is still retained in the instance",
                        element_id=element_id,
                    )
                )
        for gateway in (
            element
            for element in template.flowElements
            if isinstance(element, ExclusiveGateway)
            and element.gatewayDirection == "Diverging"
        ):
            if gateway.default is None:
                continue
            if not any(
                ref_template == record.template_id and ref_element == gateway.default
                for ref_template, ref_element in retained
            ):
                findings.append(
                    _finding(
                        "I8",
                        f"pruned fragment does not retain default flow {gateway.default!r}",
                        element_id=gateway.id,
                    )
                )
    return findings


def _binding_value_for(
    context: InstanceContext,
    parameter_name: str,
) -> Any | None:
    matches = [
        binding
        for binding in context.bindings
        if _field(binding, "name") == parameter_name
        or str(_field(binding, "name", "")).endswith(f"__{parameter_name}")
    ]
    return _field(matches[0], "value") if len(matches) == 1 else None


def _template_invariant_findings(
    template: Process,
    context: InstanceContext,
) -> list[Finding]:
    parameters = _parameter_by_name(template)
    values: dict[str, Any] = {}
    for parameter in parameters.values():
        value = _binding_value_for(context, parameter.name)
        if parameter.name == "max_duration":
            value = _binding_value_for(context, "max_duration")
        if value is not None:
            if parameter.type == "duration" and isinstance(value, str):
                try:
                    value = parse_duration(value)
                except ExpressionError:
                    pass
            values[parameter.name] = value
    if not template.acp.invariants:
        return []
    try:
        violations, _ = check_invariants(
            template.acp.invariants,
            _expression_env(template),
            values,
        )
    except (ExpressionError, TypeError, ValueError) as exc:
        return [_finding("I10", f"template invariant could not be checked: {exc}")]
    return [
        _finding("I10", finding.message, element_id=finding.element_id)
        for finding in violations
    ]


def _check_boundary_consolidation(
    process: Process,
    templates: Mapping[str, Process],
    context: InstanceContext,
    composition_has_justification: bool,
) -> list[Finding]:
    findings: list[Finding] = []
    main = [
        element
        for element in process.flowElements
        if isinstance(element, BoundaryEvent) and element.attachedToRef == "main"
    ]
    if len(main) != 1:
        return [_finding("I10", "boundary consolidation requires exactly one surviving main boundary")]
    survivor_ref = main[0].acp.template_ref
    if survivor_ref is None:
        findings.append(_finding("I10", "surviving main boundary must reference its source template"))
        return findings

    consolidated = {(item.template_id, item.element_id) for item in process.acp.consolidated_boundaries}
    if len(consolidated) != len(process.acp.consolidated_boundaries):
        findings.append(_finding("I10", "consolidated boundaries must be unique"))
    expected_consolidated: set[tuple[str, str]] = set()
    for template_id in {
        item.template_id for item in process.acp.template_refs
    }:
        template = templates.get(template_id)
        if template is None:
            continue
        boundaries = [
            element
            for element in template.flowElements
            if isinstance(element, BoundaryEvent)
            and element.attachedToRef == "main"
            and isinstance(element.acp.provenance, StructuralProvenance)
            and element.acp.provenance.derivation_rule == "plan-bound"
        ]
        for boundary in boundaries:
            identity = (template_id, boundary.id)
            if identity == (survivor_ref.template_id, survivor_ref.element_id):
                continue
            expected_consolidated.add(identity)
            if identity not in consolidated:
                findings.append(
                    _finding(
                        "I10",
                        f"plan-bound boundary {template_id!r}/{boundary.id!r} is not recorded as consolidated",
                    )
                )
            findings.extend(_template_invariant_findings(template, context))
    for identity in consolidated - expected_consolidated:
        template = templates.get(identity[0])
        source = _template_element_map(template).get(identity[1]) if template else None
        if identity == (survivor_ref.template_id, survivor_ref.element_id):
            findings.append(_finding("I10", "surviving boundary cannot also be consolidated"))
        elif not isinstance(source, BoundaryEvent):
            findings.append(_finding("I10", f"consolidated boundary {identity!r} does not resolve"))
    if expected_consolidated and not composition_has_justification:
        findings.append(_finding("I10", "boundary consolidation requires a justified composition"))
    if len({item.template_id for item in process.acp.template_refs}) <= 1 and consolidated:
        findings.append(_finding("I10", "a single-template instance cannot consolidate boundaries"))
    return findings


@trace(name="bpmn.l5.instance")
def validate_instance(
    ir: ProcessIR,
    templates: list[ProcessIR],
    context: InstanceContext,
) -> list[Finding]:
    """Check an instance against selected templates, bindings, and evidence."""
    process = _process(ir)
    template_processes = {_process(template).id: _process(template) for template in templates}
    findings, retained, _ = _template_reference_findings(
        process, template_processes, context
    )
    findings.extend(_check_instance_data_shapes(process, template_processes))
    findings.extend(_check_instance_provenance(process))
    findings.extend(_check_fragment_integrity(process, template_processes, retained))
    findings.extend(_check_bindings_and_invariants(process, context))
    findings.extend(_check_main_boundary(process))
    findings.extend(_check_verbatim_text(process, template_processes, context))
    findings.extend(_check_pruning(process, template_processes, context))
    composition_findings = _check_composition(process, template_processes, context)
    findings.extend(composition_findings)
    has_justified_boundary = not any(
        finding.code == "I9" for finding in composition_findings
    ) and any(
        isinstance(element, SequenceFlow)
        and isinstance(element.acp.provenance, StructuralProvenance)
        and element.acp.provenance.derivation_rule == "fragment-boundary"
        and element.acp.provenance.justification is not None
        for element in process.flowElements
    )
    findings.extend(
        _check_boundary_consolidation(
            process,
            template_processes,
            context,
            has_justified_boundary,
        )
    )
    return findings
