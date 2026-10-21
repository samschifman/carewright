"""Synthesize, validate, and semantically review activity automations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

import mlflow
import requests
from fastapi import HTTPException
from cpg_contracts import content_to_text
from acp_writer.llm import get_llm
from cpg_contracts.automation.instances import ActivityAutomation, Evidence, ParameterBinding
from cpg_contracts.automation.ir import ProcessIR, TemplateRef, ir_version
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.validators.grounding import InstanceContext
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.automation.validators.results import Finding

from acp_writer.llm_json import loads_json
from acp_writer.automation_policy import load_automation_policy
from acp_writer.nodes.automation_binding import bind_instance, known_phi_values
from acp_writer.nodes.automation_selection import select_templates
from acp_writer.prompts.automation_reviewer import (
    AUTOMATION_REVIEWER_SYSTEM,
    AUTOMATION_REVIEWER_USER,
)
from acp_writer.prompts.automation_synthesizer import (
    AUTOMATION_SYNTHESIZER_SYSTEM,
    AUTOMATION_SYNTHESIZER_USER,
)
from acp_writer.planning_brief import PlanningBrief

logger = logging.getLogger(__name__)
_STATIC_REPAIR_LIMIT = 3
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:OPENAI_API_KEY|LITELLM_API_KEY|LLM_API_KEY)\b\s*[:=]\s*[\"']?[^\s,;\"']+"
)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _summary(template: Any) -> Any:
    return _field(template, "summary", {})


def _template_ir(template: Any) -> ProcessIR:
    value = _field(template, "ir")
    if isinstance(value, ProcessIR):
        return value
    return ProcessIR.model_validate(value)


def _template_version(template: Any, ir: ProcessIR) -> str:
    # Template versions are content hashes of the pinned snapshot, never model output.
    return ir_version(ir)


def _new_automation_id() -> str:
    """Return a UUID that is also safe as a BPMN process XML id."""
    while True:
        automation_id = str(uuid4())
        if automation_id[0] in "abcdef":
            return automation_id


def _safe_text(value: Any, known_values: Sequence[str]) -> Any:
    """Redact exact demographic values before they can enter a prompt."""
    if isinstance(value, str):
        for secret in sorted((v for v in known_values if len(v) >= 2), key=len, reverse=True):
            value = re.sub(re.escape(secret), "[redacted]", value, flags=re.IGNORECASE)
        return value
    if isinstance(value, Mapping):
        return {str(k): _safe_text(v, known_values) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_text(v, known_values) for v in value]
    return value


def _artifact_component(value: Any, fallback: str) -> str:
    component = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "")).strip("-")
    return component[:100] or fallback


def _artifact_response_recorder(
    output_dir: str,
    run_id: Any,
    activity_id: Any,
    known_values: Sequence[str],
) -> Callable[[str], Callable[[str, str], Callable[[str], None]]] | None:
    """Create redacted request/response writers for calls in one activity synthesis."""
    if not output_dir:
        return None
    output_path = Path(output_dir)
    root = output_path.parent.parent if output_path.parent.name == "careplans" else output_path
    directory = (
        root
        / "acp-writer"
        / _artifact_component(run_id, "run")
        / "automation"
        / _artifact_component(activity_id, "activity")
    )
    attempt = 0

    def record_role(role: str) -> Callable[[str, str], Callable[[str], None]]:
        def begin(system: str, user: str) -> Callable[[str], None]:
            nonlocal attempt
            attempt += 1
            stem = f"attempt-{attempt}"

            def write_json(suffix: str, payload: Any) -> None:
                safe_payload = _safe_text(payload, known_values)
                encoded = json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str)
                encoded = _CREDENTIAL_ASSIGNMENT_RE.sub("[REDACTED_CREDENTIAL]", encoded)
                encoded = _API_KEY_RE.sub("[REDACTED_CREDENTIAL]", encoded)
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"{stem}.{suffix}.json").write_text(encoded + "\n")

            write_json("request", {
                "role": role,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            })

            return lambda content: write_json("response", {
                "role": role,
                "content": content,
            })

        return begin

    return record_role


def _activity_facts(activity: Any, known_values: Sequence[str]) -> dict[str, Any]:
    allowed = (
        "id", "type", "description", "dose", "route", "frequency", "specialty",
        "source_recommendation_id", "source_cpg", "clinical_rationale", "workflow",
    )
    data = {name: _field(activity, name) for name in allowed if _field(activity, name) is not None}
    return _safe_text(data, known_values)


def _audit_facts(audit: Sequence[Any], activity: Any, known_values: Sequence[str]) -> list[dict[str, Any]]:
    index = _field(activity, "source_dmn_call")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(audit):
        relevant = [audit[index]]
    else:
        # Some planning paths omit source_dmn_call on an activity. Preserve the
        # complete decision context rather than presenting the reviewer with
        # no evidence about patient-category decisions.
        relevant = list(audit)
    return [
        _safe_text({
            "model_id": _field(entry, "model_id"),
            "model_name": _field(entry, "model_name"),
            "inputs": _field(entry, "inputs", {}),
            "outputs": _field(entry, "outputs", {}),
        }, known_values)
        for entry in relevant
    ]


def _prior_bindings(activity: Any, brief: Any) -> list[dict[str, Any]]:
    activity_id = _field(activity, "id")
    prior = _field(brief, "prior_planning_brief", brief)
    result: list[dict[str, Any]] = []
    for prior_activity in _field(prior, "activities", []) or []:
        if activity_id and _field(prior_activity, "id") != activity_id:
            continue
        for automation in _field(prior_activity, "automations", []) or []:
            for binding in _field(automation, "bindings", []) or []:
                if _field(binding, "source") == "clinician":
                    result.append(binding.model_dump(mode="json") if hasattr(binding, "model_dump") else dict(binding))
    return result


def _source_cpg(template_ir: ProcessIR) -> str:
    return template_ir.process.acp.provenance.source_cpg


def _instance_from_template(template: Any, automation_id: str) -> ProcessIR:
    """Construct instance structure exclusively from the pinned template snapshot."""
    template_ir = _template_ir(template)
    template_id = template_ir.process.id
    version = _template_version(template, template_ir)
    instance = template_ir.model_copy(deep=True)
    process = instance.process
    process.id = automation_id
    process.kind = "instance"
    process.acp.template_refs = [TemplateRef(
        template_id=template_id,
        version=version,
        source_cpg=_source_cpg(template_ir),
    )]
    for element in process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=template_id,
            version=version,
            source_cpg=_source_cpg(template_ir),
            element_id=element.id,
        )
    return instance


def _dmn_output_names(plan_facts: Sequence[Mapping[str, Any]]) -> set[str]:
    names: set[str] = set()
    for fact in plan_facts:
        outputs = fact.get("outputs")
        if isinstance(outputs, Mapping):
            names.update(str(name) for name in outputs)
    return names


def _model_bindings(
    response: Mapping[str, Any],
    template_ir: ProcessIR,
    plan_facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Accept only binding proposals for parameters that require plan-specific values."""
    raw_bindings = response.get("bindings", [])
    if not isinstance(raw_bindings, list):
        raise ValueError("synthesizer response bindings must be an array")
    parameters = {item.name: item for item in template_ir.process.acp.parameters}
    dmn_outputs = _dmn_output_names(plan_facts)
    allowed = {
        name
        for name, parameter in parameters.items()
        if (parameter.default is None or name in dmn_outputs) and not (
            parameter.type == "string"
            and parameter.source is not None
            and parameter.source.kind == "authored"
        )
    }
    # max_duration is reserved and intentionally has no template default.
    allowed.add("max_duration")
    normalized: list[dict[str, Any]] = []
    for item in raw_bindings:
        if not isinstance(item, Mapping):
            raise ValueError("each synthesizer binding must be an object")
        name = item.get("name")
        if not isinstance(name, str) or name not in allowed:
            # In particular, model proposals for authored strings are discarded.
            continue
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"binding {name!r} requires a non-empty reason")
        parameter = parameters.get(name)
        normalized.append({
            "name": name,
            "value": item.get("value"),
            "source": "plan-derived",
            "unit": parameter.unit if parameter is not None else None,
            "note": reason.strip(),
        })
    return normalized


def _binding_requests(
    template_ir: ProcessIR,
    plan_facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Describe only parameters for which synthesis may propose patient-plan values."""
    dmn_outputs = _dmn_output_names(plan_facts)
    requested = []
    for parameter in template_ir.process.acp.parameters:
        authored_string = (
            parameter.type == "string"
            and parameter.source is not None
            and parameter.source.kind == "authored"
        )
        if (parameter.default is not None and parameter.name not in dmn_outputs) or authored_string:
            continue
        requested.append({
            "name": parameter.name,
            "type": parameter.type,
            "unit": parameter.unit,
            "required": parameter.required,
            "description": parameter.description,
        })
    if "max_duration" not in {item["name"] for item in requested}:
        requested.append({
            "name": "max_duration",
            "type": "duration",
            "unit": None,
            "required": True,
            "description": "Reserved monitoring period; bind according to the supplied plan facts and §12 precedence.",
        })
    return requested


def _slice_findings(exc: Exception) -> list[Finding]:
    message = str(exc)
    code = "I9-not-supported" if "I9-not-supported" in message else "instance-schema"
    return [Finding(rung="L5a", severity="ERROR", code=code, message=message)]


def _is_binding_finding(finding: Finding) -> bool:
    if finding.severity != "ERROR":
        return False
    if finding.code.startswith("parameter-binding-") or finding.code == "incomplete-binding":
        return True
    return finding.rung.startswith("I") and "binding" in finding.message.casefold()


def _record_contract_fault(findings: list[Finding], errors: Sequence[Finding]) -> None:
    if not errors:
        return
    findings.append(Finding(
        rung=errors[0].rung,
        severity="ERROR",
        code="contract-fault",
        message="Non-binding instance validation error is a contract fault: "
        + _json([item.__dict__ for item in errors]),
    ))


@mlflow.trace(name="automation.llm_json_call")
def _invoke_json(
    llm: Any,
    system: str,
    user: str,
    *,
    attempt_recorder: Callable[[str, str], Callable[[str], None]] | None = None,
) -> dict[str, Any]:
    finish_recording = attempt_recorder(system, user) if attempt_recorder else None
    try:
        response = llm.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
    except Exception as exc:
        if finish_recording:
            finish_recording(json.dumps({"error_type": type(exc).__name__}))
        raise
    content = getattr(response, "content", response)
    raw_content = content_to_text(content)
    if finish_recording:
        finish_recording(raw_content)
    parsed = loads_json(raw_content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


def _context(bindings: Sequence[ParameterBinding], evidence: Mapping[str, Evidence], brief: Any, activity: Any) -> InstanceContext:
    return InstanceContext(
        bindings=list(bindings),
        evidence=dict(evidence),
        dmn_audit_trail=list(_field(brief, "dmn_audit_trail", []) or []),
        careplan_feedback=str(_field(brief, "careplan_feedback", "") or ""),
        activities=[activity.model_dump(mode="json") if hasattr(activity, "model_dump") else dict(activity)],
    )


def _validation_payload(
    ir: ProcessIR,
    templates: Sequence[ProcessIR],
    context: InstanceContext,
    *,
    dmn_namespaces: Mapping[str, str] | None = None,
    dmn_summaries: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ir": ir.model_dump(mode="json", exclude_none=True),
        "templates": [template.model_dump(mode="json", exclude_none=True) for template in templates],
        "dmn_namespaces": dict(dmn_namespaces or {}),
        "dmn_summaries": {
            model_id: (
                summary.model_dump(mode="json", exclude_none=True)
                if hasattr(summary, "model_dump")
                else summary
            )
            for model_id, summary in (dmn_summaries or {}).items()
        },
        "context": {
            "bindings": [item.model_dump(mode="json", exclude_none=True) for item in context.bindings],
            "evidence": {key: value.model_dump(mode="json", exclude_none=True) for key, value in context.evidence.items()},
            "dmn_audit_trail": context.dmn_audit_trail,
            "careplan_feedback": context.careplan_feedback,
        },
    }


@mlflow.trace(name="automation.semantic_review")
def _review(
    llm: Any,
    *,
    template: Any,
    activity: Any,
    plan_facts: Any,
    ir: ProcessIR,
    bindings: Sequence[Mapping[str, Any]],
    derivation_evidence: Mapping[str, Any],
    findings: Sequence[Finding],
    attempt_recorder: Callable[[str, str], Callable[[str], None]] | None = None,
) -> tuple[bool, str]:
    template_ir = _template_ir(template)
    response = _invoke_json(
        llm,
        AUTOMATION_REVIEWER_SYSTEM,
        AUTOMATION_REVIEWER_USER.format(
            template=_json(template_ir),
            activity=_json(activity),
            plan_facts=_json(plan_facts),
            instance=_json(ir),
            bindings=_json(bindings),
            derivation_evidence=_json(derivation_evidence),
            findings=_json([item.__dict__ for item in findings]),
        ),
        attempt_recorder=attempt_recorder,
    )
    verdict = str(response.get("verdict", "REVISE")).upper()
    issues = response.get("issues") or []
    if verdict == "APPROVE" and not issues:
        return True, ""
    return False, _json(issues or [{"description": "Reviewer did not approve the instance."}])


def _automation(
    automation_id: str,
    ir: ProcessIR,
    template: Any,
    bindings: Sequence[ParameterBinding],
    evidence: Mapping[str, Evidence],
    status: str,
    rungs_passed: Sequence[str],
    findings: Sequence[Finding],
    review_notes: Sequence[str],
    bpmn_xml: str,
) -> ActivityAutomation:
    template_ir = _template_ir(template)
    caps = sorted({
        element.taskName
        for element in ir.process.flowElements
        if getattr(element, "type", None) == "task"
    })
    warnings = [item.message for item in findings if item.severity == "WARNING"]
    errors = [item.message for item in findings if item.severity == "ERROR"]
    return ActivityAutomation(
        id=automation_id,
        template_refs=[TemplateRef(
            template_id=template_ir.process.id,
            version=_template_version(template, template_ir),
            source_cpg=_source_cpg(template_ir),
        )],
        template_snapshots=[template_ir],
        ir=ir,
        bpmn_xml=bpmn_xml,
        bindings=list(bindings),
        derivation_evidence=dict(evidence),
        capabilities_used=caps,
        validation={
            "status": status,
            "rungs_passed": list(rungs_passed),
            "warnings": warnings,
            "errors": errors,
        },
        enabled=(status == "valid"),
        review_notes=list(review_notes),
    )


@mlflow.trace(name="automation.synthesize_for_activity")
def synthesize_for_activity(
    activity: Any,
    registry: Any,
    dmn_audit_trail: Sequence[Any],
    *,
    brief: Any,
    state: Mapping[str, Any],
    llm: Any | None = None,
    validator: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    reviewer: Callable[..., tuple[bool, str]] | None = None,
    prompt_sink: dict[str, str] | None = None,
    shared: dict[str, ActivityAutomation] | None = None,
) -> list[ActivityAutomation]:
    """Build one reviewed ActivityAutomation for each selected template.

    ``shared`` maps template id to an automation already synthesized for
    another activity of the same plan. A template is instantiated once per
    plan and attached to every activity it serves (the view shows it as
    shared), instead of producing near-identical copies per activity.
    """
    selected = select_templates(activity, registry, list(dmn_audit_trail))
    if not selected:
        return []
    reused: list[ActivityAutomation] = []
    if shared is not None:
        remaining = []
        for template in selected:
            template_id = _field(_summary(template), "id", None)
            if template_id in shared:
                reused.append(shared[template_id])
            else:
                remaining.append(template)
        selected = remaining
        if not selected:
            return reused
    llm = llm or get_llm(dict(state))
    if validator is None:
        from acp_writer.services.template_client import TemplateClient

        client = TemplateClient()
        validator = client.validate
    demographics = _field(state, "patient_demographics", {}) or {}
    known_values = known_phi_values(demographics)
    activity_data = _activity_facts(activity, known_values)
    plan_facts = _audit_facts(dmn_audit_trail, activity, known_values)
    feedback = _safe_text({
        "current": _field(brief, "careplan_feedback", "") or "",
        "history": _field(brief, "careplan_review_history", []) or [],
    }, known_values)
    prior_bindings = _safe_text(_prior_bindings(activity, brief), known_values)
    outputs: list[ActivityAutomation] = []

    for template in selected:
        template_ir = _template_ir(template)
        automation_policy = load_automation_policy(
            [template_ir],
            configured=_field(state, "automation_policy", {}) or {},
        )
        template_json = _json(template_ir)
        automation_id = _new_automation_id()
        instance_base = _instance_from_template(template, automation_id)
        candidate, candidate_bindings, evidence, initial_binding_findings = bind_instance(
            instance_base,
            [template],
            activity=activity,
            brief=brief,
            policy=automation_policy,
        )
        requested_bindings_json = _json(_binding_requests(template_ir, plan_facts))
        record_attempt = _artifact_response_recorder(
            str(_field(state, "output_dir", "") or ""),
            _field(state, "run_id"),
            _field(activity, "id"),
            known_values,
        )
        final_findings: list[Finding] = list(initial_binding_findings)
        rungs: list[str] = []
        repair_feedback = ""
        static_exhausted = False
        engine_unavailable = False
        validation_xml: str | None = None

        # Initial attempt plus at most three static repair rounds.
        for attempt in range(_STATIC_REPAIR_LIMIT + 1):
            prompt = AUTOMATION_SYNTHESIZER_USER.format(
                template=template_json,
                activity=_json(activity_data),
                plan_facts=_json(plan_facts),
                feedback=_json(feedback),
                prior_bindings=_json(prior_bindings),
                requested_bindings=requested_bindings_json,
                repair_feedback=repair_feedback or "None",
            )
            if prompt_sink is not None:
                prompt_key = f"{_field(activity, 'id', 'activity')}:{_field(_summary(template), 'id', template_ir.process.id)}:synthesis:{attempt + 1}"
                prompt_sink[prompt_key] = prompt
            try:
                generated = _invoke_json(
                    llm,
                    AUTOMATION_SYNTHESIZER_SYSTEM,
                    prompt,
                    attempt_recorder=record_attempt("synthesizer") if record_attempt else None,
                )
                raw_bindings = _model_bindings(generated, template_ir, plan_facts)
            except Exception as exc:
                final_findings = _slice_findings(exc)
                _record_contract_fault(
                    final_findings,
                    [item for item in final_findings if item.severity == "ERROR"],
                )
                static_exhausted = True
                break

            candidate, candidate_bindings, evidence, binding_findings = bind_instance(
                instance_base,
                [template],
                activity=activity,
                brief=brief,
                policy=automation_policy,
                initial_bindings=raw_bindings,
            )
            context = _context(candidate_bindings, evidence, brief, activity)
            dmn_namespaces = _field(template, "dmn_namespaces", {}) or {}
            dmn_summaries = _field(template, "dmn_summaries", {}) or {}
            ladder = run_static_ladder(
                candidate,
                catalog=load_catalog(),
                stage="instance",
                templates=[template_ir],
                context=context,
                dmn_namespaces=dmn_namespaces,
                dmn_summaries=dmn_summaries,
                phi_known_values=known_values,
            )
            final_findings = [*binding_findings, *ladder.findings]
            rungs = list(ladder.rungs_passed)
            validation_xml = None
            try:
                engine = validator(_validation_payload(
                    candidate,
                    [template_ir],
                    context,
                    dmn_namespaces=dmn_namespaces,
                    dmn_summaries=dmn_summaries,
                ))
                returned_xml = engine.get("bpmn_xml")
                if isinstance(returned_xml, str) and returned_xml:
                    validation_xml = returned_xml
                elif validation_xml is None and engine.get("valid"):
                    raise ValueError("automation validation response omitted compiled bpmn_xml")
            except requests.RequestException as exc:
                final_findings.append(Finding(
                    rung="L6", severity="WARNING", code="engine-unavailable",
                    message=f"Kogito validation unavailable: {exc}",
                ))
                engine_unavailable = True
                static_exhausted = True
                break
            except HTTPException as exc:
                if not 500 <= exc.status_code < 600:
                    raise
                detail = exc.detail
                if isinstance(detail, Mapping):
                    returned_xml = detail.get("bpmn_xml")
                    if isinstance(returned_xml, str) and returned_xml:
                        validation_xml = returned_xml
                    detail = detail.get("message") or detail
                final_findings.append(Finding(
                    rung="L6", severity="WARNING", code="engine-unavailable",
                    message=f"Kogito validation unavailable: {detail}",
                ))
                engine_unavailable = True
                static_exhausted = True
                break

            errors = [item for item in final_findings if item.severity == "ERROR"]
            binding_errors = [item for item in errors if _is_binding_finding(item)]
            contract_errors = [item for item in errors if not _is_binding_finding(item)]
            if contract_errors:
                _record_contract_fault(final_findings, contract_errors)
                static_exhausted = True
                break
            if binding_errors:
                if attempt == _STATIC_REPAIR_LIMIT:
                    static_exhausted = True
                    break
                repair_feedback = _json([item.__dict__ for item in binding_errors])
                continue

            if engine.get("unavailable"):
                messages = engine.get("messages") or []
                message = _json(messages or "Kogito validation unavailable")
                final_findings.append(Finding(
                    rung="L6", severity="WARNING", code="engine-unavailable",
                    message=message,
                ))
                engine_unavailable = True
                static_exhausted = True
                break
            if engine.get("valid"):
                if "L6" not in rungs:
                    rungs.append("L6")
                final_findings.extend(
                    Finding(**item) for item in engine.get("findings", [])
                    if isinstance(item, Mapping) and item.get("code")
                )
                break

            final_findings.append(Finding(
                rung="L6", severity="ERROR", code="kogito-validation",
                message=_json(engine.get("errors") or engine.get("messages") or "Kogito rejected BPMN"),
            ))
            _record_contract_fault(
                final_findings,
                [item for item in final_findings if item.severity == "ERROR" and not _is_binding_finding(item)],
            )
            static_exhausted = True
            break

        has_incomplete = any(item.code == "incomplete-binding" for item in final_findings)
        status = "escalated" if engine_unavailable or static_exhausted else (
            "incomplete" if has_incomplete else "valid"
        )
        review_notes: list[str] = []

        if status == "valid":
            try:
                if reviewer is None:
                    approved, review_feedback = _review(
                        llm,
                        template=template,
                        activity=activity_data,
                        plan_facts=plan_facts,
                        ir=candidate,
                        bindings=_safe_text(
                            [item.model_dump(mode="json", exclude_none=True) for item in candidate_bindings],
                            known_values,
                        ),
                        derivation_evidence=_safe_text(
                            {key: value.model_dump(mode="json", exclude_none=True) for key, value in evidence.items()},
                            known_values,
                        ),
                        findings=final_findings,
                        attempt_recorder=record_attempt("reviewer") if record_attempt else None,
                    )
                else:
                    approved, review_feedback = reviewer(
                        template=template,
                        activity=activity_data,
                        plan_facts=plan_facts,
                        ir=candidate,
                        findings=final_findings,
                    )
            except Exception as exc:
                approved = False
                review_feedback = f"Semantic reviewer failed: {exc}"
            if approved:
                if "L7" not in rungs:
                    rungs.append("L7")
            else:
                review_error = Finding(
                    rung="L7", severity="ERROR", code="semantic-review",
                    message=review_feedback or "Semantic reviewer did not approve the instance.",
                )
                final_findings.append(review_error)
                _record_contract_fault(final_findings, [review_error])
                status = "escalated"
                review_notes.append("Semantic reviewer rejection is escalated without an unclassified repair call.")

        if has_incomplete and status == "incomplete":
            review_notes.append("Required plan-specific values are unresolved; bind them before enabling.")
        # If the validation service is unreachable, retain the registry's
        # namespace-aware template artifact for the disabled escalated record.
        compiled_xml = validation_xml or _field(template, "bpmn_xml", "")
        outputs.append(_automation(
            automation_id,
            candidate,
            template,
            candidate_bindings,
            evidence,
            status,
            rungs,
            final_findings,
            review_notes,
            compiled_xml,
        ))
        if shared is not None:
            template_id = _field(_summary(template), "id", None)
            if template_id:
                shared.setdefault(template_id, outputs[-1])
    return reused + outputs


@mlflow.trace(name="automation.synthesize_brief")
def synthesize_automations_for_brief(
    state: Mapping[str, Any],
    *,
    registry: Any,
    validator: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    llm: Any | None = None,
) -> dict[str, Any]:
    """Attach selected, validated automations to every stable brief activity."""
    logger.info("── Automation Synthesizer ──")
    brief_data = _field(state, "planning_brief", {}) or {}
    try:
        brief = PlanningBrief.model_validate(brief_data)
    except Exception as exc:
        logger.warning("Skipping automation synthesis for invalid planning brief: %s", exc)
        return {"automation_synthesis_error": str(exc)}

    audit_trail = list(brief.dmn_audit_trail or _field(state, "dmn_results", []) or [])
    from acp_writer.services.ai_transparency import prompts_capture_enabled

    prompt_sink: dict[str, str] | None = {} if prompts_capture_enabled() else None
    warnings: list[str] = []
    brief_context = brief.model_dump(mode="json", exclude_none=True)
    brief_context["prior_planning_brief"] = _field(state, "prior_planning_brief", {}) or {}
    brief_context["careplan_feedback"] = _field(state, "careplan_feedback", "") or ""
    brief_context["careplan_review_history"] = _field(state, "careplan_review_history", []) or []
    brief_context["dmn_audit_trail"] = [
        item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else item
        for item in audit_trail
    ]

    shared: dict[str, ActivityAutomation] = {}
    for activity in brief.activities:
        try:
            activity.automations = synthesize_for_activity(
                activity,
                registry,
                audit_trail,
                brief=brief_context,
                state=state,
                llm=llm,
                validator=validator,
                prompt_sink=prompt_sink,
                shared=shared,
            )
        except Exception as exc:
            logger.exception("Automation synthesis failed for activity %s", activity.id)
            warnings.append(f"Automation synthesis failed for activity {activity.id}: {exc}")

    result = {
        "planning_brief": brief.model_dump(mode="json", exclude_none=True),
        "automation_prompts": prompt_sink or {},
    }
    if warnings:
        brief.review_status = "flagged"
        existing = brief.review_feedback or ""
        brief.review_feedback = "\n".join(part for part in (existing, *warnings) if part)
        result["automation_synthesis_warnings"] = warnings
        result["planning_brief"] = brief.model_dump(mode="json", exclude_none=True)
    return result


__all__ = ["synthesize_for_activity", "synthesize_automations_for_brief"]
