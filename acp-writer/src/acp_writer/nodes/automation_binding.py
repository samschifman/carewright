"""Bind a synthesized automation definition to planning-brief facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import os
from typing import Any

import mlflow

from cpg_contracts.automation.compiler.kogito_profile import normalize_duration
from cpg_contracts.automation.instances import Evidence, ParameterBinding
from cpg_contracts.automation.ir import (
    ConceptRef,
    Parameter,
    Process,
    ProcessIR,
    Task,
)
from cpg_contracts.automation.validators.results import Finding
from acp_writer.tools.concept_resolver import resolve as resolve_concept


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _process(value: Any) -> Process | None:
    if isinstance(value, ProcessIR):
        return value.process
    if isinstance(value, Process):
        return value
    candidate = _field(value, "ir")
    if isinstance(candidate, ProcessIR):
        return candidate.process
    if isinstance(candidate, Mapping):
        try:
            return ProcessIR.model_validate({"ir_version": "1.0", "process": candidate}).process
        except Exception:
            return None
    return None


def _template_processes(templates: Sequence[Any]) -> dict[str, Process]:
    result: dict[str, Process] = {}
    for template in templates:
        process = _process(template)
        if process is not None:
            result[process.id] = process
    return result


def _instance_parameter_name(process: Process, source_name: str) -> str | None:
    names = [parameter.name for parameter in process.acp.parameters]
    if source_name in names:
        return source_name
    matches = [name for name in names if name.endswith(f"__{source_name}")]
    return matches[0] if len(matches) == 1 else None


def _duration_from_period(value: Any, *, as_of: str | None = None) -> str | None:
    """Read a period as an ISO duration or derive whole days from FHIR Period."""
    if isinstance(value, str):
        try:
            return normalize_duration(value)
        except ValueError:
            return None
    if not isinstance(value, Mapping):
        return None
    duration = value.get("duration")
    if isinstance(duration, str):
        try:
            return normalize_duration(duration)
        except ValueError:
            return None
    end = value.get("end")
    if not isinstance(end, str):
        return None
    start = value.get("start") or as_of or date.today().isoformat()
    if not isinstance(start, str):
        return None
    try:
        days = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
    except ValueError:
        return None
    return f"P{days}D" if days > 0 else None


def known_phi_values(patient_demographics: Mapping[str, Any] | None) -> list[str]:
    """Return patient names, identifiers, and birth dates for the L3 PHI scan."""
    if not isinstance(patient_demographics, Mapping):
        return []
    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in values:
            values.append(value.strip())
        elif isinstance(value, Mapping):
            for child in value.values():
                add(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                add(child)

    for key in ("name", "names", "identifiers", "birth_date", "birthDate"):
        add(patient_demographics.get(key))
    return values


def _prior_clinician_bindings(activity: Any, brief: Any) -> list[ParameterBinding]:
    activity_id = _field(activity, "id")
    prior_brief = _field(brief, "prior_planning_brief", brief)
    activities = _field(prior_brief, "activities", []) or []
    result: list[ParameterBinding] = []
    for prior_activity in activities:
        if activity_id and _field(prior_activity, "id") != activity_id:
            continue
        for automation in _field(prior_activity, "automations", []) or []:
            for binding in _field(automation, "bindings", []) or []:
                try:
                    parsed = (
                        binding
                        if isinstance(binding, ParameterBinding)
                        else ParameterBinding.model_validate(binding)
                    )
                except Exception:
                    continue
                if parsed.source == "clinician":
                    result.append(parsed)
    return result


def _concept_binding(
    element: Task,
    input_name: str,
    concept: ConceptRef,
) -> tuple[ParameterBinding | None, Finding | None]:
    resolved = resolve_concept(concept.concept)
    if resolved is None or not resolved.system or not resolved.code:
        return None, Finding(
            rung="L5a",
            severity="WARNING",
            code="incomplete-binding",
            message=f"concept {concept.concept!r} on task {element.id!r} could not be resolved to one code",
            element_id=element.id,
        )
    code = f"{resolved.system}|{resolved.code}"
    return (
        ParameterBinding(
            name=f"{element.id}__{input_name}",
            value=code,
            source="plan-derived",
            note=f"Resolved concept {concept.concept!r} to {code}.",
        ),
        None,
    )


@mlflow.trace(name="automation.bind_instance")
def bind_instance(
    instance_ir: ProcessIR,
    templates: Sequence[Any],
    *,
    activity: Any,
    brief: Any,
    policy: Mapping[str, Any] | None,
    initial_bindings: Sequence[ParameterBinding | Mapping[str, Any]] = (),
) -> tuple[ProcessIR, list[ParameterBinding], dict[str, Evidence], list[Finding]]:
    """Resolve concepts and bind defaults, prior clinician values, and monitoring period.

    `monitoring_period` remains an ordinary CPG-sourced template parameter. Its
    value can supply the reserved `max_duration` binding, but the reserved
    parameter itself never receives a template default.
    """
    policy = policy or {}
    result_ir = instance_ir.model_copy(deep=True)
    process = result_ir.process
    template_processes = _template_processes(templates)
    bindings: dict[str, ParameterBinding] = {}
    findings: list[Finding] = []
    evidence: dict[str, Evidence] = {}

    for raw in initial_bindings:
        try:
            binding = raw if isinstance(raw, ParameterBinding) else ParameterBinding.model_validate(raw)
        except Exception as exc:
            findings.append(Finding(
                rung="L5a", severity="ERROR", code="invalid-binding",
                message=f"synthesizer returned an invalid parameter binding: {exc}",
            ))
            continue
        bindings[binding.name] = binding

    for binding in _prior_clinician_bindings(activity, brief):
        if binding.name in {parameter.name for parameter in process.acp.parameters}:
            bindings[binding.name] = binding

    # Concept references are part of the pinned template's I/O map. Resolve
    # them into separate plan-derived bindings without rewriting that map.
    for element in process.flowElements:
        if not isinstance(element, Task):
            continue
        for input_name, value in element.inputs.items():
            if not isinstance(value, ConceptRef):
                continue
            binding, finding = _concept_binding(element, input_name, value)
            if finding is not None:
                findings.append(finding)
                continue
            assert binding is not None
            bindings[binding.name] = binding

    template_parameters: dict[str, Parameter] = {}
    for template in template_processes.values():
        for parameter in template.acp.parameters:
            if parameter.name != "max_duration":
                template_parameters.setdefault(parameter.name, parameter)

    # Default bindings are copied from the pinned template definition; an
    # existing synthesizer or clinician binding has higher precedence.
    for source_name, parameter in template_parameters.items():
        target_name = _instance_parameter_name(process, source_name)
        if target_name is None or target_name in bindings or parameter.default is None:
            continue
        source_kind = parameter.source.kind if parameter.source is not None else None
        source = {
            "cpg": "cpg-default",
            # Authored string content remains a pinned template default unless
            # a clinician binding explicitly replaces it.
            "authored": "cpg-default" if parameter.type == "string" else "authored",
            "reviewer": "reviewer",
            "policy": "plan-derived",
        }.get(source_kind)
        if source is None:
            continue
        bindings[target_name] = ParameterBinding(
            name=target_name,
            value=parameter.default,
            source=source,
            unit=parameter.unit,
        )

    # Monitoring period precedence: CPG source parameter, CarePlan period, then
    # the environment policy. `monitoring_period` is deliberately not reserved.
    cpg_period: tuple[str, str] | None = None
    for template in template_processes.values():
        candidate = next(
            (parameter for parameter in template.acp.parameters
             if parameter.name == "monitoring_period"
             and parameter.type == "duration"
             and parameter.default is not None
             and parameter.source is not None
             and parameter.source.kind == "cpg"),
            None,
        )
        if candidate is not None:
            try:
                cpg_period = (normalize_duration(str(candidate.default)), candidate.name)
            except ValueError:
                findings.append(Finding(
                    rung="L5a", severity="ERROR", code="invalid-monitoring-period",
                    message="CPG monitoring_period is not a supported ISO duration",
                    element_id=candidate.name,
                ))
            if cpg_period:
                break

    if cpg_period is not None:
        cpg_value, source_name = cpg_period
        target_name = _instance_parameter_name(process, source_name)
        if target_name is not None and target_name not in bindings:
            bindings[target_name] = ParameterBinding(
                name=target_name,
                value=cpg_value,
                source="cpg-default",
            )
        max_duration = cpg_value
        max_source = "cpg-default"
        max_note = f"Bound from CPG-sourced parameter {source_name}."
    else:
        period = policy.get("careplan_period", _field(brief, "careplan_period"))
        max_duration = _duration_from_period(period, as_of=policy.get("as_of"))
        if max_duration is not None:
            max_source = "plan-derived"
            max_note = "Bound from CarePlan.period."
        else:
            raw_default = policy.get(
                "environment_default",
                os.getenv("ACP_AUTOMATION_DEFAULT_MONITORING_PERIOD", "P90D"),
            )
            try:
                max_duration = normalize_duration(str(raw_default))
            except ValueError:
                max_duration = None
                findings.append(Finding(
                    rung="L5a", severity="ERROR", code="invalid-monitoring-period",
                    message="policy monitoring-period default is not a supported ISO duration",
                    element_id="max_duration",
                ))
            max_source = "plan-derived"
            max_note = "Bound from policy automation.default_monitoring_period."

    if max_duration is not None and any(p.name == "max_duration" for p in process.acp.parameters):
        bindings["max_duration"] = ParameterBinding(
            name="max_duration", value=max_duration, source=max_source, note=max_note
        )
        evidence["ev-monitoring-period"] = Evidence(
            kind="monitoring-period",
            payload={"value": max_duration, "source": max_source, "note": max_note},
        )

    for parameter in process.acp.parameters:
        if parameter.name not in bindings and parameter.required:
            findings.append(Finding(
                rung="L5a", severity="WARNING", code="incomplete-binding",
                message=f"required parameter {parameter.name!r} is unbound; the instance is incomplete",
                element_id=parameter.name,
            ))

    return result_ir, list(bindings.values()), evidence, findings


__all__ = ["bind_instance", "known_phi_values"]
