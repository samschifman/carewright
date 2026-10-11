"""FHIR Bundle Generator — produce valid FHIR R4 Bundle from Planning Brief.

Deterministic code, no LLM. Delegates to fhir_bundle_builder
for resource construction. Adds AI Transparency IG compliance
(AIAST tags, AI-Device, AI-Provenance).
"""

import logging

import mlflow

from acp_writer.output import write_artifact
from acp_writer.planning_brief import PlanningBrief
from acp_writer.state import CarePlanComposerState
from acp_writer.validators.fhir_bundle_builder import build_fhir_bundle

logger = logging.getLogger(__name__)


@mlflow.trace(name="fhir_bundle_generator")
def fhir_bundle_generator(state: CarePlanComposerState) -> dict:
    """Generate a FHIR R4 transaction Bundle from the Planning Brief."""
    logger.info("── FHIR Bundle Generator ──")
    brief_dict = state.get("planning_brief", {})
    output_dir = state.get("output_dir", "")
    feedback = state.get("fhir_review_feedback", "")

    if not brief_dict or (not brief_dict.get("goals") and not brief_dict.get("activities")):
        logger.warning("No Planning Brief or empty goals/activities — producing empty bundle")
        return {
            "fhir_bundle": {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [],
            },
        }

    try:
        brief = PlanningBrief.model_validate(brief_dict)
    except Exception as e:
        # PlanningBrief coerces loose conflicts before validation (see its
        # _coerce_conflicts_field validator), so reaching here means the goals/
        # activities themselves are invalid. Surface it — an empty bundle must
        # not masquerade as a successful generation.
        logger.error("Invalid Planning Brief: %s", e)
        return {
            "fhir_bundle": {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [],
            },
            "fhir_generation_error": f"Invalid Planning Brief: {e}",
        }

    prompts: dict[str, str] = {}
    if state.get("plan_composer_prompt"):
        prompts["plan_composer"] = state["plan_composer_prompt"]
    if state.get("conflict_prompt"):
        prompts["conflict_analyst"] = state["conflict_prompt"]
    for label, prompt in (state.get("automation_prompts") or {}).items():
        if prompt:
            prompts[label] = prompt

    bundle = build_fhir_bundle(
        brief,
        patient_demographics=state.get("patient_demographics"),
        model_id=state.get("llm_model"),
        prompts=prompts,
        careplan_id=state.get("careplan_id"),
    )

    resource_types: dict[str, int] = {}
    for entry in bundle.get("entry", []):
        rt = entry.get("resource", {}).get("resourceType", "unknown")
        resource_types[rt] = resource_types.get(rt, 0) + 1

    logger.info("Generated FHIR Bundle: %s", resource_types)

    if output_dir:
        write_artifact(output_dir, "fhir-bundle.json", bundle)
        write_artifact(
            output_dir,
            "planning-brief.json",
            brief.model_dump(mode="json", exclude_none=True),
        )

    return {"fhir_bundle": bundle}
