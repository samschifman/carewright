"""Plan Composer — core clinical reasoning node.

LLM maps DMN results + recommendations to a PlanningBrief.
Assigns FHIR codes via terminology lookup, populates workflow
context for future BPMN generation.
"""

import json
import logging
import time
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

logger = logging.getLogger(__name__)


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

    # Revision mode (F17a): a prior brief in state means this is a request-changes
    # loop. The monolith never sets this key (it is one-shot from an IPS bundle),
    # so it always authors.
    prior_brief = state.get("prior_planning_brief") or {}
    is_revision = bool(prior_brief.get("goals") or prior_brief.get("activities"))

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

    response = llm.invoke([
        {"role": "system", "content": compose_system_prompt(is_revision)},
        {"role": "user", "content": user_prompt},
    ])

    elapsed = time.time() - t0
    logger.info("LLM responded in %.1fs", elapsed)

    try:
        brief_data = _parse_brief_from_response(content_to_text(response.content))
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
            "plan_composer_prompt": user_prompt,
        }

    except (json.JSONDecodeError, Exception) as e:
        logger.error("Failed to parse Planning Brief from LLM response: %s", e)
        logger.debug("Raw response: %s", content_to_text(response.content)[:500])
        return {
            "planning_brief": {
                "patient_reference": patient_ref,
                "applicable_cpgs": cpg_ids,
                "goals": [],
                "activities": [],
                "review_status": "flagged",
                "review_feedback": f"LLM response parse error: {e}",
            },
            "brief_review_feedback": "",
            "plan_composer_prompt": user_prompt,
        }
