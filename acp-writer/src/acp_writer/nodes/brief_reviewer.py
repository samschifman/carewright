"""Brief Reviewer — adversarial review of the Planning Brief.

Uses a clinical pharmacist persona distinct from Plan Composer.
APPROVE/REVISE protocol with max 2 review loops.
"""

import json
import logging
import time

import mlflow
from cpg_contracts import content_to_text

from acp_writer.llm_json import loads_json
from acp_writer.llm import get_llm
from acp_writer.output import write_artifact
from acp_writer.planning_brief import PlanningBrief, ReviewStatus
from acp_writer.prompts.brief_reviewer import (
    BRIEF_REVIEWER_REVISION_NOTE,
    BRIEF_REVIEWER_SYSTEM,
    BRIEF_REVIEWER_USER,
)
from acp_writer.state import CarePlanComposerState

logger = logging.getLogger(__name__)


def _format_code_list(codes: list[dict]) -> str:
    if not codes:
        return "None"
    return ", ".join(c.get("display", c.get("code", "?")) for c in codes)


def _schema_validate(brief_dict: dict) -> list[str]:
    """Deterministic schema validation gate — free before spending LLM tokens."""
    errors = []
    try:
        PlanningBrief.model_validate(brief_dict)
    except Exception as e:
        errors.append(f"Schema validation failed: {e}")
        return errors

    if not brief_dict.get("goals") and not brief_dict.get("activities"):
        errors.append("Brief has no goals and no activities — nothing to generate.")

    for i, act in enumerate(brief_dict.get("activities", [])):
        if not act.get("source_cpg"):
            errors.append(f"Activity {i} missing source_cpg — provenance required.")
        if act.get("type") == "medication" and not act.get("dose"):
            errors.append(f"Activity {i} is medication type but missing dose.")
        if act.get("type") == "medication" and not act.get("code"):
            errors.append(f"Activity {i} is medication type but missing a coded medication concept.")

    return errors


@mlflow.trace(name="brief_reviewer")
def brief_reviewer(state: CarePlanComposerState) -> dict:
    """Review the Planning Brief for clinical correctness."""
    brief_dict = state.get("planning_brief", {})
    review_count = state.get("brief_review_count", 0)
    output_dir = state.get("output_dir", "")

    schema_errors = _schema_validate(brief_dict)
    if schema_errors:
        feedback = "Schema validation errors:\n" + "\n".join(f"- {e}" for e in schema_errors)
        logger.warning("Brief failed schema validation: %s", schema_errors)
        return {
            "brief_review_feedback": feedback,
            "brief_review_count": review_count + 1,
        }

    logger.info("── Brief Reviewer (iteration %d) ──", review_count + 1)

    patient_ref = state.get("patient_reference", "unknown")
    condition_codes = state.get("condition_codes", [])
    medication_codes = state.get("medication_codes", [])
    allergy_codes = state.get("allergy_codes", [])
    recommendations = state.get("recommendations", [])

    # F17: on a request-changes revision the composer minimally revised an
    # already-approved base plan, so the reviewer should judge the changes for
    # safety rather than demand authoring-style completeness rewrites.
    prior_brief = state.get("prior_planning_brief") or {}
    is_revision = bool(prior_brief.get("goals") or prior_brief.get("activities"))
    revision_note = BRIEF_REVIEWER_REVISION_NOTE if is_revision else ""

    user_prompt = BRIEF_REVIEWER_USER.format(
        revision_note=revision_note,
        patient_reference=patient_ref,
        conditions=_format_code_list(condition_codes),
        medications=_format_code_list(medication_codes),
        allergies=_format_code_list(allergy_codes),
        planning_brief=json.dumps(brief_dict, indent=2, default=str),
        recommendations=json.dumps(
            [{"id": r.get("id"), "title": r.get("title"), "type": r.get("recommendation_type"),
              "strength": (r.get("certainty") or {}).get("strength", "ungraded"),
              "content": r.get("content"), "source_cpg": r.get("source_cpg")}
             for r in recommendations],
            indent=2,
        ),
    )

    llm = get_llm(state)
    logger.info("Calling LLM...")
    t0 = time.time()

    response = llm.invoke([
        {"role": "system", "content": BRIEF_REVIEWER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ])

    elapsed = time.time() - t0
    logger.info("LLM responded in %.1fs", elapsed)

    try:
        review = loads_json(content_to_text(response.content))
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Could not parse review response, treating as APPROVE: %s", e)
        review = {"verdict": "APPROVE", "issues": []}

    verdict = review.get("verdict", "APPROVE").upper()
    issues = review.get("issues", [])

    if output_dir:
        write_artifact(output_dir, f"brief-review-{review_count + 1}.json", review)

    if verdict == "REVISE" and issues:
        feedback_parts = []
        for i, issue in enumerate(issues, 1):
            severity = issue.get("severity", "error")
            desc = issue.get("description", "")
            fix = issue.get("fix", "")
            feedback_parts.append(f"{i}. [{severity}] {desc} — Fix: {fix}")
        feedback = "\n".join(feedback_parts)
        logger.info("Brief REVISE: %d issues", len(issues))

        updated_brief = dict(brief_dict)
        updated_brief["review_status"] = ReviewStatus.REVISED.value

        return {
            "planning_brief": updated_brief,
            "brief_review_feedback": feedback,
            "brief_review_count": review_count + 1,
        }

    logger.info("Brief APPROVED (iteration %d)", review_count + 1)

    updated_brief = dict(brief_dict)
    updated_brief["review_status"] = ReviewStatus.APPROVED.value

    return {
        "planning_brief": updated_brief,
        "brief_review_feedback": "",
        "brief_review_count": review_count + 1,
    }
