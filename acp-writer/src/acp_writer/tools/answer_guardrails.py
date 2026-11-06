"""Deterministic answer verification guardrails.

One choke point, every path, fail-closed. Verification lives at the
QAAnswer boundary — no answer leaves the backend unverified.
Refusal is decided by code, never by the agent.

Each answer class has a different evidence contract:
- numeric_retrieval: value-consistency, concept-consistency, conflict, provenance
- boolean_presence: provenance, conflict
- boolean_absence: ledger-backed absence check, conflict
- composite_reasoning: provenance (multi-resource), conflict
"""

import logging
from typing import Any

from acp_writer.tools.bundle_inventory import BundleInventory, InventoryEntry
from acp_writer.tools.ips_extractor import (
    _get_resources_with_entry,
    _normalize_display,
    _resource_ref,
)
from acp_writer.tools.qa_result import QAAnswer
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


def classify_answer(
    answer: QAAnswer,
    tool_ledger: list[dict] | None = None,
    question_intent: str | None = None,
) -> str:
    """Classify the answer for guardrail dispatch.

    question_intent (boolean/numeric/open) overrides type inference
    so that a boolean-intent question with a numeric answer is classified
    under the boolean contract, not numeric_retrieval.

    Returns one of: numeric_retrieval, boolean_presence, boolean_absence,
    composite_reasoning, insufficient.
    """
    if answer.insufficient_data:
        return "insufficient"

    if question_intent == "boolean" or isinstance(answer.value, bool):
        if tool_ledger:
            relevant = [
                e for e in tool_ledger
                if e.get("type") in ("check_condition", "check_medication", "check_allergy", "lookup_observation")
            ]
            if relevant:
                all_misses = all(e.get("definitive_miss", False) for e in relevant)
                if all_misses:
                    return "boolean_absence"
                if any(e.get("found", False) for e in relevant):
                    return "boolean_presence"

        if isinstance(answer.value, bool) and answer.value is False and not answer.provenance:
            return "boolean_absence"

        if answer.provenance and len(answer.provenance) > 2:
            return "composite_reasoning"

        return "boolean_presence"

    if isinstance(answer.value, (int, float)):
        return "numeric_retrieval"

    return "composite_reasoning"


@trace_safe(name="guardrail_verify_answer")
def verify_answer(
    answer: QAAnswer,
    question: str,
    bundle: dict,
    inventory: BundleInventory,
    tool_ledger: list[dict] | None = None,
    question_intent: str | None = None,
) -> QAAnswer:
    """Run guardrails appropriate to the answer class.

    Called on EVERY answer path — resolver, query-plan, and agent.
    Dispatches guardrails by answer-type verification contract.
    question_intent overrides the classifier's type inference when provided.
    """
    if answer.insufficient_data:
        return answer

    answer_class = classify_answer(answer, tool_ledger, question_intent)
    answer.resolution_basis = f"verification_class:{answer_class}"

    if answer_class == "numeric_retrieval":
        result = _check_provenance_required(answer)
        if result.insufficient_data:
            return result
        result = _check_value_consistency(result, bundle)
        if result.insufficient_data:
            return result
        result = _check_concept_consistency(result, question, inventory)
        if result.insufficient_data:
            return result
        return _check_conflict(result, bundle, inventory)

    if answer_class == "boolean_presence":
        result = _check_provenance_required(answer)
        if result.insufficient_data:
            return result
        return _check_conflict(result, bundle, inventory)

    if answer_class == "boolean_absence":
        result = _check_absence_with_ledger(answer, tool_ledger, question, inventory)
        if result.insufficient_data:
            return result
        return _check_conflict(result, bundle, inventory)

    if answer_class == "composite_reasoning":
        result = _check_provenance_required(answer)
        if result.insufficient_data:
            return result
        return _check_conflict(result, bundle, inventory)

    return answer


def _downgrade(answer: QAAnswer, guardrail: str, reason: str) -> QAAnswer:
    """Produce a guardrail-downgraded insufficient_data answer."""
    logger.info('Guardrail (details redacted)')
    return QAAnswer(
        value=None,
        kind="insufficient_data",
        provenance=answer.provenance,
        insufficient_data=True,
        error=f"guardrail:{guardrail}",
        answered_by="guardrail_downgrade",
        resolution_basis=f"{guardrail}: {reason}",
    )


@trace_safe(name="guardrail_provenance_required")
def _check_provenance_required(answer: QAAnswer) -> QAAnswer:
    """Non-insufficient_data answers require provenance; else downgrade."""
    if answer.value is not None and not answer.provenance:
        return _downgrade(answer, "provenance_required", "answer without provenance")
    logger.debug("Guardrail [provenance_required]: passed")
    return answer


@trace_safe(name="guardrail_value_consistency")
def _check_value_consistency(answer: QAAnswer, bundle: dict) -> QAAnswer:
    """Numeric answers must match a value in a cited resource. Fail closed.

    Only applies to numeric_retrieval class (booleans excluded by dispatch
    and by the isinstance guard below for direct-call safety).
    """
    if isinstance(answer.value, bool) or not isinstance(answer.value, (int, float)):
        logger.debug("Guardrail [value_consistency]: skipped (non-numeric)")
        return answer

    if not answer.provenance:
        logger.debug("Guardrail [value_consistency]: skipped (no provenance)")
        return answer

    for ref in answer.provenance:
        if _value_in_resource(answer.value, ref, bundle):
            logger.debug("Guardrail [value_consistency]: passed (value found in %s)", ref)
            return answer

    return _downgrade(
        answer, "value_consistency",
        f"numeric value {answer.value} not found in cited resources {answer.provenance}",
    )


def _value_in_resource(value: float, ref: str, bundle: dict) -> bool:
    """Check if a numeric value appears in a resource referenced by ref.

    Returns False (fail closed) if the resource cannot be found.
    """
    resource_found = False

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        resource_ref = _resource_ref(resource, entry)
        if resource_ref != ref:
            continue
        resource_found = True

        vq = resource.get("valueQuantity")
        if isinstance(vq, dict) and vq.get("value") is not None:
            try:
                if abs(float(vq["value"]) - value) < 0.01:
                    return True
            except (ValueError, TypeError):
                pass

        vs = resource.get("valueString")
        if vs is not None:
            try:
                if abs(float(vs) - value) < 0.01:
                    return True
            except (ValueError, TypeError):
                pass

        for component in resource.get("component", []):
            cvq = component.get("valueQuantity")
            if cvq and cvq.get("value") is not None:
                try:
                    if abs(float(cvq["value"]) - value) < 0.01:
                        return True
                except (ValueError, TypeError):
                    pass

    if not resource_found:
        logger.debug("Guardrail [value_consistency]: resource %s not found — fail closed", ref)

    return False


@trace_safe(name="guardrail_concept_consistency")
def _check_concept_consistency(
    answer: QAAnswer,
    question_term: str,
    inventory: BundleInventory,
) -> QAAnswer:
    """Verify cited resource matches the question's concept.

    Only applies to numeric_retrieval class (its original purpose:
    hemoglobin vs HbA1c). Uses terminology cross-check independent
    of the resolution pipeline.
    """
    if not answer.provenance:
        logger.debug("Guardrail [concept_consistency]: skipped (no provenance)")
        return answer

    cited_entries = [
        e for e in inventory.entries
        if e.fhir_reference in answer.provenance and e.resource_type == "Observation"
    ]

    if not cited_entries:
        logger.debug("Guardrail [concept_consistency]: skipped (no cited observations)")
        return answer

    term_candidates = _get_terminology_candidates(question_term)

    if term_candidates is None:
        logger.debug("Guardrail [concept_consistency]: skipped (terminology unavailable)")
        return answer

    if not term_candidates:
        norm_term = _normalize_display(question_term)
        for entry in cited_entries:
            norm_display = _normalize_display(entry.display) if entry.display else ""
            if norm_term and norm_display and (norm_term in norm_display or norm_display in norm_term):
                logger.debug("Guardrail [concept_consistency]: passed (display match)")
                return answer
        return _downgrade(
            answer, "concept_consistency",
            f"cannot verify '{question_term}' matches cited resources (no terminology candidates)",
        )

    cited_tokens = {e.code_token for e in cited_entries if e.system and e.code}

    if cited_tokens & term_candidates:
        logger.debug("Guardrail [concept_consistency]: passed (code match)")
        return answer

    return _downgrade(
        answer, "concept_consistency",
        f"'{question_term}' resolved to codes {term_candidates} but cited resources have {cited_tokens}",
    )


def _get_terminology_candidates(term: str) -> set[str] | None:
    """Get candidate code tokens for a term via terminology lookup."""
    try:
        from acp_writer.tools.concept_resolution import (
            ICD10_SYSTEM,
            LOINC_SYSTEM,
            RXNORM_SYSTEM,
            SNOMED_SYSTEM,
        )
        from acp_writer.tools.terminology_lookup import find_candidates
    except ImportError:
        return None

    candidates = set()
    for system in [LOINC_SYSTEM, SNOMED_SYSTEM, RXNORM_SYSTEM, ICD10_SYSTEM]:
        try:
            results = find_candidates(system, term, n=5)
            for r in results:
                if r.found and r.code:
                    candidates.add(f"{r.system}|{r.code}")
        except Exception:
            continue

    from acp_writer.tools.concept_resolver import resolve as resolve_concept
    resolved = resolve_concept(term)
    if resolved:
        if resolved.codes:
            candidates.update(resolved.codes)
        elif resolved.system and resolved.code:
            candidates.add(f"{resolved.system}|{resolved.code}")

    return candidates


@trace_safe(name="guardrail_absence_ledger")
def _check_absence_with_ledger(
    answer: QAAnswer,
    tool_ledger: list[dict] | None,
    question: str | None = None,
    inventory: BundleInventory | None = None,
) -> QAAnswer:
    """Validate absence answers against the tool-call ledger.

    A boolean absence answer is valid ONLY if:
    - The ledger records a definitive_miss for the concept, OR
    - On-demand pipeline resolution (D3) produces a definitive miss.

    A bare answer without evidence is downgraded (fail closed).
    """
    if tool_ledger is None:
        tool_ledger = []

    relevant = [
        e for e in tool_ledger
        if e.get("type") in ("check_condition", "check_medication", "check_allergy", "lookup_observation")
    ]

    has_definitive_miss = any(e.get("definitive_miss", False) for e in relevant)
    has_found = any(e.get("found", False) for e in relevant)

    if has_found and answer.value is False:
        logger.debug("Guardrail [absence_ledger]: ledger shows concept FOUND but answer is False — downgrade")
        return _downgrade(
            answer, "absence_ledger",
            "answer claims absence but tool ledger shows concept was found",
        )

    if has_definitive_miss:
        logger.debug("Guardrail [absence_ledger]: definitive_miss confirmed — absence valid")
        return answer

    if not relevant and isinstance(answer.value, bool) and answer.value is False and not answer.provenance:
        if question and inventory:
            on_demand = _on_demand_absence_check(question, inventory)
            if on_demand == "definitive_miss":
                logger.debug("Guardrail [absence_ledger]: on-demand pipeline confirms absence")
                answer.resolution_basis = "negative_evidence:on_demand_pipeline"
                return answer
            if on_demand == "present":
                return _downgrade(
                    answer, "absence_contradicted",
                    "on-demand pipeline found the concept present in the bundle",
                )
            return _downgrade(
                answer, "absence_ledger",
                "on-demand pipeline unresolved — fail closed",
            )

        logger.debug("Guardrail [absence_ledger]: no ledger, no pipeline — downgrade")
        return _downgrade(
            answer, "absence_ledger",
            "absence answer without ledger evidence (no tool calls recorded)",
        )

    if not has_definitive_miss and isinstance(answer.value, bool) and answer.value is False and not answer.provenance:
        logger.debug("Guardrail [absence_ledger]: no definitive_miss — downgrade")
        return _downgrade(
            answer, "absence_ledger",
            "absence answer without definitive_miss in ledger",
        )

    logger.debug("Guardrail [absence_ledger]: passed")
    return answer


def _on_demand_absence_check(question: str, inventory: BundleInventory) -> str:
    """Run the concept-resolution pipeline on demand for absence verification.

    Returns: "definitive_miss", "present", or "unresolved".
    """
    try:
        from acp_writer.tools.concept_resolution import resolve_concept_in_bundle
    except ImportError:
        return "unresolved"

    concept = _extract_asked_concept(question)
    if not concept:
        return "unresolved"

    for resource_kind in ["condition", "medication", "observation", "allergy"]:
        result = resolve_concept_in_bundle(concept, inventory, resource_kind, llm_client=None)
        if result.resolved:
            logger.debug("On-demand absence: concept '%s' found as %s", concept, resource_kind)
            return "present"
        if result.definitive_miss:
            continue

    logger.debug("On-demand absence: concept '%s' not found (deterministic-only)", concept)
    return "definitive_miss"


def _extract_asked_concept(question: str) -> str:
    """Extract the clinical concept from a question (simple heuristic)."""
    import re
    q = question.strip().rstrip("?").strip()
    for prefix in [
        r"^(?:is|are|does|do|has|have|should|was|were|can|could|would|will)\s+(?:the\s+)?patient(?:'s?)?\s+(?:on\s+|have\s+|missing\s+|lacking\s+|currently\s+on\s+)?",
        r"^(?:is|are|does|do)\s+(?:there\s+)?(?:any\s+)?",
    ]:
        m = re.match(prefix, q, re.IGNORECASE)
        if m:
            return q[m.end():].strip()
    return q


@trace_safe(name="guardrail_conflict_enforcement")
def _check_conflict(
    answer: QAAnswer,
    bundle: dict,
    inventory: BundleInventory,
) -> QAAnswer:
    """Downgrade answers based on conflicting data.

    Applies to ALL answer classes. Checks all observations sharing a code
    with any cited observation for conflicting values.
    """
    if not answer.provenance:
        logger.debug("Guardrail [conflict]: skipped (no provenance)")
        return answer

    cited_obs = [
        e for e in inventory.entries
        if e.fhir_reference in answer.provenance and e.resource_type == "Observation"
    ]

    if not cited_obs:
        logger.debug("Guardrail [conflict]: skipped (no cited observations)")
        return answer

    for cited in cited_obs:
        if not cited.system or not cited.code:
            continue

        siblings = [
            e for e in inventory.entries
            if e.resource_type == "Observation"
            and e.code_token == cited.code_token
            and e.fhir_reference != cited.fhir_reference
        ]

        if not siblings:
            continue

        cited_val = _get_obs_value(cited.fhir_reference, bundle)
        for sib in siblings:
            sib_val = _get_obs_value(sib.fhir_reference, bundle)
            if cited_val is not None and sib_val is not None and abs(cited_val - sib_val) > 0.01:
                return _downgrade(
                    answer, "conflict_enforcement",
                    f"conflicting values for {cited.code_token}: "
                    f"{cited.fhir_reference}={cited_val} vs {sib.fhir_reference}={sib_val}",
                )

    logger.debug("Guardrail [conflict]: passed")
    return answer


def _get_obs_value(ref: str, bundle: dict) -> float | None:
    """Extract the numeric value from an observation by reference."""
    for resource, entry in _get_resources_with_entry(bundle, "Observation"):
        if _resource_ref(resource, entry) != ref:
            continue
        vq = resource.get("valueQuantity")
        if vq and vq.get("value") is not None:
            try:
                return float(vq["value"])
            except (ValueError, TypeError):
                pass
        for comp in resource.get("component", []):
            cvq = comp.get("valueQuantity")
            if cvq and cvq.get("value") is not None:
                try:
                    return float(cvq["value"])
                except (ValueError, TypeError):
                    pass
    return None


def check_definitive_miss(
    tool_ledger: list[dict],
    answer: QAAnswer,
) -> QAAnswer:
    """Mechanical refusal: if every resolution for the asked concept ended in
    definitive_miss and the answer claims presence, override with refusal.

    Called at the choke point AFTER verify_answer for agent-answered booleans.
    """
    if answer.insufficient_data:
        return answer

    if not isinstance(answer.value, bool):
        return answer

    if not tool_ledger:
        return answer

    relevant_calls = [
        entry for entry in tool_ledger
        if entry.get("type") in ("check_condition", "check_medication", "check_allergy", "lookup_observation")
    ]

    if not relevant_calls:
        return answer

    all_misses = all(entry.get("definitive_miss", False) for entry in relevant_calls)

    if all_misses and answer.value is True:
        logger.info(
            "Mechanical refusal: agent claims presence but all %d tool calls returned definitive_miss",
            len(relevant_calls),
        )
        return QAAnswer(
            value=False,
            kind="boolean",
            provenance=[],
            insufficient_data=False,
            error="mechanical_refusal:definitive_miss",
            answered_by="guardrail_downgrade",
            resolution_basis="mechanical_refusal",
        )

    return answer
