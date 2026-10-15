"""Plan Composer — core clinical reasoning node.

LLM maps DMN results + recommendations to a PlanningBrief.
Assigns FHIR codes via terminology lookup, populates workflow
context for future BPMN generation.
"""

import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import mlflow
from cpg_contracts import content_to_text

from acp_writer.llm import get_llm
from acp_writer.llm_json import loads_json
from acp_writer.output import write_artifact
from acp_writer.planning_brief import (
    PlanningBrief,
    coerce_conflicts,
    normalize_review_history,
    render_clinician_directives,
    render_feedback_history,
    stamp_activity_ids,
)
from acp_writer.prompts.plan_composer import (
    PLAN_COMPOSER_USER,
    compose_system_prompt,
)
from acp_writer.state import CarePlanComposerState
from acp_writer.tools.concept_resolver import resolve

logger = logging.getLogger(__name__)

_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:OPENAI_API_KEY|LITELLM_API_KEY|LLM_API_KEY)\b\s*[:=]\s*[\"']?[^\s,;\"']+"
)
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_SENSITIVE_FIELD_RE = re.compile(r"(?i)(?:api[_-]?key|secret|token|password|credential)")


def _artifact_component(value: Any, fallback: str) -> str:
    component = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "")).strip("-")
    return component[:100] or fallback


def _patient_values(demographics: Mapping[str, Any], patient_reference: str) -> list[str]:
    values: list[str] = [patient_reference] if patient_reference else []
    if not isinstance(demographics, Mapping):
        return values

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(value, Mapping):
            for child in value.values():
                add(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                add(child)

    for key in ("name", "names", "identifiers", "birth_date", "birthDate"):
        add(demographics.get(key))
    return list(dict.fromkeys(values))


def _sanitize_artifact_value(value: Any, patient_values: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED_CREDENTIAL]" if _SENSITIVE_FIELD_RE.search(str(key))
            else _sanitize_artifact_value(child, patient_values)
            for key, child in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_sanitize_artifact_value(child, patient_values) for child in value]
    if not isinstance(value, str):
        return value

    sanitized = value
    for name in ("OPENAI_API_KEY", "LITELLM_API_KEY", "LLM_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED_CREDENTIAL]")
    sanitized = _CREDENTIAL_ASSIGNMENT_RE.sub("[REDACTED_CREDENTIAL]", sanitized)
    sanitized = _API_KEY_RE.sub("[REDACTED_CREDENTIAL]", sanitized)
    for patient_value in sorted((v for v in patient_values if len(v) >= 2), key=len, reverse=True):
        sanitized = re.sub(re.escape(patient_value), "[redacted]", sanitized, flags=re.IGNORECASE)
    return sanitized


def _record_composer_round(
    output_dir: str,
    run_id: Any,
    round_number: int,
    request: dict[str, Any],
    response: Any | None,
    patient_values: Sequence[str],
) -> None:
    if not output_dir:
        return
    output_path = Path(output_dir)
    root = output_path.parent.parent if output_path.parent.name == "careplans" else output_path
    directory = root / "acp-writer" / _artifact_component(run_id, "run") / "plan-composer"
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"round-{round_number}"
    safe_request = _sanitize_artifact_value(request, patient_values)
    (directory / f"{stem}.request.json").write_text(
        json.dumps(safe_request, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    if response is not None:
        safe_response = _sanitize_artifact_value(response, patient_values)
        (directory / f"{stem}.response.json").write_text(
            json.dumps(safe_response, ensure_ascii=False, indent=2, default=str) + "\n"
        )


def _offending_fragment(brief_data: Any, raw_response: str) -> str:
    if isinstance(brief_data, Mapping):
        for field in ("goals", "activities"):
            items = brief_data.get(field)
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, Mapping):
                        return json.dumps(item, ensure_ascii=False, default=str)
    return raw_response

def _source_recommendation_ids(item: dict) -> list[str]:
    values = [item.get("source_recommendation_id"), *(item.get("source_recommendation_ids") or [])]
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))


@mlflow.trace(name="resolve_missing_medication_codes")
def _resolve_missing_medication_codes(brief_data: dict[str, Any]) -> None:
    """Resolve missing medication coding through the shared concept path."""
    for activity in brief_data.get("activities") or []:
        if activity.get("type") != "medication" or activity.get("code"):
            continue
        concept = resolve(str(activity.get("description") or ""))
        if (
            concept is None
            or concept.action != "extract_medication"
            or not concept.system
            or not concept.code
        ):
            continue
        activity["code"] = {"system": concept.system, "code": concept.code}


def _merge_same_cpg_medication_regimens(brief_data: dict[str, Any]) -> dict[tuple[str, str, str, str, str], list[str]]:
    """Collapse duplicate same-CPG rows that prescribe the same regimen.

    The returned keys map each regimen to all source recommendation ids so
    related goals can be consolidated without losing provenance.
    """
    activities = brief_data.get("activities") or []
    groups: dict[tuple[str, str, str, str, str], list[dict]] = {}
    first_indices: dict[tuple[str, str, str, str, str], int] = {}
    activity_keys: dict[int, tuple[str, str, str, str, str]] = {}
    for index, activity in enumerate(activities):
        if activity.get("type") != "medication" or not activity.get("code"):
            continue
        code = activity["code"]
        key = (
            str(activity.get("source_cpg") or ""),
            str(code.get("system") or ""),
            str(code.get("code") or ""),
            re.sub(r"\s+", "", str(activity.get("dose") or "")).lower(),
            "|".join(
                re.sub(r"\s+", " ", str(activity.get(field) or "")).strip().lower()
                for field in ("route", "frequency")
            ),
        )
        groups.setdefault(key, []).append(activity)
        first_indices.setdefault(key, index)
        activity_keys[index] = key

    rec_groups: dict[tuple[str, str, str, str, str], list[str]] = {}
    merged: list[dict] = []
    merged_items: dict[tuple[str, str, str, str, str], dict] = {}
    for key, group in groups.items():
        rec_ids = list(dict.fromkeys(
            rec_id for activity in group for rec_id in _source_recommendation_ids(activity)
        ))
        rec_groups[key] = rec_ids
        primary = dict(group[0])
        primary["source_recommendation_ids"] = rec_ids
        if rec_ids:
            primary["source_recommendation_id"] = rec_ids[0]
        if len(group) > 1:
            conditions = list(dict.fromkeys(
                str(activity.get("description") or "").strip().rstrip(".")
                for activity in group
                if activity.get("description")
            ))
            code = primary["code"]
            regimen = " ".join(
                part for part in [
                    code.get("display"),
                    primary.get("dose"),
                    primary.get("route"),
                    primary.get("frequency"),
                ] if part
            )
            primary["description"] = (
                f"Use one {regimen} regimen when either applicable same-CPG decision condition is met: "
                + " OR ".join(f"({condition})" for condition in conditions)
                + "."
            )
            rationales = list(dict.fromkeys(
                str(activity.get("clinical_rationale") or "").strip()
                for activity in group
                if activity.get("clinical_rationale")
            ))
            if rationales:
                primary["clinical_rationale"] = " ".join(rationales)
            workflows = [activity.get("workflow") or {} for activity in group]
            for field in ("escalation", "monitoring_trigger"):
                values = list(dict.fromkeys(
                    str(workflow.get(field) or "").strip()
                    for workflow in workflows
                    if workflow.get(field)
                ))
                if values:
                    primary.setdefault("workflow", {})[field] = " ".join(values)
        merged_items[key] = primary
    for index, activity in enumerate(activities):
        key = activity_keys.get(index)
        if key is None:
            merged.append(activity)
        elif first_indices[key] == index:
            merged.append(merged_items[key])
    brief_data["activities"] = merged
    return rec_groups


def _merge_goals_for_merged_regimens(
    brief_data: dict[str, Any],
    medication_groups: dict[tuple[str, str, str, str, str], list[str]],
) -> None:
    merged_rec_sets = [set(ids) for ids in medication_groups.values() if len(ids) > 1]
    if not merged_rec_sets:
        return
    goals = brief_data.get("goals") or []
    consumed: set[int] = set()
    merged_goals: list[dict] = []
    for rec_ids in merged_rec_sets:
        matching = [
            (index, goal) for index, goal in enumerate(goals)
            if rec_ids.intersection(_source_recommendation_ids(goal))
        ]
        if len(matching) < 2:
            continue
        first_index, first = matching[0]
        result = dict(first)
        all_rec_ids = list(dict.fromkeys(
            rec_id for _, goal in matching for rec_id in _source_recommendation_ids(goal)
        ))
        result["source_recommendation_ids"] = all_rec_ids
        if all_rec_ids:
            result["source_recommendation_id"] = all_rec_ids[0]
        descriptions = list(dict.fromkeys(
            str(goal.get("description") or "").strip().rstrip(".")
            for _, goal in matching if goal.get("description")
        ))
        result["description"] = "One medication initiation goal covers the applicable criteria: " + " OR ".join(
            f"({description})" for description in descriptions
        ) + "."
        consumed.update(index for index, _ in matching)
        merged_goals.append(result)
    if consumed:
        brief_data["goals"] = [goal for i, goal in enumerate(goals) if i not in consumed] + merged_goals


def _neutralize_unprovided_pronouns(
    brief_data: dict[str, Any], patient_demographics: dict[str, Any]
) -> None:
    """Use neutral patient wording unless explicit pronouns were supplied."""
    if patient_demographics.get("pronouns"):
        return

    def normalize(value: str) -> str:
        value = re.sub(r"\bhimself\b", "the patient", value, flags=re.IGNORECASE)
        value = re.sub(r"\bhis\b", "the patient's", value, flags=re.IGNORECASE)
        return re.sub(r"\b(?:he|him)\b", "the patient", value, flags=re.IGNORECASE)

    for key in ("goals", "activities"):
        for item in brief_data.get(key) or []:
            for field in ("description", "clinical_rationale"):
                value = item.get(field)
                if isinstance(value, str):
                    item[field] = normalize(value)
            workflow = item.get("workflow")
            if isinstance(workflow, dict):
                for field, value in workflow.items():
                    if isinstance(value, str):
                        workflow[field] = normalize(value)


def _format_conditions(condition_codes: list[dict]) -> str:
    if not condition_codes:
        return "No conditions identified."
    lines = []
    for c in condition_codes:
        display = c.get("display", c.get("code", "unknown"))
        system = c.get("system", "")
        code = c.get("code", "")
        lines.append(f"- {display} ({system}|{code})")
    return "\n".join(lines)


def _format_dmn_results(dmn_results: list[dict]) -> str:
    if not dmn_results:
        return "No DMN decisions evaluated."
    return json.dumps(dmn_results, indent=2, default=str)


def _format_recommendations(recommendations: list[dict]) -> str:
    if not recommendations:
        return "No recommendations retrieved."
    summary = []
    for rec in recommendations:
        cert = rec.get("certainty", {}) or {}
        strength = cert.get("strength", "ungraded") if cert else "ungraded"
        summary.append({
            "id": rec.get("id"),
            "title": rec.get("title"),
            "content": rec.get("content"),
            "type": rec.get("recommendation_type"),
            "strength": strength,
            "source_cpg": rec.get("source_cpg"),
            "remarks": rec.get("remarks"),
        })
    return json.dumps(summary, indent=2)


def _format_demographics(demographics: dict) -> str:
    if not demographics:
        return "Unknown"
    parts = []
    if demographics.get("name"):
        parts.append(demographics["name"])
    if demographics.get("gender"):
        parts.append(demographics["gender"])
    if demographics.get("birth_date"):
        parts.append(f"DOB: {demographics['birth_date']}")
    return ", ".join(parts) if parts else "Unknown"


def _sanitize_provenance(brief_data: dict, default_cpg: str) -> None:
    """Default null/missing source_cpg on goals & activities to the run's CPG.

    source_cpg is a required string on PlanGoal / PlanActivity, but the LLM
    routinely emits ``source_cpg: null`` for lifestyle/general items that aren't
    tied to a specific recommendation. A single null makes the whole
    PlanningBrief fail validation, so plan_composer falls back to an EMPTY plan
    (0 goals / 0 activities) — which surfaces as a blank care plan, especially
    on a request_changes regeneration. Coerce nulls to the applicable CPG so
    provenance stays meaningful and the item survives validation.
    """
    for key in ("goals", "activities"):
        for item in brief_data.get(key) or []:
            if isinstance(item, dict) and not item.get("source_cpg"):
                item["source_cpg"] = default_cpg


def _format_base_plan(base_goals: list[dict], base_activities: list[dict]) -> str:
    """Render the revision base's goals + activities (F17a/F18b). Returns "" in
    authoring mode (no base) so the user prompt is unchanged. On the first loop
    iteration the base is the clinician-reviewed prior brief; on later
    iterations it is the LATEST DRAFT, so internal-reviewer fixes build on the
    directed changes already applied instead of reverting to the prior plan.
    Conflicts are intentionally NOT rendered here — they reach the composer
    through the "Clinician-directed changes" section (render_clinician_directives).
    """
    if not base_goals and not base_activities:
        return ""
    return (
        "\n## Care Plan Base (authoritative — revise minimally)\n"
        "This is the current plan under revision. Reproduce it and apply ONLY "
        "the changes the Clinician-directed changes section and the Reviewer "
        "Feedback require; do not add, re-word, or re-code untouched items.\n\n"
        "### Base Goals\n"
        f"{json.dumps(base_goals, indent=2, default=str)}\n\n"
        "### Base Activities\n"
        f"{json.dumps(base_activities, indent=2, default=str)}\n"
    )


def _parse_brief_from_response(content: str) -> dict[str, Any]:
    """Extract JSON from LLM response, handling markdown code blocks."""
    return loads_json(content)


@mlflow.trace(name="plan_composer")
def plan_composer(state: CarePlanComposerState) -> dict:
    """Map decisions + recommendations → PlanningBrief."""
    patient_ref = state.get("patient_reference", "Patient/unknown")
    demographics = state.get("patient_demographics", {})
    condition_codes = state.get("condition_codes", [])
    dmn_results = state.get("dmn_results", [])
    recommendations = state.get("recommendations", [])
    applicable_cpgs = state.get("applicable_cpgs", [])
    feedback = state.get("brief_review_feedback", "")
    output_dir = state.get("output_dir", "")

    # Revision mode (F17a): a prior brief means request-changes, except that a
    # parse/normalization retry must re-author from its feedback. The monolith
    # never sets this key, so it always authors.
    prior_brief = state.get("prior_planning_brief") or {}
    is_revision = bool(
        (prior_brief.get("goals") or prior_brief.get("activities"))
        and not state.get("brief_parse_failed")
    )

    # Evolving base (F18b): iteration 1 revises the clinician-reviewed prior
    # brief; once the loop has produced a draft (state["planning_brief"]), later
    # iterations revise THAT draft — otherwise every internal-reviewer round
    # would rebuild from the prior brief and revert the clinician-directed
    # changes applied in earlier iterations.
    draft_brief = state.get("planning_brief") or {}
    base_brief = draft_brief if (is_revision and (draft_brief.get("goals") or draft_brief.get("activities"))) else prior_brief
    base_goals = base_brief.get("goals") or []
    base_activities = base_brief.get("activities") or []

    # Durable clinician channel (F18a): rendered EVERY iteration straight from
    # state — never via brief_review_feedback, which the internal reviewer
    # overwrites each round (the F18 bug). Carries the clinician's instruction,
    # the prior conflicts with suggestions, and any F18c enforcement note.
    clinician_directives = ""
    if is_revision:
        clinician_directives = render_clinician_directives(
            prior_brief.get("conflicts"),
            comment=state.get("careplan_feedback", ""),
            enforcement_note=state.get("directive_enforcement_note", ""),
        )
        if clinician_directives:
            clinician_directives = f"\n{clinician_directives}\n"

    # Accumulated clinician feedback across the review loop (F17b). Rendered
    # oldest-first so standing constraints from earlier rounds don't expire, and
    # recorded on the brief for the audit trail / AI-InputPrompt DocRef.
    review_history = state.get("careplan_review_history") or []

    cpg_ids = [c.get("cpg_id", c) if isinstance(c, dict) else c for c in applicable_cpgs]

    if not cpg_ids and not recommendations:
        logger.warning("No applicable CPGs or recommendations — cannot compose a care plan")
        brief_dict = {
            "patient_reference": patient_ref,
            "applicable_cpgs": [],
            "dmn_audit_trail": [],
            "goals": [],
            "activities": [],
            "conflicts": [],
            "review_status": "flagged",
            "review_feedback": "No clinical practice guidelines matched this patient's conditions. "
                "Register applicable guidelines and ingest recommendations before generating a care plan.",
        }
        if output_dir:
            write_artifact(output_dir, "planning-brief.json", brief_dict)
        return {"planning_brief": brief_dict, "brief_review_feedback": ""}

    feedback_text = ""
    if feedback:
        feedback_text = f"\n## Reviewer Feedback (address these issues)\n{feedback}"

    history_block = render_feedback_history(review_history)
    feedback_history_text = f"\n{history_block}\n" if history_block else ""

    user_prompt = PLAN_COMPOSER_USER.format(
        patient_reference=patient_ref,
        demographics=_format_demographics(demographics),
        conditions=_format_conditions(condition_codes),
        dmn_results=_format_dmn_results(dmn_results),
        recommendations=_format_recommendations(recommendations),
        prior_plan=_format_base_plan(base_goals, base_activities),
        clinician_directives=clinician_directives,
        feedback_history=feedback_history_text,
        applicable_cpgs=json.dumps(cpg_ids),
        feedback=feedback_text,
    )

    review_round = state.get("brief_review_count", 0)
    logger.info(
        "── Plan Composer (round %d, %s mode) ──",
        review_round + 1,
        "revision" if is_revision else "authoring",
    )

    llm = get_llm(state)
    logger.info("Calling LLM...")
    t0 = time.time()

    messages = [
        {"role": "system", "content": compose_system_prompt(is_revision)},
        {"role": "user", "content": user_prompt},
    ]
    patient_values = _patient_values(demographics, patient_ref)
    _record_composer_round(
        output_dir,
        state.get("run_id"),
        review_round + 1,
        {"messages": messages},
        None,
        patient_values,
    )
    response = llm.invoke(messages)

    elapsed = time.time() - t0
    logger.info("LLM responded in %.1fs", elapsed)

    try:
        raw_response = content_to_text(response.content)
        _record_composer_round(
            output_dir,
            state.get("run_id"),
            review_round + 1,
            {"messages": messages},
            {"content": raw_response},
            patient_values,
        )
        brief_data = _parse_brief_from_response(raw_response)
        # The DMN audit trail is authoritative data the executor already built —
        # inject it directly rather than asking the LLM to echo it back through
        # the prompt (F10). This removes a large, error-prone round-trip and
        # guarantees the recorded trail matches what actually executed.
        brief_data["dmn_audit_trail"] = dmn_results
        brief_data["conflicts"] = coerce_conflicts(brief_data.get("conflicts"))
        # Record the accumulated clinician feedback on the brief (F17b) — injected
        # in code, not asked of the LLM, so the audit trail is authoritative. Left
        # untouched (default []) on authoring / monolith runs with no history.
        if review_history:
            brief_data["revision_history"] = normalize_review_history(review_history)
        _sanitize_provenance(brief_data, cpg_ids[0] if cpg_ids else "unspecified")
        _neutralize_unprovided_pronouns(brief_data, demographics)
        _resolve_missing_medication_codes(brief_data)
        merged_medication_groups = _merge_same_cpg_medication_regimens(brief_data)
        _merge_goals_for_merged_regimens(brief_data, merged_medication_groups)
        brief = PlanningBrief.model_validate(brief_data)
        brief = stamp_activity_ids(brief, base_brief)
        brief_dict = brief.model_dump(mode="json")

        if output_dir:
            write_artifact(output_dir, "planning-brief.json", brief_dict)

        logger.info(
            "Planning Brief: %d goals, %d activities, %d conflicts",
            len(brief.goals),
            len(brief.activities),
            len(brief.conflicts),
        )

        return {
            "planning_brief": brief_dict,
            "brief_review_feedback": "",
            "brief_parse_failed": False,
            "plan_composer_prompt": user_prompt,
        }

    except Exception as e:
        raw_response = content_to_text(response.content)
        try:
            parsed_fragment_source = _parse_brief_from_response(raw_response)
        except Exception:
            parsed_fragment_source = None
        fragment = _offending_fragment(parsed_fragment_source, raw_response)
        safe_error = _sanitize_artifact_value(str(e), patient_values)
        safe_fragment = _sanitize_artifact_value(fragment, patient_values)
        feedback = (
            f"Planning Brief parse/normalization error: {safe_error}\n"
            f"Offending fragment (first 500 characters): {str(safe_fragment)[:500]}"
        )
        logger.exception("Failed to parse or normalize Planning Brief from LLM response")
        return {
            "planning_brief": {
                "patient_reference": patient_ref,
                "applicable_cpgs": cpg_ids,
                "goals": [],
                "activities": [],
                "review_status": "flagged",
                "review_feedback": feedback,
            },
            "brief_review_feedback": feedback,
            "brief_parse_failed": True,
            "brief_review_count": review_round + 1,
            "plan_composer_prompt": user_prompt,
        }
