"""Adversarial clinical and operational review of generated Process IR."""

import json
import logging
import time

import mlflow
from cpg_contracts import content_to_text, get_llm
from cpg_ingester.nodes.structure_analyzer import _parse_llm_json
from cpg_ingester.output import write_artifact
from cpg_ingester.llm_responses import persist_llm_response
from cpg_ingester.prompts.bpmn_semantic_reviewer import (
    BPMN_SEMANTIC_REVIEWER_SYSTEM,
    BPMN_SEMANTIC_REVIEWER_USER,
)

logger = logging.getLogger(__name__)


def _validate_review_result(result: dict) -> dict:
    """Validate claims and derive routing fields from the claims themselves."""
    if not isinstance(result, dict):
        raise ValueError("reviewer response must be a JSON object")
    claims = result.get("claims_checked")
    if not isinstance(claims, list):
        raise ValueError("reviewer response must include a claims_checked list")
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise ValueError(f"reviewer claim {index} is not an object")
        if claim.get("severity") not in {"CRITICAL", "MINOR"}:
            raise ValueError(f"reviewer claim {index} has invalid severity")
        if claim.get("verdict") not in {"VERIFIED", "DISCREPANCY"}:
            raise ValueError(f"reviewer claim {index} has invalid verdict")
    critical = [
        claim for claim in claims
        if claim.get("verdict") == "DISCREPANCY" and claim.get("severity") == "CRITICAL"
    ]
    derived_discrepancies = []
    for claim in critical:
        feedback = claim.get("feedback") or claim.get("evidence") or claim.get("claim", "")
        if isinstance(feedback, str) and feedback.strip() and feedback not in derived_discrepancies:
            derived_discrepancies.append(feedback.strip())
    result["discrepancies_found"] = bool(critical)
    supplied = result.get("discrepancies")
    if not isinstance(supplied, list) or not all(
        isinstance(item, str) and item.strip() for item in supplied
    ) or bool(supplied) != bool(critical):
        result["discrepancies"] = derived_discrepancies
    return result


def _safe_name(value: str) -> str:
    """Make an artifact filename from a process label."""
    import re

    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")[:50] or "process"


@mlflow.trace(name="bpmn_semantic_reviewer")
def bpmn_semantic_reviewer(state: dict) -> dict:
    """Review generated Process IR claims against the original CPG text."""
    process_ir = state.get("process_ir", {})
    item = state.get("item", {})
    source_pages = state.get("source_pages", "")
    output_dir = state.get("output_dir", "output")
    retry_count = state.get("semantic_retry_count", 0)
    name = item.get("name", "unknown process")

    if not process_ir:
        return {"semantic_discrepancies": ["No ProcessIR available to review"]}
    if not source_pages.strip():
        logger.warning("No source text for semantic review of %s", name)
        return {
            "semantic_discrepancies": [
                "No source text available to verify this process against the CPG"
            ],
            "force_escalate": True,
            "escalation_reason": "no-source-text",
        }

    messages = [
        {"role": "system", "content": BPMN_SEMANTIC_REVIEWER_SYSTEM},
        {"role": "user", "content": BPMN_SEMANTIC_REVIEWER_USER.format(
            name=name,
            process_ir=json.dumps(process_ir, indent=2, ensure_ascii=False, default=str),
            source_pages=source_pages,
        )},
    ]
    llm = get_llm(state)
    logger.info("Reviewing ProcessIR claims for %s", name)
    started = time.time()
    response = llm.invoke(messages)
    logger.info("ProcessIR reviewer responded in %.1fs", time.time() - started)
    persist_llm_response(
        output_dir,
        process_name=name,
        role="reviewer",
        content=content_to_text(response.content),
    )

    try:
        result = _validate_review_result(_parse_llm_json(content_to_text(response.content)))
    except (json.JSONDecodeError, ValueError) as exc:
        messages.extend([
            {"role": "assistant", "content": content_to_text(response.content)},
            {"role": "user", "content": (
                "Your last response did not match the required JSON schema: "
                f"{exc}. Return one corrected JSON object only."
            )},
        ])
        response = llm.invoke(messages)
        persist_llm_response(
            output_dir,
            process_name=name,
            role="reviewer",
            content=content_to_text(response.content),
        )
        try:
            result = _validate_review_result(_parse_llm_json(content_to_text(response.content)))
        except (json.JSONDecodeError, ValueError):
            logger.warning("ProcessIR reviewer response remained invalid for %s", name)
            return {
                "semantic_discrepancies": [
                    "Semantic reviewer did not return a valid claim-level JSON response"
                ],
                "force_escalate": True,
                "escalation_reason": "reviewer-unparseable",
            }

    discrepancies = result["discrepancies"]
    safe_name = _safe_name(name)
    write_artifact(output_dir, f"bpmn-review-{safe_name}-{retry_count + 1}.json", result)
    logger.info(
        "ProcessIR semantic review %s: %d claims, %d critical discrepancies",
        name, len(result["claims_checked"]), len(discrepancies),
    )
    return {
        "semantic_discrepancies": discrepancies,
        "semantic_retry_count": retry_count + int(bool(discrepancies)),
        "force_escalate": False,
    }
