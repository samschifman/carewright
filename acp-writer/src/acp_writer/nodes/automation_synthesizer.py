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
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.instances import ActivityAutomation, Evidence, ParameterBinding
from cpg_contracts.automation.ir import ProcessIR, TemplateRef, ir_version
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.validators.grounding import InstanceContext
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.automation.validators.results import Finding

from acp_writer.llm_json import loads_json
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
_REVIEW_REPAIR_LIMIT = 2
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
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(audit):
        return []
    entry = audit[index]
    return [_safe_text({
        "model_id": _field(entry, "model_id"),
        "model_name": _field(entry, "model_name"),
        "outputs": _field(entry, "outputs", {}),
    }, known_values)]


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


def _keep_instance_provenance(element: Any) -> None:
    provenance = _field(_field(element, "acp"), "provenance")
    allowed = {
        "fragment-boundary", "fragment-merge", "exclusive-join",
        "start-event", "end-event", "parameter-binding", "plan-bound",
    }
    if _field(provenance, "kind") == "structural" and _field(provenance, "derivation_rule") not in allowed:
        element.acp.provenance = None


def _instance_from_template(template: Any) -> ProcessIR:
    """Conservative single-template fallback retained as a disabled artifact."""
    template_ir = _template_ir(template)
    summary = _summary(template)
    template_id = template_ir.process.id
    version = _template_version(template, template_ir)
    instance = template_ir.model_copy(deep=True)
    process = instance.process
    process.id = f"instance_{uuid4().hex}"
    process.kind = "instance"
    process.acp.template_refs = [TemplateRef(
        template_id=template_id,
        version=version,
        source_cpg=_source_cpg(template_ir),
    )]
    process.acp.provenance.template_snapshots = [template_ir.process.model_copy(deep=True)]
    for element in process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=template_id,
            version=version,
            source_cpg=_source_cpg(template_ir),
            element_id=element.id,
        )
        _keep_instance_provenance(element)
    return instance


def _normalize_instance_metadata(
    raw_ir: Mapping[str, Any],
    template: Any,
    process_id: str,
) -> ProcessIR:
    """Overwrite model-authored instance metadata from the selected snapshot."""
    result = json.loads(json.dumps(raw_ir))
    template_ir = _template_ir(template)
    template_id = template_ir.process.id
    version = ir_version(template_ir)
    process = result.get("process")
    if not isinstance(process, dict):
        raise ValueError("synthesized IR must contain a process object")
    process["id"] = process_id
    process["kind"] = "instance"
    acp = process.setdefault("acp", {})
    if not isinstance(acp, dict):
        raise ValueError("synthesized process acp must be an object")
    provenance = acp.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    provenance["source_cpg"] = _source_cpg(template_ir)
    acp["provenance"] = provenance
    acp["template_refs"] = [{"template_id": template_id, "version": version}]

    if acp.get("pruned") or acp.get("consolidated_boundaries") or acp.get("unify"):
        raise ValueError("I9-not-supported: pruning, boundary consolidation, and fragment records are deferred")
    acp["provenance"]["template_snapshots"] = [
        template_ir.process.model_dump(mode="json", exclude_none=True)
    ]
    snapshots = {template_id: template_ir}
    for element in process.get("flowElements", []):
        if not isinstance(element, dict):
            continue
        element_acp = element.get("acp")
        if not isinstance(element_acp, dict):
            continue
        ref = element_acp.get("template_ref")
        if not isinstance(ref, dict):
            continue
        snapshot = snapshots.get(ref.get("template_id"))
        if snapshot is not None:
            ref["version"] = ir_version(snapshot)
            ref["source_cpg"] = _source_cpg(snapshot)

    instance = ProcessIR.model_validate(result)
    for element in instance.process.flowElements:
        _keep_instance_provenance(element)
    return instance


def _slice_findings(exc: Exception) -> list[Finding]:
    message = str(exc)
    code = "I9-not-supported" if "I9-not-supported" in message else "instance-schema"
    return [Finding(rung="L5a", severity="ERROR", code=code, message=message)]


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


def _validation_payload(ir: ProcessIR, templates: Sequence[ProcessIR], context: InstanceContext) -> dict[str, Any]:
    return {
        "ir": ir.model_dump(mode="json", exclude_none=True),
        "templates": [template.model_dump(mode="json", exclude_none=True) for template in templates],
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
        bpmn_xml=compile(ir),
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
) -> list[ActivityAutomation]:
    """Build one reviewed ActivityAutomation for each selected template."""
    selected = select_templates(activity, registry, list(dmn_audit_trail))
    if not selected:
        return []
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
        template_json = _json(template_ir)
        automation_id = _new_automation_id()
        record_attempt = _artifact_response_recorder(
            str(_field(state, "output_dir", "") or ""),
            _field(state, "run_id"),
            _field(activity, "id"),
            known_values,
        )
        candidate: ProcessIR | None = None
        candidate_bindings: list[ParameterBinding] = []
        evidence: dict[str, Evidence] = {}
        final_findings: list[Finding] = []
        rungs: list[str] = []
        repair_feedback = ""
        static_exhausted = False
        engine_unavailable = False

        # Initial attempt plus at most three static repair rounds.
        for attempt in range(_STATIC_REPAIR_LIMIT + 1):
            prompt = AUTOMATION_SYNTHESIZER_USER.format(
                template=template_json,
                activity=_json(activity_data),
                plan_facts=_json(plan_facts),
                feedback=_json(feedback),
                prior_bindings=_json(prior_bindings),
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
                raw_ir = generated.get("ir", generated)
                candidate = _normalize_instance_metadata(raw_ir, template, automation_id)
                raw_bindings = generated.get("bindings", [])
            except Exception as exc:
                final_findings = _slice_findings(exc)
                repair_feedback = _json([item.__dict__ for item in final_findings])
                if attempt == _STATIC_REPAIR_LIMIT:
                    static_exhausted = True
                continue

            candidate, candidate_bindings, evidence, binding_findings = bind_instance(
                candidate,
                [template_ir],
                activity=activity,
                brief=brief,
                policy=_field(state, "automation_policy", {}) or {},
                initial_bindings=raw_bindings,
            )
            context = _context(candidate_bindings, evidence, brief, activity)
            ladder = run_static_ladder(
                candidate,
                catalog=load_catalog(),
                stage="instance",
                templates=[template_ir],
                context=context,
                phi_known_values=known_values,
            )
            final_findings = [*binding_findings, *ladder.findings]
            rungs = list(ladder.rungs_passed)
            if ladder.ok:
                try:
                    engine = validator(_validation_payload(candidate, [template_ir], context))
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
                    final_findings.append(Finding(
                        rung="L6", severity="WARNING", code="engine-unavailable",
                        message=f"Kogito validation unavailable: {exc.detail}",
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
            if attempt == _STATIC_REPAIR_LIMIT:
                static_exhausted = True
            repair_feedback = _json([item.__dict__ for item in final_findings])

        if candidate is None:
            candidate = _instance_from_template(template)
            candidate.process.id = automation_id
            candidate, candidate_bindings, evidence, binding_findings = bind_instance(
                candidate,
                [template_ir],
                activity=activity,
                brief=brief,
                policy=_field(state, "automation_policy", {}) or {},
            )
            final_findings.extend(binding_findings)
            static_exhausted = True

        has_incomplete = any(item.code == "incomplete-binding" for item in final_findings)
        status = (
            "escalated" if engine_unavailable
            else ("incomplete" if has_incomplete else ("escalated" if static_exhausted else "valid"))
        )
        review_notes: list[str] = []

        if status == "valid":
            approved = False
            review_feedback = ""
            for review_attempt in range(_REVIEW_REPAIR_LIMIT + 1):
                try:
                    if reviewer is None:
                        approved, review_feedback = _review(
                            llm,
                            template=template,
                            activity=activity_data,
                            plan_facts=plan_facts,
                            ir=candidate,
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
                    break
                if review_attempt == _REVIEW_REPAIR_LIMIT:
                    status = "escalated"
                    final_findings.append(Finding(
                        rung="L7", severity="ERROR", code="semantic-review",
                        message=review_feedback or "Semantic reviewer did not approve the instance.",
                    ))
                    review_notes.append("Semantic reviewer did not approve within the repair budget.")
                    break
                repair_feedback = f"L7 reviewer requested repair: {review_feedback}"
                try:
                    repair_prompt = AUTOMATION_SYNTHESIZER_USER.format(
                        template=template_json,
                        activity=_json(activity_data),
                        plan_facts=_json(plan_facts),
                        feedback=_json(feedback),
                        prior_bindings=_json(prior_bindings),
                        repair_feedback=repair_feedback,
                    )
                    if prompt_sink is not None:
                        prompt_key = f"{_field(activity, 'id', 'activity')}:{_field(_summary(template), 'id', template_ir.process.id)}:synthesis:semantic-{review_attempt + 1}"
                        prompt_sink[prompt_key] = repair_prompt
                    generated = _invoke_json(
                        llm,
                        AUTOMATION_SYNTHESIZER_SYSTEM,
                        repair_prompt,
                        attempt_recorder=record_attempt("synthesizer") if record_attempt else None,
                    )
                    candidate = _normalize_instance_metadata(
                        generated.get("ir", generated), template, automation_id
                    )
                    candidate, candidate_bindings, evidence, binding_findings = bind_instance(
                        candidate,
                        [template_ir],
                        activity=activity,
                        brief=brief,
                        policy=_field(state, "automation_policy", {}) or {},
                        initial_bindings=generated.get("bindings", []),
                    )
                    context = _context(candidate_bindings, evidence, brief, activity)
                    ladder = run_static_ladder(
                        candidate,
                        catalog=load_catalog(),
                        stage="instance",
                        templates=[template_ir],
                        context=context,
                        phi_known_values=known_values,
                    )
                    final_findings = [*binding_findings, *ladder.findings]
                    rungs = list(ladder.rungs_passed)
                    if not ladder.ok:
                        review_feedback = _json([item.__dict__ for item in final_findings])
                        continue
                    engine = validator(_validation_payload(candidate, [template_ir], context))
                    if not engine.get("valid"):
                        review_feedback = _json(engine.get("errors") or engine.get("messages") or "Kogito validation failed")
                        continue
                    rungs.append("L6")
                except Exception as exc:
                    review_feedback = str(exc)
                    continue

        if has_incomplete and status == "incomplete":
            review_notes.append("Required plan-specific values are unresolved; bind them before enabling.")
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
        ))
    return outputs


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
