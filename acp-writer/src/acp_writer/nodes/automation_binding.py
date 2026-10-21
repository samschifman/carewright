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
    BusinessRuleTask,
    ConceptRef,
    ParamRef,
    Parameter,
    Process,
    ProcessIR,
    Task,
)
from cpg_contracts.automation.validators.results import Finding
from acp_writer.automation_policy import validate_automation_policy
from acp_writer.tools.concept_resolver import resolve as resolve_concept
from acp_writer.tools.terminology_lookup import LOINC_SYSTEM, find as find_terminology_concept


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


def _matching_enum_outputs(
    parameter: Parameter,
    dmn_audit_trail: Sequence[Any],
    linked_model_ids: set[str],
) -> list[dict[str, Any]]:
    """Find DMN string outputs that state one declared CPG enum value verbatim."""
    if (
        parameter.source is None
        or parameter.source.kind != "cpg"
        or parameter.constraints is None
        or not parameter.constraints.enum
    ):
        return []

    matches: list[dict[str, Any]] = []
    for audit in dmn_audit_trail:
        if _field(audit, "model_id") not in linked_model_ids:
            continue
        outputs = _field(audit, "outputs", {}) or {}
        if not isinstance(outputs, Mapping):
            continue
        for output_name, output_value in outputs.items():
            if not isinstance(output_value, str):
                continue
            value = output_value.strip()
            for enum_value in parameter.constraints.enum:
                folded_value = value.casefold()
                folded_enum = enum_value.casefold()
                if folded_value.startswith(folded_enum):
                    suffix = folded_value[len(folded_enum):]
                    if not suffix or suffix[0].isspace() or suffix[0] in ":;—-":
                        matches.append({
                            "enum_value": enum_value,
                            "audit": audit,
                            "output_name": str(output_name),
                            "output_value": output_value,
                        })
    return matches


def _linked_decision_model_ids(
    templates: Sequence[Any], selected_template_ids: set[str]
) -> set[str]:
    linked: set[str] = set()
    for template in templates:
        process = _process(template)
        if process is None or process.id not in selected_template_ids:
            continue
        summary = _field(template, "summary", {}) or {}
        linked_ids = _field(summary, "linked_decision_model_ids", []) or []
        linked.update(value for value in linked_ids if isinstance(value, str))
        if not linked_ids:
            dmn_summaries = _field(template, "dmn_summaries", {}) or {}
            if isinstance(dmn_summaries, Mapping):
                linked.update(str(value) for value in dmn_summaries)
    return linked


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
    system = resolved.system if resolved is not None else None
    code = resolved.code if resolved is not None else None
    if not system or not code:
        lookup = find_terminology_concept(LOINC_SYSTEM, concept.concept)
        if lookup.found and lookup.system and lookup.code:
            system, code = lookup.system, lookup.code
    if not system or not code:
        return None, Finding(
            rung="L5a",
            severity="WARNING",
            code="incomplete-binding",
            message=f"concept {concept.concept!r} on task {element.id!r} could not be resolved to one code",
            element_id=element.id,
        )
    code = f"{system}|{code}"
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
    selected_template_ids = {
        ref.template_id for ref in process.acp.template_refs
    }
    linked_model_ids = _linked_decision_model_ids(templates, selected_template_ids)
    policy = validate_automation_policy(list(template_processes.values()), policy)
    policy_parameters = policy.get("parameters", {})
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

    # Some DMN outputs include an enum label followed by explanatory detail.
    # Bind the CPG-declared enum only when that label is an unambiguous prefix;
    # do not ask an LLM to copy a value the executed decision already supplies.
    dmn_audit_trail = _field(brief, "dmn_audit_trail", []) or []
    for parameter in process.acp.parameters:
        if parameter.name in bindings:
            continue
        matches = _matching_enum_outputs(
            parameter, dmn_audit_trail, linked_model_ids
        )
        enum_values = {item["enum_value"] for item in matches}
        if len(enum_values) != 1:
            continue
        selected = next(item for item in matches if item["enum_value"] in enum_values)
        audit = selected["audit"]
        model_id = _field(audit, "model_id", "unknown")
        audit_id = _field(audit, "id")
        bindings[parameter.name] = ParameterBinding(
            name=parameter.name,
            value=selected["enum_value"],
            source="plan-derived",
            note=(
                f"Matched DMN output {model_id}.{selected['output_name']} "
                "to the parameter's declared enum value."
            ),
        )
        evidence[f"ev-dmn-enum-{parameter.name}"] = Evidence(
            kind="dmn-output",
            payload={
                **({"audit_id": audit_id} if audit_id else {}),
                "model_id": model_id,
                "output_name": selected["output_name"],
                "output_value": selected["output_value"],
            },
        )

    # A rule task maps decision inputs to template parameters by name. The
    # plan already evaluated those decisions with this patient's values, so
    # every mapped input the audit trail records is a plan-derived binding.
    # The mapping is the template's own, so no name heuristics are involved.
    parameter_types = {q.name: q for q in process.acp.parameters}
    audit_inputs: dict[str, dict[str, Any]] = {}
    for audit in dmn_audit_trail:
        model_id = _field(audit, "model_id")
        inputs = _field(audit, "inputs", {}) or {}
        if isinstance(model_id, str) and isinstance(inputs, Mapping):
            audit_inputs.setdefault(model_id, dict(inputs))
    for element in process.flowElements:
        if not isinstance(element, BusinessRuleTask):
            continue
        inputs = audit_inputs.get(element.dmnModel)
        if not inputs:
            continue
        for input_name, ref in element.inputs.items():
            if not isinstance(ref, ParamRef) or ref.param in bindings or input_name not in inputs:
                continue
            parameter = parameter_types.get(ref.param)
            value = inputs[input_name]
            if parameter is None or not _value_fits_parameter(parameter, value):
                continue
            bindings[ref.param] = ParameterBinding(
                name=ref.param,
                value=value,
                source="plan-derived",
                unit=parameter.unit,
                note=f"Decision input {element.dmnModel}.{input_name} evaluated for this plan.",
            )
            evidence[f"ev-dmn-input-{ref.param}"] = Evidence(
                kind="dmn-input",
                payload={"model_id": element.dmnModel, "input_name": input_name, "value": value},
            )

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
            configured_max_duration = policy_parameters.get("max_duration")
            raw_default = (
                configured_max_duration.get("value")
                if isinstance(configured_max_duration, Mapping)
                else policy.get(
                    "environment_default",
                    os.getenv("ACP_AUTOMATION_DEFAULT_MONITORING_PERIOD", "P90D"),
                )
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
            if isinstance(configured_max_duration, Mapping):
                max_source = "policy"
                max_note = (
                    "Policy parameter 'max_duration': "
                    f"{configured_max_duration['note']}"
                )
            else:
                max_source = "plan-derived"
                max_note = "Bound from policy automation.default_monitoring_period."

    if max_duration is not None and any(p.name == "max_duration" for p in process.acp.parameters):
        bindings["max_duration"] = ParameterBinding(
            name="max_duration", value=max_duration, source=max_source, note=max_note
        )
        evidence["ev-monitoring-period"] = Evidence(
            kind="monitoring-period",
            payload={
                "value": max_duration,
                "source": max_source,
                "note": max_note,
                **({"policy_key": "max_duration"} if max_source == "policy" else {}),
            },
        )

    for parameter in process.acp.parameters:
        entry = policy_parameters.get(parameter.name)
        if (
            not parameter.required
            or parameter.default is not None
            or parameter.name in bindings
            or not isinstance(entry, Mapping)
        ):
            continue
        note = f"Policy parameter '{parameter.name}': {entry['note']}"
        binding = ParameterBinding(
            name=parameter.name,
            value=entry["value"],
            source="policy",
            unit=entry.get("unit"),
            note=note,
        )
        bindings[parameter.name] = binding
        evidence[f"ev-policy-{parameter.name}"] = Evidence(
            kind="policy-parameter",
            payload={
                "policy_key": parameter.name,
                "value": entry["value"],
                "note": entry["note"],
                **({"unit": entry["unit"]} if entry.get("unit") is not None else {}),
            },
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

def _value_fits_parameter(parameter: Parameter, value: Any) -> bool:
    """Accept an evaluated decision input only when it has the parameter's type."""
    enum = parameter.constraints.enum if parameter.constraints is not None else None
    if enum:
        return isinstance(value, str) and value in enum
    if parameter.type == "boolean":
        return isinstance(value, bool)
    if parameter.type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if parameter.type in {"decimal", "quantity"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if parameter.type == "string":
        return isinstance(value, str)
    return False
