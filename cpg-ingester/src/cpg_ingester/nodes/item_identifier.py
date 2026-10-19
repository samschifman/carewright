"""Item Identifier — identifies decisions and recommendations, pre-assigns GUIDs, classifies tiers."""

import json
import logging
import re
import time
import uuid

import mlflow
from pydantic import ValidationError
from cpg_contracts import (
    Extraction,
    content_to_text,
    decision_model_id,
    get_llm,
)
from cpg_contracts.automation.ir import automation_template_id
from cpg_ingester.validators.dmn_syntax import VALID_HIT_POLICIES
from cpg_ingester.nodes.structure_analyzer import _parse_llm_json
from cpg_ingester.output import write_artifact
from cpg_ingester.review_feedback import render_reviewer_feedback
from cpg_ingester.prompts.item_identifier import (
    ITEM_IDENTIFIER_SYSTEM,
    ITEM_IDENTIFIER_USER,
)

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"treatment", "screening", "monitoring", "risk-assessment", "diagnostic"}
VALID_PROCESS_TIERS = {"A", "B"}
VALID_REC_TYPES = {
    "treatment", "diagnostic", "monitoring", "lifestyle", "educational",
    "referral", "screening", "contraindication", "process",
}
VALID_STRENGTHS = {
    "strong-for", "conditional-for", "consensus", "no-recommendation",
    "conditional-against", "strong-against",
}
VALID_EVIDENCE = {"high", "moderate", "low", "very-low", "ungraded"}
CODE_TOKEN_RE = re.compile(r"^https?://[^|\s]+\|[^|\s]+$")


def _normalize_extraction_fields(result: dict) -> dict:
    """Normalize legacy/model aliases to the Extraction contract field names."""
    for decision in result.get("decisions", []) or []:
        for variable in decision.get("inputs", []) or []:
            extraction = variable.get("extraction")
            if not isinstance(extraction, dict):
                continue
            if "function" not in extraction and "type" in extraction:
                extraction["function"] = extraction.pop("type")
            else:
                extraction.pop("type", None)
            if "params" not in extraction:
                legacy_params = extraction.pop("parameters", None)
                if isinstance(legacy_params, dict):
                    extraction["params"] = legacy_params
                else:
                    # Some model outputs flattened parameters into the
                    # extraction object instead of nesting them.
                    flat_params = {
                        key: value for key, value in extraction.items()
                        if key not in {"function", "params", "parameters"}
                    }
                    if flat_params:
                        extraction["params"] = flat_params
                        for key in flat_params:
                            extraction.pop(key, None)
            extraction.pop("parameters", None)
    return result


def _assign_guids(manifest: list[dict]) -> list[dict]:
    """Assign GUIDs to all items and resolve cross-reference names to GUIDs."""
    name_to_guid = {}
    for item in manifest:
        guid = str(uuid.uuid4())
        item["id"] = guid
        name = item.get("name") or item.get("title", "")
        if item.get("type") == "decision":
            item["model_id"] = decision_model_id(name)
        elif item.get("type") == "process":
            if isinstance(name, str) and name.strip():
                item["template_id"] = automation_template_id(name)
        name_to_guid[name] = guid

    for item in manifest:
        raw_refs = item.get("cross_references", []) or []
        resolved = []
        for ref in raw_refs:
            if isinstance(ref, str) and ref in name_to_guid:
                resolved.append(name_to_guid[ref])
            elif isinstance(ref, str):
                resolved.append(ref)
        item["cross_references"] = resolved

        modifies_name = item.get("modifies")
        if modifies_name and modifies_name in name_to_guid:
            item["modifies"] = name_to_guid[modifies_name]

    return manifest


def _validate_decision(item: dict) -> list[str]:
    """Validate a decision item and return a list of issues."""
    issues = []
    if not item.get("name"):
        issues.append("Decision missing 'name'")
    if item.get("category") and item["category"] not in VALID_CATEGORIES:
        issues.append(f"Invalid category: {item['category']}")
    if item.get("hit_policy") and item["hit_policy"] not in VALID_HIT_POLICIES:
        issues.append(f"Invalid hit_policy: {item['hit_policy']}")
    if not item.get("inputs"):
        issues.append("Decision has no inputs")
    for input_variable in item.get("inputs", []):
        codes = input_variable.get("codes")
        if codes is not None:
            if not isinstance(codes, list) or any(
                not isinstance(code, str) or not CODE_TOKEN_RE.fullmatch(code)
                for code in codes
            ):
                issues.append(
                    f"Decision input '{input_variable.get('name', '?')}' has invalid codes"
                )
        extraction = input_variable.get("extraction")
        if extraction is not None:
            if not isinstance(extraction, dict):
                issues.append(f"Decision input '{input_variable.get('name', '?')}' has invalid extraction")
                continue
            try:
                Extraction.model_validate(extraction)
            except ValidationError as exc:
                issues.append(
                    f"Decision input '{input_variable.get('name', '?')}' extraction invalid: "
                    f"{exc.errors()}"
                )
    return issues


def _validate_recommendation(item: dict) -> list[str]:
    """Validate a recommendation item and return a list of issues."""
    issues = []
    if not item.get("title"):
        issues.append("Recommendation missing 'title'")
    if item.get("recommendation_type") and item["recommendation_type"] not in VALID_REC_TYPES:
        issues.append(f"Invalid recommendation_type: {item['recommendation_type']}")
    if item.get("certainty_strength") and item["certainty_strength"] not in VALID_STRENGTHS:
        issues.append(f"Invalid certainty_strength: {item['certainty_strength']}")
    if item.get("certainty_evidence") and item["certainty_evidence"] not in VALID_EVIDENCE:
        issues.append(f"Invalid certainty_evidence: {item['certainty_evidence']}")
    return issues


def _validate_process(item: dict) -> list[str]:
    """Validate a process candidate and return a list of issues."""
    issues = []
    if not isinstance(item.get("name"), str) or not item["name"].strip():
        issues.append("Process missing 'name'")
    if not isinstance(item.get("section"), str) or not item["section"].strip():
        issues.append("Process missing 'section'")
    steps = item.get("steps")
    if not isinstance(steps, list) or not any(
        isinstance(step, str) and step.strip() for step in steps
    ):
        issues.append("Process must have at least one step")
    timing_cues = item.get("timing_cues")
    has_timing = isinstance(timing_cues, list) and any(
        isinstance(cue, str) and cue.strip() for cue in timing_cues
    )
    trigger_hint = item.get("trigger_hint")
    has_trigger = isinstance(trigger_hint, str) and bool(trigger_hint.strip())
    if not has_timing and not has_trigger:
        issues.append("Process needs at least one timing cue or trigger hint")
    if item.get("tier_hint") not in VALID_PROCESS_TIERS:
        issues.append(f"Invalid process tier_hint: {item.get('tier_hint')}")
    linked_recommendations = item.get("linked_recommendations")
    if not isinstance(linked_recommendations, list) or not any(
        isinstance(name, str) and name.strip() for name in linked_recommendations
    ):
        issues.append("Process must link to at least one recommendation")
    return issues


@mlflow.trace(name="item_identifier")
def item_identifier(state: dict) -> dict:
    """Identify all decisions and recommendations in the CPG."""
    logger.info("── Item Identifier ──")
    markdown = state.get("markdown", "")
    section_map = state.get("section_map", [])
    abbreviations = state.get("abbreviations", {})
    output_dir = state.get("output_dir", "output")
    feedback = state.get("classification_review_feedback", "")

    llm = get_llm(state)

    section_map_str = "\n".join(
        f"- [{s['classification']}] Page {s.get('page_start', '?')}: \"{s['heading']}\""
        for s in section_map
    )
    abbr_str = "\n".join(f"- {k}: {v}" for k, v in abbreviations.items()) if abbreviations else "(none extracted)"

    content = markdown
    prior_manifest = state.get("prior_item_manifest") or []
    feedback_context = {
        str(item.get("id")): str(item.get("name") or item.get("title") or item.get("id"))
        for item in prior_manifest
        if isinstance(item, dict) and item.get("id")
    }
    feedback_context.update({
        f"section-{index}": str(section.get("heading", ""))
        for index, section in enumerate(state.get("prior_section_map") or section_map)
    })
    reviewer_feedback = render_reviewer_feedback(
        state.get("review_feedback"),
        state.get("review_comment", ""),
        state.get("review_iteration", 0),
        item_types={"classification", "process"},
        item_context=feedback_context,
    )
    if reviewer_feedback:
        content = reviewer_feedback + "\n\n---\n\n" + content
    if feedback:
        content = f"REVIEWER FEEDBACK FROM PREVIOUS ITERATION — address these issues:\n{feedback}\n\n---\n\n{content}"

    logger.info("Identifying items (LLM)...")
    t0 = time.time()
    response = llm.invoke([
        {"role": "system", "content": ITEM_IDENTIFIER_SYSTEM},
        {"role": "user", "content": ITEM_IDENTIFIER_USER.format(
            section_map=section_map_str,
            abbreviations=abbr_str,
            content=content,
        )},
    ])

    logger.info("Item identification: %.1fs", time.time() - t0)

    try:
        result = _parse_llm_json(content_to_text(response.content))
    except (json.JSONDecodeError, ValueError):
        logger.error("Failed to parse item identifier response")
        return {"item_manifest": []}

    result = _normalize_extraction_fields(result)

    decisions = result.get("decisions", [])
    recommendations = result.get("recommendations", [])
    processes = result.get("processes", [])

    validation_issues = []
    for d in decisions:
        d["type"] = "decision"
        issues = _validate_decision(d)
        if issues:
            validation_issues.extend(issues)
            logger.warning("Decision '%s' has issues: %s", d.get("name"), issues)

    for r in recommendations:
        r["type"] = "recommendation"
        issues = _validate_recommendation(r)
        if issues:
            validation_issues.extend(issues)
            logger.warning("Recommendation '%s' has issues: %s", r.get("title"), issues)

    for process in processes:
        process["type"] = "process"
        issues = _validate_process(process)
        if issues:
            validation_issues.extend(issues)
            logger.warning("Process '%s' has issues: %s", process.get("name"), issues)

    manifest = decisions + recommendations + processes
    manifest = _assign_guids(manifest)

    for item in manifest:
        section_heading = item.get("section", "")
        matching = [s for s in section_map if section_heading in s.get("heading", "")]
        if matching:
            item["source_page_range"] = f"pages {matching[0].get('page_start', '?')}-{matching[0].get('page_end', '?')}"

    write_artifact(output_dir, "manifest.json", manifest)

    logger.info(
        "Item identification complete: %d decisions, %d recommendations, %d processes, %d validation issues",
        len(decisions), len(recommendations), len(processes), len(validation_issues),
    )

    review_count = state.get("classification_review_count", 0)
    if feedback:
        review_count += 1

    return {
        "item_manifest": manifest,
        "classification_review_count": review_count,
        "classification_review_feedback": "",
    }
