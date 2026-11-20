"""Concept-resolution pipeline — open-vocabulary clinical concept resolution.

Resolves clinical terms to bundle entries through a cascade:
1. Concept map cache (deterministic, instant)
2. Terminology server candidates (network, cached 30 days)
3. Inventory code/display match (deterministic)
4. LLM inventory match (open-vocabulary fallback)

A "definitive miss" from this pipeline means cache, terminology,
AND LLM all failed — making "the patient does not have X" safe.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from acp_writer.tools.bundle_inventory import BundleInventory, InventoryEntry
from acp_writer.tools.concept_resolver import resolve as resolve_concept
from acp_writer.tools.ips_extractor import _normalize_display
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)

SNOMED_SYSTEM = "http://snomed.info/sct"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC_SYSTEM = "http://loinc.org"

_CONDITION_SYSTEMS = [SNOMED_SYSTEM, ICD10_SYSTEM]
_MEDICATION_SYSTEMS = [RXNORM_SYSTEM]
_OBSERVATION_SYSTEMS = [LOINC_SYSTEM]


@dataclass
class ResolutionResult:
    """Result of the concept-resolution pipeline."""
    resolved: bool
    entries: list[InventoryEntry] = field(default_factory=list)
    match_basis: str = ""
    unresolved: bool = False
    definitive_miss: bool = False
    codes_tried: list[str] = field(default_factory=list)
    steps_run: list[str] = field(default_factory=list)


def _match_inventory_by_codes(
    inventory_entries: list[InventoryEntry],
    code_tokens: list[str],
) -> list[InventoryEntry]:
    """Match inventory entries by code tokens, with ICD-10 prefix support."""
    matches = []
    for entry in inventory_entries:
        for token in code_tokens:
            if "|" not in token:
                continue
            t_sys, t_code = token.rsplit("|", 1)
            if entry.system == t_sys and entry.code == t_code:
                matches.append(entry)
                break
            if entry.system == t_sys and (
                entry.code.startswith(t_code + ".") or t_code.startswith(entry.code + ".")
            ):
                matches.append(entry)
                break
    return matches


def _match_inventory_by_display(
    inventory_entries: list[InventoryEntry],
    terms: list[str],
) -> list[InventoryEntry]:
    """Match inventory entries by normalized display text."""
    matches = []
    norm_terms = [_normalize_display(t) for t in terms]
    for entry in inventory_entries:
        for text in [entry.display, entry.text]:
            if not text:
                continue
            norm = _normalize_display(text)
            for norm_term in norm_terms:
                if norm_term in norm or norm in norm_term:
                    matches.append(entry)
                    break
            else:
                continue
            break
    return matches


@trace_safe(name="concept_resolve")
def resolve_concept_in_bundle(
    term: str,
    inventory: BundleInventory,
    resource_kind: str = "condition",
    llm_client: Any = None,
) -> ResolutionResult:
    """Resolve a clinical term against a bundle's inventory.

    Args:
        term: Clinical term (e.g., "thyroid disorder", "blood pressure medication")
        inventory: The bundle's code inventory from build_bundle_inventory
        resource_kind: "condition", "medication", "observation", or "allergy"
        llm_client: Optional LLM client for open-vocabulary fallback (None = deterministic only)

    Returns:
        ResolutionResult with matched entries and resolution metadata
    """
    steps = []
    codes_tried = []

    if resource_kind == "condition":
        inv_entries = inventory.conditions()
        systems = _CONDITION_SYSTEMS
    elif resource_kind == "medication":
        inv_entries = inventory.medications()
        systems = _MEDICATION_SYSTEMS
    elif resource_kind == "observation":
        inv_entries = inventory.observations()
        systems = _OBSERVATION_SYSTEMS
    elif resource_kind == "allergy":
        inv_entries = inventory.allergies()
        systems = [SNOMED_SYSTEM]
    else:
        inv_entries = inventory.entries
        systems = _CONDITION_SYSTEMS + _MEDICATION_SYSTEMS + _OBSERVATION_SYSTEMS

    # Step 1: Concept map cache
    steps.append("cache")
    resolved = resolve_concept(term)
    if resolved and resolved.codes:
        codes_tried.extend(resolved.codes)
        matches = _match_inventory_by_codes(inv_entries, resolved.codes)
        if matches:
            return ResolutionResult(
                resolved=True, entries=matches, match_basis="cache",
                codes_tried=codes_tried, steps_run=steps,
            )
    elif resolved and resolved.system and resolved.code:
        token = f"{resolved.system}|{resolved.code}"
        codes_tried.append(token)
        matches = _match_inventory_by_codes(inv_entries, [token])
        if matches:
            return ResolutionResult(
                resolved=True, entries=matches, match_basis="cache",
                codes_tried=codes_tried, steps_run=steps,
            )

    # Step 2: Terminology server candidates
    steps.append("terminology")
    try:
        from acp_writer.tools.terminology_lookup import find_candidates

        for system in systems:
            candidates = find_candidates(system, term, n=5)
            for candidate in candidates:
                if candidate.found and candidate.code:
                    token = f"{candidate.system}|{candidate.code}"
                    codes_tried.append(token)

            candidate_tokens = [
                f"{c.system}|{c.code}" for c in candidates if c.found and c.code
            ]
            if candidate_tokens:
                matches = _match_inventory_by_codes(inv_entries, candidate_tokens)
                if matches:
                    _log_learned_mapping(term, matches, "terminology")
                    return ResolutionResult(
                        resolved=True, entries=matches, match_basis="terminology",
                        codes_tried=codes_tried, steps_run=steps,
                    )
    except Exception as exc:
        logger.warning('Terminology lookup failed exception_type=%s (details redacted)', type(exc).__name__)

    # Step 3: Display-text matching
    steps.append("display_text")
    display_terms = [term]
    if resolved and resolved.code:
        display_terms.append(resolved.code)
    matches = _match_inventory_by_display(inv_entries, display_terms)
    if matches:
        _log_learned_mapping(term, matches, "display_text")
        return ResolutionResult(
            resolved=True, entries=matches, match_basis="display_text",
            codes_tried=codes_tried, steps_run=steps,
        )

    # Step 4: LLM inventory match (open-vocabulary fallback)
    if llm_client is not None:
        steps.append("llm_inventory")
        matches = _llm_inventory_match(term, inv_entries, resource_kind, llm_client)
        if matches is None:
            steps.append("llm_inventory:failed")
            return ResolutionResult(
                resolved=False, unresolved=True,
                codes_tried=codes_tried, steps_run=steps,
            )
        if matches:
            _log_learned_mapping(term, matches, "llm_inventory")
            return ResolutionResult(
                resolved=True, entries=matches, match_basis="llm_inventory",
                codes_tried=codes_tried, steps_run=steps,
            )

        return ResolutionResult(
            resolved=False, definitive_miss=True,
            codes_tried=codes_tried, steps_run=steps,
        )

    return ResolutionResult(
        resolved=False, unresolved=True,
        codes_tried=codes_tried, steps_run=steps,
    )


@trace_safe(name="llm_inventory_match")
def _llm_inventory_match(
    term: str,
    inventory_entries: list[InventoryEntry],
    resource_kind: str,
    llm_client: Any,
) -> list[InventoryEntry]:
    """Use an LLM to match a clinical term against inventory entries."""
    from pydantic import BaseModel, Field

    class InventoryMatch(BaseModel):
        matched_references: list[str] = Field(
            default_factory=list,
            description="FHIR references of inventory entries that match the clinical term. Empty if none match."
        )
        reasoning: str = Field(default="", description="Brief explanation")

    entries_text = "\n".join(
        f"- {entry.fhir_reference}: {entry.display or entry.text or entry.code} "
        f"[{entry.system.rsplit('/', 1)[-1] if entry.system else 'text'} {entry.code}] "
        f"({entry.status or 'active'})"
        for entry in inventory_entries[:30]
    )

    prompt = (
        f"Clinical term to find: \"{term}\"\n\n"
        f"Available {resource_kind} entries in the patient's record:\n{entries_text}\n\n"
        f"Which entries match this clinical term? Consider synonyms, "
        f"related concepts, and different terminology systems. "
        f"Return the FHIR references of matching entries, or empty if none match."
    )

    try:
        structured_llm = llm_client.with_structured_output(InventoryMatch)
        result = structured_llm.invoke([
            {"role": "system", "content": "You are a clinical terminology expert matching clinical terms to coded entries in a patient record."},
            {"role": "user", "content": prompt},
        ])

        ref_set = set(result.matched_references)
        return [e for e in inventory_entries if e.fhir_reference in ref_set]

    except Exception as exc:
        logger.warning('LLM inventory match failed exception_type=%s (details redacted)', type(exc).__name__)
        return None


def _log_learned_mapping(
    term: str,
    matches: list[InventoryEntry],
    basis: str,
) -> None:
    """Append a learned resolution to the JSONL log."""
    try:
        log_path = Path("working/learned-concept-mappings.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "term": term,
            "codes": [e.code_token for e in matches if e.system],
            "displays": [e.display for e in matches if e.display],
            "match_basis": basis,
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
