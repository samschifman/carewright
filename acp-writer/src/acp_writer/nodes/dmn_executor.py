"""DMN Executor — resolve inputs via concept pipeline, evaluate via client.

The resolution loop interleaves with evaluation: chained DMN models'
outputs feed later models' inputs. The evaluation client abstracts
in-process (monolith) vs HTTP (decision-engine pod) execution.
"""

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

import mlflow
from pydantic import ValidationError

from acp_writer.state import CarePlanComposerState
from acp_writer.tools.dmn_evaluation import (
    DmnEngineError,
    DmnEvaluationClient,
    ModelNotDeployed,
    get_evaluation_client,
)
from acp_writer.tools.ips_extractor import (
    extract_allergy,
    extract_condition,
    extract_medication,
    extract_observation,
    extract_observation_concept,
    extract_patient_age,
)
from acp_writer.tools.temporal_index import TemporalIndex, build_temporal_index
from acp_writer.tools.temporal_queries import (
    consecutive_above,
    cross_resource_temporal,
    observation_count,
    observations_in_window,
    rate_of_change,
)
from cpg_contracts import Extraction

logger = logging.getLogger(__name__)

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"

_CONDITION_SYSTEMS = {SNOMED, "http://hl7.org/fhir/sid/icd-10-cm"}
_MEDICATION_SYSTEMS = {RXNORM}

_INACTIVE_CONDITION_STATUSES = {"resolved", "inactive", "remission"}
_INACTIVE_MEDICATION_STATUSES = {"cancelled", "entered-in-error", "stopped"}
_INACTIVE_ALLERGY_STATUSES = {"resolved", "inactive"}
def _filter_active_entries(entries: list, inactive_statuses: set) -> list:
    """Filter inventory entries to those with active (or absent) status."""
    return [e for e in entries if (e.status or "active") not in inactive_statuses]


def _infer_resource_kind(var_name: str, var_type: str) -> str:
    """Infer FHIR resource kind from DMN variable name and type."""
    from acp_writer.tools.concept_resolver import resolve as resolve_concept
    resolved = resolve_concept(var_name)
    if resolved and resolved.action in ("extract_medication", "extract_drug_class"):
        return "medication"
    if resolved and resolved.action == "extract_allergy":
        return "allergy"
    if resolved and resolved.action == "extract_observation":
        return "observation"

    key = var_name.lower()
    if var_type.lower() == "boolean":
        if any(kw in key for kw in ("medication", "drug", "med ", "on ")):
            return "medication"
        if any(kw in key for kw in ("allergy", "allergic")):
            return "allergy"
        return "condition"
    return "observation"


def _try_extract_by_code(
    ips_bundle: dict, system: str, code: str, var_type: str,
) -> tuple[Any, str | None]:
    """Try extracting a value from the IPS using a system|code pair."""
    is_boolean = var_type.lower() == "boolean" if var_type else False

    if is_boolean and system in _CONDITION_SYSTEMS:
        result = extract_condition(ips_bundle, system, code)
        return result.found, result.fhir_reference

    if is_boolean and system in _MEDICATION_SYSTEMS:
        result = extract_medication(ips_bundle, system, code)
        return result.found, result.fhir_reference

    result = extract_observation(ips_bundle, system, code)
    if result.found:
        return result.value, result.fhir_reference

    if not is_boolean:
        result = extract_condition(ips_bundle, system, code)
        if result.found:
            return result.found, result.fhir_reference

    return None, None


@mlflow.trace(name="dmn_extract_via_pipeline")
def _extract_via_pipeline(
    ips_bundle: dict, var_name: str, var_type: str,
    inventory: Any, llm_client: Any, reference_date: str | None = None,
) -> tuple[Any, str | None, dict]:
    """Extract a DMN input value using the concept-resolution pipeline."""
    from acp_writer.tools.concept_resolution import resolve_concept_in_bundle

    resource_kind = _infer_resource_kind(var_name, var_type)
    audit = {"match_basis": None, "steps_run": [], "degraded": False}

    try:
        resolution = resolve_concept_in_bundle(
            var_name, inventory, resource_kind, llm_client=llm_client,
        )
    except Exception as exc:
        logger.warning("Pipeline resolution failed for '%s': %s", var_name, exc)
        audit["degraded"] = True
        audit["error"] = str(exc)
        return None, None, audit

    audit["match_basis"] = resolution.match_basis
    audit["steps_run"] = resolution.steps_run

    if not resolution.resolved:
        if resolution.definitive_miss and var_type.lower() == "boolean":
            audit["match_basis"] = "definitive_miss"
            return False, None, audit
        if resolution.unresolved:
            audit["degraded"] = True
        return None, None, audit

    entry = resolution.entries[0]

    if resource_kind == "observation":
        code_tokens = [entry.code_token] if entry.system else None
        display_terms = [entry.display] if (entry.display and not code_tokens) else None
        obs_result = extract_observation_concept(
            ips_bundle, code_tokens=code_tokens, display_terms=display_terms,
        )
        if obs_result.found:
            return obs_result.value, obs_result.fhir_reference, audit
        return None, None, audit

    if resource_kind == "condition":
        active = _filter_active_entries(resolution.entries, _INACTIVE_CONDITION_STATUSES)
        if active:
            return True, active[0].fhir_reference, audit
        audit["note"] = f"matched but inactive: {entry.fhir_reference} ({entry.status})"
        return None, None, audit

    if resource_kind == "medication":
        active = _filter_active_entries(resolution.entries, _INACTIVE_MEDICATION_STATUSES)
        if active:
            return True, active[0].fhir_reference, audit
        audit["note"] = f"matched but inactive: {entry.fhir_reference} ({entry.status})"
        return None, None, audit

    if resource_kind == "allergy":
        active = _filter_active_entries(resolution.entries, _INACTIVE_ALLERGY_STATUSES)
        if active:
            return True, active[0].fhir_reference, audit
        audit["note"] = f"matched but inactive: {entry.fhir_reference} ({entry.status})"
        return None, None, audit

    return None, None, audit


@mlflow.trace(name="dmn_extract_temporal")
def _extract_temporal(
    ips_bundle: dict,
    extraction: dict,
    temporal_index: TemporalIndex,
    reference_date: str | date | None,
) -> tuple[Any, list[str], dict]:
    """Execute an explicitly annotated temporal extraction."""
    try:
        contract = Extraction.model_validate(extraction)
    except ValidationError as exc:
        return None, [], {
            "match_basis": "decision_variable_extraction",
            "extraction": extraction,
            "degraded": True,
            "error": exc.errors(),
        }
    function = contract.function
    params = contract.params
    audit = {
        "match_basis": "decision_variable_extraction",
        "extraction": extraction,
    }
    if isinstance(reference_date, str):
        try:
            resolved_date = date.fromisoformat(reference_date[:10])
        except ValueError:
            audit["degraded"] = True
            audit["error"] = f"Invalid reference date: {reference_date}"
            return None, [], audit
    else:
        resolved_date = reference_date or datetime.now(timezone.utc).date()

    code = params.get("code", "")
    try:
        if function == "observations_in_window":
            result = observations_in_window(
                temporal_index, code, params.get("duration", "P12M"), resolved_date,
            )
        elif function == "observation_count":
            result = observation_count(
                temporal_index, code, params.get("duration", "P12M"), resolved_date,
                threshold=params.get("threshold"), comparator=params.get("comparator"),
            )
        elif function == "consecutive_above":
            result = consecutive_above(
                temporal_index, code, params.get("threshold", 0), resolved_date,
            )
        elif function == "rate_of_change":
            result = rate_of_change(
                temporal_index, code, params.get("duration", "P1Y"), resolved_date,
            )
        else:
            result = cross_resource_temporal(
                temporal_index, ips_bundle, params.get("anchor_code", ""),
                params.get("target_code", ""), params.get("window", "P14D"),
            )
    except (TypeError, ValueError, KeyError) as exc:
        audit["degraded"] = True
        audit["error"] = str(exc)
        return None, [], audit

    audit["data_quality"] = result.data_quality
    audit["insufficient_data"] = result.insufficient_data
    return result.value, result.provenance, audit


@mlflow.trace(name="dmn_resolve_input")
def _extract_input_value(
    ips_bundle: dict, var_name: str, var_type: str,
    prior_results: dict[str, dict],
    codes: list[str] | None = None,
    reference_date: str | None = None,
    inventory: Any = None, llm_client: Any = None,
    extraction: dict | None = None,
    temporal_index: TemporalIndex | None = None,
) -> tuple[Any, str | list[str] | None, dict]:
    """Extract a DMN input value — prior results, codes, then pipeline."""
    key = re.sub(r"([a-z])([A-Z])", r"\1 \2", var_name).lower().strip()
    audit: dict[str, Any] = {}

    if extraction:
        temporal_index = temporal_index or build_temporal_index(ips_bundle)
        value, ref, temporal_audit = _extract_temporal(
            ips_bundle, extraction, temporal_index, reference_date,
        )
        return value, ref, temporal_audit

    for model_output in prior_results.values():
        for decision_name, decision_val in model_output.items():
            if isinstance(decision_val, dict):
                for field_name, field_val in decision_val.items():
                    if field_name.lower() == key:
                        return field_val, None, {"match_basis": "prior_dmn"}
                    composite = f"{decision_name} {field_name}".lower()
                    if composite == key or field_name.lower() in key:
                        return field_val, None, {"match_basis": "prior_dmn"}
            elif decision_name.lower() == key:
                return decision_val, None, {"match_basis": "prior_dmn"}

    if codes:
        for code_token in codes:
            if "|" in code_token:
                system, code = code_token.rsplit("|", 1)
                value, ref = _try_extract_by_code(ips_bundle, system, code, var_type)
                if value is not None:
                    return value, ref, {"match_basis": "decision_variable_codes"}

    if inventory is not None:
        value, ref, audit = _extract_via_pipeline(
            ips_bundle, var_name, var_type, inventory, llm_client, reference_date,
        )
        if value is not None:
            return value, ref, audit
        if audit.get("match_basis") == "definitive_miss":
            return False, None, audit

    logger.warning("Could not extract value for DMN input: %s (type: %s)", var_name, var_type)
    return None, None, {"match_basis": None, "degraded": audit.get("degraded", False)}


@mlflow.trace(name="dmn_resolve_inputs")
def resolve_inputs(
    expected_inputs: list[dict],
    ips_bundle: dict,
    inventory: Any,
    prior_results: dict[str, dict],
    llm_client: Any = None,
    reference_date: str | None = None,
    temporal_index: TemporalIndex | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, dict]]:
    """Resolve all inputs for one DMN model.

    Returns (inputs_dict, fhir_refs, input_audit).
    """
    inputs: dict[str, Any] = {}
    fhir_refs: list[str] = []
    input_audit: dict[str, dict] = {}

    for var in expected_inputs:
        value, ref, var_audit = _extract_input_value(
            ips_bundle, var["name"], var.get("type", "string"), prior_results,
            codes=var.get("codes"),
            reference_date=reference_date,
            inventory=inventory,
            llm_client=llm_client,
            extraction=var.get("extraction"),
            temporal_index=temporal_index,
        )
        if value is not None:
            inputs[var["name"]] = value
        if isinstance(ref, list):
            fhir_refs.extend(ref)
        elif ref:
            fhir_refs.append(ref)
        input_audit[var["name"]] = var_audit

    missing = [v["name"] for v in expected_inputs if v["name"] not in inputs]
    if missing:
        logger.warning("DMN model missing inputs: %s", missing)

    return inputs, fhir_refs, input_audit


@mlflow.trace(name="dmn_executor")
def dmn_executor(state: CarePlanComposerState) -> dict:
    """Execute applicable DMN models in topological order.

    The resolution loop interleaves with evaluation: each model's
    inputs are resolved (concept pipeline + LLM), then evaluated
    (via the evaluation client), before the next model starts.
    Chained outputs feed later inputs.
    """
    logger.info("── DMN Executor ──")

    ips_bundle = state.get("ips_bundle", {})
    applicable_models = state.get("applicable_dmn_models", [])
    dependency_graph = state.get("dmn_dependency_graph", [])

    if not applicable_models:
        logger.info("No applicable DMN models — skipping execution")
        return {"dmn_results": []}

    from acp_writer.tools.bundle_inventory import build_bundle_inventory
    inventory = build_bundle_inventory(ips_bundle)
    temporal_index = build_temporal_index(ips_bundle)

    llm_client = None
    try:
        from acp_writer.llm import get_llm
        llm_client = get_llm(state)
    except Exception as exc:
        logger.warning("LLM client unavailable for DMN extraction — degraded mode: %s", exc)

    evaluation_client = get_evaluation_client(state)

    model_map = {m["id"]: m for m in applicable_models}
    prior_results: dict[str, dict] = {}
    audit_trail: list[dict[str, Any]] = []

    execution_order: list[str] = []
    if dependency_graph:
        for level in dependency_graph:
            execution_order.extend(level)
    else:
        execution_order = [m["id"] for m in applicable_models]

    today = datetime.now(timezone.utc).date().isoformat()

    for model_id in execution_order:
        model_info = model_map.get(model_id)
        if not model_info:
            continue

        inputs, fhir_refs, input_audit = resolve_inputs(
            model_info.get("inputs", []),
            ips_bundle, inventory, prior_results,
            llm_client=llm_client,
            reference_date=today,
            temporal_index=temporal_index,
        )

        logger.info("Evaluating DMN model: %s with inputs: %s", model_info.get("name"), inputs)

        try:
            result = evaluation_client.evaluate(model_id, inputs)
            prior_results[model_id] = result

            audit_trail.append({
                "model_id": model_id,
                "model_name": model_info.get("name", model_id),
                "inputs": inputs,
                "outputs": result,
                "fhir_references": fhir_refs,
                "input_resolution": input_audit,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            logger.info("DMN result for %s: %s", model_info.get("name"), result)

        except ModelNotDeployed:
            logger.warning("Model %s not deployed — skipping", model_id)
            audit_trail.append({
                "model_id": model_id,
                "model_name": model_info.get("name", model_id),
                "inputs": inputs,
                "outputs": {},
                "fhir_references": fhir_refs,
                "input_resolution": input_audit,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": "Model not deployed",
            })

        except DmnEngineError as exc:
            logger.error("DMN engine rejected %s: %s", model_id, exc.messages)
            audit_trail.append({
                "model_id": model_id,
                "model_name": model_info.get("name", model_id),
                "inputs": inputs,
                "outputs": {},
                "fhir_references": fhir_refs,
                "input_resolution": input_audit,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
                "error_status": exc.status_code,
                "error_messages": exc.messages,
            })

        except Exception as e:
            logger.error("DMN evaluation failed for %s: %s", model_id, e)
            audit_trail.append({
                "model_id": model_id,
                "model_name": model_info.get("name", model_id),
                "inputs": inputs,
                "outputs": {},
                "fhir_references": fhir_refs,
                "input_resolution": input_audit,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
            })

    return {"dmn_results": audit_trail}
