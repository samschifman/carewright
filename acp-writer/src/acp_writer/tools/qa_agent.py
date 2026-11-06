"""LLM agent for complex clinical QA.

Uses LangGraph's prebuilt ReAct agent with concept-based tools.
Tools accept clinical TERMS (not system+code) and delegate to the
concept-resolution pipeline for open-vocabulary matching.

Tools are built as closures per call — no module-level mutable state.
"""

import json
import logging
from datetime import date
from typing import Any

from acp_writer.llm_json import strip_code_fence
from acp_writer.tools.bundle_inventory import BundleInventory, build_bundle_inventory
from acp_writer.tools.concept_resolution import resolve_concept_in_bundle
from acp_writer.tools.ips_extractor import (
    extract_observation_concept,
    extract_patient_age,
)
from acp_writer.tools.ips_serializer import serialize_ips
from acp_writer.tools.temporal_index import build_temporal_index
from acp_writer.tools.temporal_queries import (
    observation_count,
    rate_of_change,
)
from cpg_contracts.tracing import trace_safe
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_AGENT_SYSTEM_PROMPT = """You are a clinical data analyst answering factual questions about a patient.

You have tools that accept clinical TERMS (not codes). The tools handle terminology
resolution internally — you don't need to know SNOMED, LOINC, or RxNorm codes.

Available tools:
- check_condition(term): Check if patient has a condition. Use clinical terms like
  "diabetes", "hypothyroidism", "heart failure", "acid reflux", etc.
- check_medication(term): Check if patient is on a medication or drug class.
  Use terms like "metformin", "ACE inhibitor", "statin", "thyroid medication", etc.
- lookup_observation(term): Get the most recent value of an observation.
  Use terms like "blood pressure", "HbA1c", "TSH", "potassium", etc.
- check_allergy(term): Check if patient has an allergy.
- get_patient_age(): Get patient's age in years.
- find_code(system, text): Look up a code in a terminology system.
- verify_code(system, code): Verify a code exists and get its display name.

Tool results include what was searched and how it matched (by code, display text,
or terminology lookup). On a miss, you'll see what IS in the patient's record
for that resource type — use this to refine your search.

Clinical reasoning guidelines:
- Apply standard medical knowledge about drug classes, treatment targets, and risk factors
- For drug class questions, use the medication term (e.g., "proton pump inhibitor")
- For negation/absence: only answer "absent" if the tool confirms a definitive miss
- When data is genuinely ambiguous or conflicting, say insufficient_data

Respond with a JSON object:
{"answer": <value>, "provenance": [<fhir_references>], "insufficient_data": false, "reasoning": "brief"}"""


def _build_tools(
    bundle: dict,
    inventory: BundleInventory,
    index: Any,
    reference_date: date,
    llm_client: Any,
) -> list:
    """Build tool list with per-call state captured in closures."""

    @tool
    def check_condition(term: str) -> str:
        """Check if the patient has a condition matching a clinical term.

        Use natural clinical language — no need for codes.
        Examples: "diabetes", "hypothyroidism", "heart failure", "acid reflux",
        "thyroid disorder", "kidney disease", "high blood pressure"

        Returns what was searched, what matched, and on a miss, lists all
        conditions in the patient's record so you can refine your search.
        """
        result = resolve_concept_in_bundle(term, inventory, "condition", llm_client=llm_client)

        if result.resolved:
            active = [e for e in result.entries
                      if (e.status or "active") not in ("resolved", "inactive", "remission")]
            if active:
                entry = active[0]
                return json.dumps({
                    "found": True,
                    "display": entry.display,
                    "code": entry.code_token,
                    "reference": entry.fhir_reference,
                    "status": entry.status,
                    "match_basis": result.match_basis,
                    "steps_run": result.steps_run,
                })
            inactive_entry = result.entries[0]
            return json.dumps({
                "found": False,
                "note": f"Found {inactive_entry.display} but status={inactive_entry.status} (not active)",
                "match_basis": result.match_basis,
            })

        conditions = inventory.conditions()
        alternatives = [f"{e.display} [{e.system.rsplit('/', 1)[-1] if e.system else 'text'} {e.code}] ({e.status or 'active'})"
                       for e in conditions[:10]]
        return json.dumps({
            "found": False,
            "definitive_miss": result.definitive_miss,
            "steps_run": result.steps_run,
            "note": f"No match for '{term}'. Patient's conditions: {'; '.join(alternatives)}" if alternatives else f"No match for '{term}'. No conditions in record.",
        })

    @tool
    def check_medication(term: str) -> str:
        """Check if the patient is on a medication or drug class.

        Use natural terms — drug names or class names work.
        Examples: "metformin", "ACE inhibitor", "statin", "thyroid medication",
        "proton pump inhibitor", "blood pressure medication", "anticoagulant"

        Returns match details or a list of patient's medications on miss.
        """
        result = resolve_concept_in_bundle(term, inventory, "medication", llm_client=llm_client)

        if result.resolved:
            active = [e for e in result.entries
                      if (e.status or "active") not in ("cancelled", "entered-in-error", "stopped")]
            if active:
                entry = active[0]
                return json.dumps({
                    "found": True,
                    "display": entry.display or entry.text,
                    "code": entry.code_token if entry.system else "free-text",
                    "reference": entry.fhir_reference,
                    "match_basis": result.match_basis,
                    "steps_run": result.steps_run,
                })
            inactive_entry = result.entries[0]
            return json.dumps({
                "found": False,
                "note": f"Found {inactive_entry.display or inactive_entry.text} but status={inactive_entry.status} (not active)",
                "match_basis": result.match_basis,
            })

        meds = inventory.medications()
        alternatives = [f"{e.display or e.text} [{e.system.rsplit('/', 1)[-1] if e.system else 'text'}]"
                       for e in meds[:10]]
        return json.dumps({
            "found": False,
            "definitive_miss": result.definitive_miss,
            "steps_run": result.steps_run,
            "note": f"No match for '{term}'. Patient's medications: {'; '.join(alternatives)}" if alternatives else f"No match for '{term}'. No medications in record.",
        })

    @tool
    def lookup_observation(term: str) -> str:
        """Look up the most recent value of an observation.

        Use natural terms — no need for LOINC codes.
        Examples: "blood pressure", "HbA1c", "TSH", "potassium",
        "fasting glucose", "eGFR", "LDL cholesterol", "BMI"

        Returns the value, unit, date, and match details, or lists
        available observations on miss.
        """
        result = resolve_concept_in_bundle(term, inventory, "observation", llm_client=llm_client)

        if result.resolved:
            entry = result.entries[0]
            code_tokens = [entry.code_token] if entry.system else None
            display_terms = [entry.display] if entry.display else None
            obs_result = extract_observation_concept(bundle, code_tokens=code_tokens, display_terms=display_terms)
            if obs_result.found:
                return json.dumps({
                    "found": True,
                    "value": obs_result.value,
                    "unit": obs_result.unit,
                    "date": obs_result.date,
                    "reference": obs_result.fhir_reference,
                    "display": entry.display,
                    "match_basis": result.match_basis,
                })

        obs = inventory.observations()
        seen = set()
        alternatives = []
        for e in obs:
            key = e.display or e.code
            if key not in seen:
                seen.add(key)
                alternatives.append(f"{e.display} [{e.system.rsplit('/', 1)[-1] if e.system else '?'} {e.code}]")
            if len(alternatives) >= 15:
                break

        return json.dumps({
            "found": False,
            "steps_run": result.steps_run if result else [],
            "note": f"No match for '{term}'. Available observations: {'; '.join(alternatives)}" if alternatives else f"No observation found for '{term}'.",
        })

    @tool
    def check_allergy(term: str) -> str:
        """Check if the patient has an allergy.

        Examples: "penicillin", "sulfonamide", "ACE inhibitor allergy"
        """
        result = resolve_concept_in_bundle(term, inventory, "allergy", llm_client=llm_client)
        if result.resolved:
            entry = result.entries[0]
            return json.dumps({"found": True, "display": entry.display, "reference": entry.fhir_reference})

        allergies = inventory.allergies()
        alternatives = [e.display for e in allergies[:5]]
        return json.dumps({
            "found": False,
            "note": f"No allergy match for '{term}'. Patient allergies: {'; '.join(alternatives)}" if alternatives else "No allergies recorded.",
        })

    @tool
    def get_patient_age() -> str:
        """Get the patient's current age in years."""
        result = extract_patient_age(bundle, reference_date)
        return json.dumps(result.to_dict())

    @tool
    def find_code(system: str, text: str) -> str:
        """Look up a clinical code in a terminology system.

        Args:
            system: "snomed", "rxnorm", "loinc", or "icd10"
            text: The clinical term to look up
        """
        system_map = {
            "snomed": "http://snomed.info/sct",
            "rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "loinc": "http://loinc.org",
            "icd10": "http://hl7.org/fhir/sid/icd-10-cm",
        }
        full_system = system_map.get(system.lower(), system)
        from acp_writer.tools.terminology_lookup import find
        result = find(full_system, text)
        return json.dumps(result.to_dict())

    @tool
    def verify_code(system: str, code: str) -> str:
        """Verify a code exists in a terminology system and get its display name.

        Args:
            system: "snomed", "rxnorm", "loinc", or "icd10"
            code: The code to verify
        """
        system_map = {
            "snomed": "http://snomed.info/sct",
            "rxnorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "loinc": "http://loinc.org",
            "icd10": "http://hl7.org/fhir/sid/icd-10-cm",
        }
        full_system = system_map.get(system.lower(), system)
        from acp_writer.tools.terminology_lookup import verify
        result = verify(full_system, code)
        return json.dumps(result.to_dict())

    @tool
    def count_observations_in_window(term: str, duration: str,
                                      threshold: float | None = None,
                                      comparator: str | None = None) -> str:
        """Count observations matching a clinical term in a time window.

        Args:
            term: Clinical term (e.g., "systolic blood pressure")
            duration: ISO 8601 duration (e.g., "P3M" for 3 months)
            threshold: Optional numeric threshold
            comparator: Optional comparator: "ge", "gt", "le", "lt", "eq"
        """
        resolution = resolve_concept_in_bundle(term, inventory, "observation", llm_client=llm_client)
        if not resolution.resolved:
            return json.dumps({"found": False, "note": f"Could not resolve '{term}' to an observation code"})

        code_token = resolution.entries[0].code_token
        result = observation_count(index, code_token, duration, reference_date, threshold, comparator)
        return json.dumps({"found": result.found, "value": result.value,
                          "provenance": result.provenance,
                          "insufficient_data": result.insufficient_data})

    @tool
    def get_observation_trend(term: str, duration: str) -> str:
        """Compute the rate of change of an observation over time.

        Args:
            term: Clinical term (e.g., "eGFR")
            duration: Time window (e.g., "P1Y" for 1 year)

        Returns slope normalized per year. Negative = declining.
        """
        resolution = resolve_concept_in_bundle(term, inventory, "observation", llm_client=llm_client)
        if not resolution.resolved:
            return json.dumps({"found": False, "note": f"Could not resolve '{term}'"})

        code_token = resolution.entries[0].code_token
        result = rate_of_change(index, code_token, duration, reference_date)
        return json.dumps({"found": result.found, "value": result.value,
                          "provenance": result.provenance,
                          "insufficient_data": result.insufficient_data})

    return [
        check_condition, check_medication, lookup_observation,
        check_allergy, get_patient_age, find_code, verify_code,
        count_observations_in_window, get_observation_trend,
    ]


@trace_safe(name="qa_agent_answer")
def agent_answer(
    question: str,
    bundle: dict,
    reference_date: date,
    llm_client: Any,
    max_iterations: int = 10,
    extra_context: str | None = None,
) -> dict:
    """Run the ReAct QA agent to answer a clinical question."""
    inventory = build_bundle_inventory(bundle)
    index = build_temporal_index(bundle)
    tools = _build_tools(bundle, inventory, index, reference_date, llm_client)

    condensed = serialize_ips(bundle)
    inventory_text = inventory.render_for_llm()

    context_parts = [
        f"Patient summary:\n{condensed}",
        f"Bundle inventory:\n{inventory_text}",
        f"Reference date: {reference_date.isoformat()}",
    ]
    if extra_context:
        context_parts.append(f"Context from prior retrieval: {extra_context}")
    context_parts.append(
        f"Question: {question}\n\n"
        f"Use the tools to answer. Respond with JSON containing "
        f"answer, provenance, insufficient_data, and reasoning."
    )

    try:
        agent = create_react_agent(
            llm_client,
            tools,
            prompt=SystemMessage(content=_AGENT_SYSTEM_PROMPT),
        )

        result = agent.invoke(
            {"messages": [HumanMessage(content="\n\n".join(context_parts))]},
            {"recursion_limit": max_iterations * 2},
        )

        last_message = result["messages"][-1]
        parsed = _parse_agent_response(last_message)
        parsed["tool_ledger"] = _extract_tool_ledger(result["messages"])
        return parsed

    except Exception as exc:
        logger.warning('QA agent failed exception_type=%s (details redacted)', type(exc).__name__)
        return {"answer": None, "provenance": [], "insufficient_data": True,
                "error": str(exc), "tool_ledger": []}


def _extract_tool_ledger(messages: list) -> list[dict]:
    """Extract definitive_miss signals from agent tool-call history."""
    ledger = []
    for msg in messages:
        if not hasattr(msg, "content") or not hasattr(msg, "name"):
            continue
        name = getattr(msg, "name", None)
        if not name:
            continue
        content = msg.content
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        try:
            data = json.loads(content)
            ledger.append({
                "type": name,
                "found": data.get("found", False),
                "definitive_miss": data.get("definitive_miss", False),
            })
        except (json.JSONDecodeError, TypeError):
            pass
    return ledger


def _parse_agent_response(message: Any) -> dict:
    """Parse the agent's final message into a structured answer."""
    content = message.content if hasattr(message, "content") else str(message)
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    content = content.strip()

    if "INSUFFICIENT_DATA" in content.upper():
        return {"answer": None, "provenance": [], "insufficient_data": True}

    content = strip_code_fence(content)

    try:
        parsed = json.loads(content)
        return {
            "answer": parsed.get("answer"),
            "provenance": parsed.get("provenance", []),
            "insufficient_data": parsed.get("insufficient_data", False),
        }
    except json.JSONDecodeError:
        if content.lower() in ("true", "false"):
            return {"answer": content.lower() == "true", "provenance": [], "insufficient_data": False}
        try:
            return {"answer": float(content), "provenance": [], "insufficient_data": False}
        except ValueError:
            return {"answer": content, "provenance": [], "insufficient_data": False}
