"""Recommendation Extractor — extracts recommendations per section batch."""

import json
import logging
import time

import mlflow
from cpg_contracts import content_to_text, get_llm
from cpg_ingester.nodes.structure_analyzer import _parse_llm_json
from cpg_ingester.output import write_artifact
from cpg_ingester.review_feedback import render_reviewer_feedback
from cpg_ingester.prompts.rec_extractor import (
    REC_EXTRACTOR_SYSTEM,
    REC_EXTRACTOR_USER,
)

logger = logging.getLogger(__name__)


@mlflow.trace(name="rec_extractor")
def rec_extractor(state: dict) -> dict:
    """Extract recommendations for a section batch."""
    logger.info("── Rec Extractor ──")
    items = state.get("items", [])
    source_pages = state.get("source_pages", "")
    grading_definitions = state.get("grading_definitions", "")
    abbreviations = state.get("abbreviations", {})
    output_dir = state.get("output_dir", "output")
    schema_errors = state.get("schema_errors", [])
    semantic_discrepancies = state.get("semantic_discrepancies", [])

    if not items:
        return {"recommendations": [], "schema_errors": [], "semantic_discrepancies": []}

    item_specs = json.dumps(items, indent=2, default=str)
    abbr_str = "\n".join(f"- {k}: {v}" for k, v in abbreviations.items()) if abbreviations else "(none)"

    feedback_sections = []
    if schema_errors:
        feedback_sections.append(
            "PREVIOUS ATTEMPT HAD SCHEMA ERRORS — fix these:\n"
            + "\n".join(f"- {e}" for e in schema_errors)
        )
    if semantic_discrepancies:
        feedback_sections.append(
            "PREVIOUS ATTEMPT HAD SEMANTIC ISSUES — fix these:\n"
            + "\n".join(f"- {d}" for d in semantic_discrepancies)
        )
    human_feedback = state.get("human_feedback", "") or render_reviewer_feedback(
        state.get("review_feedback"),
        state.get("review_comment", ""),
        state.get("review_iteration", 0),
    )
    if human_feedback:
        feedback_sections.append(human_feedback)
    feedback = "\n\n".join(feedback_sections)

    llm = get_llm(state)

    logger.info("Calling LLM...")
    t0 = time.time()
    response = llm.invoke([
        {"role": "system", "content": REC_EXTRACTOR_SYSTEM},
        {"role": "user", "content": REC_EXTRACTOR_USER.format(
            item_specs=item_specs,
            grading_definitions=grading_definitions or "(not specified)",
            abbreviations=abbr_str,
            source_pages=source_pages,
            feedback=feedback,
        )},
    ])
    logger.info("LLM responded in %.1fs", time.time() - t0)

    try:
        result = _parse_llm_json(content_to_text(response.content))
    except (json.JSONDecodeError, ValueError):
        logger.error("Failed to parse recommendation extractor response")
        return {"recommendations": [], "schema_errors": ["LLM response was not valid JSON"], "semantic_discrepancies": []}

    recommendations = result.get("recommendations", [])

    section = items[0].get("section", "unknown") if items else "unknown"
    write_artifact(output_dir, f"recommendations-{section}.json", recommendations)

    review_count = state.get("review_count", 0)
    if schema_errors or semantic_discrepancies:
        review_count += 1

    logger.info("Extracted %d recommendations for section %s", len(recommendations), section)

    return {
        "recommendations": recommendations,
        "schema_errors": [],
        "semantic_discrepancies": [],
        "review_count": review_count,
    }
