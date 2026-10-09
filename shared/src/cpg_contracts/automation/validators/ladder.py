"""Run the deterministic BPMN validation rungs from IR through grounding."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
import re
from typing import Literal

from pydantic import ValidationError

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.catalog import Catalog
from cpg_contracts.automation.compiler import ParseError, compile, parse
from cpg_contracts.automation.expressions import (
    ExpressionError,
    TypeInfo,
    check_invariants,
    parse_duration,
    typecheck,
)
from cpg_contracts.automation.instances import ParameterBinding
from cpg_contracts.automation.ir import (
    BusinessRuleTask,
    Parameter,
    ProcessIR,
    SequenceFlow,
    Task,
)
from cpg_contracts.automation.validators.catalog_conformance import (
    validate_catalog,
    validate_catalog_xml,
)
from cpg_contracts.automation.validators.grounding import (
    InstanceContext,
    validate_instance,
    validate_template_structure,
    validate_template_text,
)
from cpg_contracts.automation.validators.lint import validate_structure
from cpg_contracts.automation.validators.phi import scan_phi
from cpg_contracts.automation.validators.preflight import validate_preflight
from cpg_contracts.automation.validators.results import Finding, LadderResult
from cpg_contracts.automation.validators.xsd import validate_xsd
from cpg_contracts.decisions import DecisionModelSummary


Stage = Literal["template", "instance"]


def _finding(rung: str, code: str, message: str, element_id: str | None = None) -> Finding:
    return Finding(
        rung=rung,
        severity="ERROR",
        code=code,
        message=message,
        element_id=element_id,
    )


def _expression_environment(ir: ProcessIR) -> dict[str, TypeInfo]:
    process = ir.process
    environment = {
        prop.name: TypeInfo(type=prop.type, unit=prop.unit)
        for prop in process.properties
    }
    environment.update(
        {
            parameter.name: TypeInfo(type=parameter.type, unit=parameter.unit)
            for parameter in process.acp.parameters
        }
    )
    return environment


def _runtime_value(parameter: Parameter, value: object) -> object:
    if parameter.type == "duration" and isinstance(value, str):
        return parse_duration(value)
    return value


def _matches_type(parameter: Parameter, value: object) -> bool:
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
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if parameter.type == "boolean":
        return isinstance(value, bool)
    if parameter.type == "string":
        return isinstance(value, str)
    return False


def _within_constraints(parameter: Parameter, value: object) -> bool:
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


def _l0(
    ir: ProcessIR,
    *,
    stage: Stage,
    catalog: Catalog,
    context: InstanceContext | None,
) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    deferred: list[str] = []
    if ir.process.kind != stage:
        findings.append(
            _finding(
                "L0",
                "stage-kind",
                f"stage {stage!r} requires process kind {stage!r}, got {ir.process.kind!r}",
            )
        )

    for element in ir.process.flowElements:
        if isinstance(element, Task):
            try:
                catalog.get(element.taskName)
            except KeyError:
                findings.append(
                    _finding(
                        "L0",
                        "unknown-capability",
                        f"task {element.id!r} references an unknown capability",
                        element.id,
                    )
                )

    parameters = {parameter.name: parameter for parameter in ir.process.acp.parameters}
    supplied_values: dict[str, object] = {}
    if ir.process.kind == "template":
        for parameter in parameters.values():
            if parameter.default is None:
                continue
            if not _matches_type(parameter, parameter.default):
                findings.append(
                    _finding(
                        "L0",
                        "parameter-default-type",
                        f"default for parameter {parameter.name!r} does not match its type",
                        parameter.name,
                    )
                )
                continue
            if not _within_constraints(parameter, parameter.default):
                findings.append(
                    _finding(
                        "L0",
                        "parameter-default-constraint",
                        f"default for parameter {parameter.name!r} is outside its constraints",
                        parameter.name,
                    )
                )
            try:
                supplied_values[parameter.name] = _runtime_value(parameter, parameter.default)
            except ExpressionError as exc:
                findings.append(
                    _finding("L0", "parameter-default-duration", str(exc), parameter.name)
                )
    elif context is not None:
        bindings_by_name: dict[str, list[ParameterBinding]] = {}
        for binding in context.bindings:
            bindings_by_name.setdefault(binding.name, []).append(binding)
        for name, bindings in bindings_by_name.items():
            parameter = parameters.get(name)
            if parameter is None or len(bindings) != 1:
                continue
            value = bindings[0].value
            if not _matches_type(parameter, value):
                findings.append(
                    _finding(
                        "L0",
                        "parameter-binding-type",
                        f"binding for parameter {name!r} does not match its type",
                        name,
                    )
                )
                continue
            if not _within_constraints(parameter, value):
                findings.append(
                    _finding(
                        "L0",
                        "parameter-binding-constraint",
                        f"binding for parameter {name!r} is outside its constraints",
                        name,
                    )
                )
            try:
                supplied_values[name] = _runtime_value(parameter, value)
            except ExpressionError as exc:
                findings.append(
                    _finding("L0", "parameter-binding-duration", str(exc), name)
                )

    environment = _expression_environment(ir)
    for element in ir.process.flowElements:
        if not isinstance(element, SequenceFlow) or element.conditionExpression is None:
            continue
        try:
            result_type = typecheck(element.conditionExpression, environment)
            if result_type != "boolean":
                raise ExpressionError("condition must have boolean type")
        except ExpressionError as exc:
            findings.append(
                _finding("L0", "condition-expression", str(exc), element.id)
            )

    try:
        invariant_findings, deferred = check_invariants(
            ir.process.acp.invariants,
            environment,
            supplied_values,
        )
        findings.extend(invariant_findings)
    except (ExpressionError, TypeError, ValueError) as exc:
        findings.append(_finding("L0", "invariant-expression", str(exc)))
    return findings, deferred


def _result(
    *,
    findings: list[Finding],
    rungs_passed: list[str],
    deferred: list[str],
    failing_rung: str | None = None,
) -> LadderResult:
    if failing_rung is not None:
        return LadderResult(False, findings, rungs_passed, deferred)
    return LadderResult(not any(item.severity == "ERROR" for item in findings), findings, rungs_passed, deferred)


@trace(name="bpmn.validation_ladder.static")
def run_static_ladder(
    ir: ProcessIR | str,
    *,
    catalog: Catalog,
    stage: Stage,
    section_text: str | Mapping[str, str] | None = None,
    templates: list[ProcessIR] | None = None,
    context: InstanceContext | None = None,
    dmn_summaries: dict[str, DecisionModelSummary] | None = None,
    phi_known_values: Iterable[str] = (),
) -> LadderResult:
    """Run L0–L5 in order, stopping after the first rung with an error."""
    xml: str | None = None
    preflight_findings: list[Finding] | None = None
    if isinstance(ir, str):
        xml = ir
        preflight_findings = validate_preflight(xml)
        if any(item.severity == "ERROR" for item in preflight_findings):
            return _result(
                findings=preflight_findings,
                rungs_passed=[],
                deferred=[],
                failing_rung="L1",
            )
        try:
            process_ir = parse(xml)
        except (ParseError, ValueError, ValidationError) as exc:
            finding = _finding("L0", "xml-ir-parse", f"BPMN XML cannot be recovered as a process IR: {exc}")
            return _result(findings=[finding], rungs_passed=[], deferred=[], failing_rung="L0")
    else:
        try:
            process_ir = ProcessIR.model_validate(ir.model_dump(mode="python"))
        except (ValidationError, TypeError, ValueError) as exc:
            finding = _finding("L0", "ir-schema", f"process IR is invalid: {exc}")
            return _result(findings=[finding], rungs_passed=[], deferred=[], failing_rung="L0")

    findings, deferred = _l0(
        process_ir,
        stage=stage,
        catalog=catalog,
        context=context,
    )
    result = _result(
        findings=findings,
        rungs_passed=[],
        deferred=deferred,
        failing_rung="L0" if any(item.severity == "ERROR" for item in findings) else None,
    )
    if not result.ok:
        return result
    result.rungs_passed.append("L0")

    if xml is None:
        namespaces = {
            model_id: summary.namespace
            for model_id, summary in (dmn_summaries or {}).items()
            if summary.namespace is not None
        }
        try:
            xml = compile(process_ir, catalog=catalog, dmn_namespaces=namespaces)
        except (ParseError, TypeError, ValueError, KeyError) as exc:
            result.findings.append(
                _finding("L1", "compiler-output", f"compiler could not produce BPMN XML: {exc}")
            )
            result.ok = False
            return result

    l1_findings = preflight_findings if preflight_findings is not None else validate_preflight(xml)
    result.findings.extend(l1_findings)
    if any(item.severity == "ERROR" for item in l1_findings):
        result.ok = False
        return result
    result.rungs_passed.append("L1")

    l2_findings = validate_xsd(xml)
    result.findings.extend(l2_findings)
    if any(item.severity == "ERROR" for item in l2_findings):
        result.ok = False
        return result
    result.rungs_passed.append("L2")

    l3_findings = [*validate_structure(xml), *scan_phi(xml, phi_known_values)]
    result.findings.extend(l3_findings)
    if any(item.severity == "ERROR" for item in l3_findings):
        result.ok = False
        return result
    result.rungs_passed.append("L3")

    l4_findings = [
        *validate_catalog(process_ir, catalog, dmn_summaries),
        *validate_catalog_xml(xml, catalog),
    ]
    result.findings.extend(l4_findings)
    if any(item.severity == "ERROR" for item in l4_findings):
        result.ok = False
        return result
    result.rungs_passed.append("L4")

    if stage == "template":
        l5a_findings = validate_template_structure(process_ir)
    elif templates is None or context is None:
        l5a_findings = [
            _finding(
                "L5a",
                "instance-context",
                "instance grounding requires selected templates and an InstanceContext",
            )
        ]
    else:
        l5a_findings = validate_instance(process_ir, templates, context)
    result.findings.extend(l5a_findings)
    if any(item.severity == "ERROR" for item in l5a_findings):
        result.ok = False
        return result
    result.rungs_passed.append("L5a")

    if stage == "template" and section_text is not None:
        l5b_findings = validate_template_text(process_ir, section_text)
        result.findings.extend(l5b_findings)
        if any(item.severity == "ERROR" for item in l5b_findings):
            result.ok = False
            return result
        result.rungs_passed.append("L5b")
    return result
