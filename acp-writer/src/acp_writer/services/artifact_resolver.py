"""Resolve artifact refs in run details by fetching data from MinIO.

The sonataflow_client produces a RunDetail with raw ``workflowData``;
this module hydrates ref keys into the fields the UI expects.
PHI data (IPS bundles, planning briefs, FHIR bundles) lives in the
cpg-phi bucket; non-PHI data (recommendations) in cpg-artifacts.
"""

import logging
from typing import Any

import mlflow
from cpg_contracts.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


def _fetch_ref(store: ArtifactStore, ref: str) -> dict | None:
    try:
        return store.get(ref)
    except Exception as exc:
        logger.warning("Could not resolve artifact ref %s: %s", ref, exc)
        return None


def _format_number(x) -> str:
    """Render a numeric bound without a trailing ``.0`` (7.0 → "7", 7.5 → "7.5").

    Target bounds are typed ``float`` on the brief, so an LLM-emitted ``7``
    round-trips through JSON as ``7.0``; strip the noise for display.
    """
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _format_brief_goal_target(
    measure_code: dict | None, value: dict | None
) -> str:
    """Format a planning-brief goal target (FHIRCode + TargetValue) into a string.

    e.g. ``{"display": "HbA1c"}`` + ``{"high": 7, "unit": "%"}`` → ``"HbA1c < 7 %"``.
    Returns ``""`` when no measure is present.
    """
    mc = measure_code or {}
    measure = mc.get("display") or mc.get("code") or ""
    if not measure:
        return ""
    val = value or {}
    low, high, unit = val.get("low"), val.get("high"), val.get("unit", "")
    if low is not None and high is not None:
        return f"{measure}: {_format_number(low)}–{_format_number(high)} {unit}".rstrip()
    if high is not None:
        return f"{measure} < {_format_number(high)} {unit}".rstrip()
    if low is not None:
        return f"{measure} > {_format_number(low)} {unit}".rstrip()
    return measure


@mlflow.trace(name="plan_goal_from_entry")
def plan_goal_from_entry(entry: dict, idx: int) -> dict:
    """Map a planning-brief goal dict → the BFF ``PlanGoal`` shape.

    The brief has no id; assign an index-based id (``g{idx}``) — the UI uses it
    only as a React key. snake_case → camelCase, mirroring
    ``plan_conflict_from_entry``.
    """
    pg: dict = {
        "id": entry.get("id") or f"g{idx}",
        "description": entry.get("description", ""),
    }
    target = _format_brief_goal_target(
        entry.get("target_measure_code"), entry.get("target_value")
    )
    if target:
        pg["target"] = target
    if entry.get("source_cpg"):
        pg["sourceCpgId"] = entry["source_cpg"]
    if entry.get("source_recommendation_id"):
        pg["sourceRecommendationId"] = entry["source_recommendation_id"]
    return pg


@mlflow.trace(name="plan_activity_from_entry")
def plan_activity_from_entry(entry: dict, idx: int) -> dict:
    """Map a planning-brief activity dict → the BFF ``PlanActivity`` shape.

    The brief has no id; assign an index-based id (``a{idx}``). snake_case →
    camelCase, mirroring ``plan_conflict_from_entry``. ``source_dmn_call``,
    ``code``, ``type``, and ``workflow`` are intentionally not surfaced yet
    (see spec — deferred to the raw-FHIR viewer / surface 2).
    """
    pa: dict = {
        "id": entry.get("id") or f"a{idx}",
        "description": entry.get("description", ""),
    }
    for key in ("dose", "route", "frequency", "specialty"):
        val = entry.get(key)
        if val:
            pa[key] = val
    for src_key, dst_key in (
        ("source_cpg", "sourceCpg"),
        ("source_recommendation_id", "sourceRecommendationId"),
        ("clinical_rationale", "clinicalRationale"),
    ):
        val = entry.get(src_key)
        if val:
            pa[dst_key] = val
    automations = [
        _automation_view_from_entry(item)
        for item in entry.get("automations", []) or []
        if isinstance(item, dict) or hasattr(item, "model_dump")
    ]
    if automations:
        pa["automations"] = automations
    return pa


@mlflow.trace(name="plan_activity_automation_view")
def _automation_view_from_entry(entry: dict) -> dict:
    """Map a planning-brief automation instance into its UI view model."""
    if hasattr(entry, "model_dump"):
        entry = entry.model_dump(mode="json", exclude_none=True)
    validation = entry.get("validation") or {}
    status = validation.get("status", "escalated")
    refs = []
    for ref in entry.get("template_refs", []) or []:
        refs.append({
            "templateId": ref.get("template_id", ""),
            "version": ref.get("version", ""),
            **({"sourceCpg": ref["source_cpg"]} if ref.get("source_cpg") else {}),
        })
    bindings = []
    for binding in entry.get("bindings", []) or []:
        item = {
            "name": binding.get("name", ""),
            "value": binding.get("value"),
            "source": binding.get("source", "authored"),
        }
        for key in ("unit", "note"):
            if binding.get(key) is not None:
                item[key] = binding[key]
        bindings.append(item)
    return {
        "id": entry.get("id", ""),
        "templateRefs": refs,
        "activityIds": entry.get("activity_ids", []),
        "enabled": entry.get("enabled") is True,
        "revision": entry.get("revision", ""),
        "bpmnXml": entry.get("bpmn_xml", ""),
        "capabilitiesUsed": entry.get("capabilities_used", []),
        "bindings": bindings,
        "validation": {
            "status": status,
            "rungsPassed": validation.get("rungs_passed", []),
            "warnings": validation.get("warnings", []),
            "errors": validation.get("errors", []),
        },
        "reviewNotes": entry.get("review_notes", []),
    }


@mlflow.trace(name="plan_conflict_from_entry")
def plan_conflict_from_entry(entry: dict) -> dict:
    """Map a planning-brief ``ConflictEntry`` dict → the BFF ``PlanConflict`` shape.

    The brief uses snake_case (``cpg_id``/``recommendation_id``); the API uses
    camelCase (``cpgId``/``recommendationId``). Only the UI-facing fields are
    carried through — the full provenance record is the read-back source of
    truth (see ``ai_transparency.plan_conflict_from_provenance``).
    """
    pc: dict = {
        "id": entry.get("id", ""),
        "description": entry.get("description", ""),
    }
    for src_key, dst_key in (("severity", "severity"), ("category", "category"),
                             ("status", "status"), ("confidence", "confidence"),
                             ("suggested_resolution", "suggestedResolution"),
                             ("resolution", "resolution")):
        val = entry.get(src_key)
        if val:
            pc[dst_key] = val

    sources: list[dict] = []
    for s in entry.get("sources", []) or []:
        if not isinstance(s, dict):
            continue
        src: dict = {"cpgId": s.get("cpg_id", "")}
        if s.get("recommendation_id"):
            src["recommendationId"] = s["recommendation_id"]
        if s.get("excerpt"):
            src["excerpt"] = s["excerpt"]
        sources.append(src)
    if sources:
        pc["sources"] = sources
    return pc


def _to_coded_items(codes: list | None) -> list[dict]:
    """Convert internal code lists to the contract's CodedItem[] shape."""
    if not codes:
        return []
    items = []
    for c in codes:
        if isinstance(c, dict):
            items.append({
                "display": c.get("display", c.get("name", str(c.get("code", "")))),
                "code": c.get("code", ""),
                "system": c.get("system", ""),
            })
        elif isinstance(c, str):
            items.append({"display": c})
    return items


@mlflow.trace(name="enrich_run_detail")
def enrich_run_detail(
    detail: dict[str, Any],
    phi_store: ArtifactStore | None,
    artifacts_store: ArtifactStore | None,
) -> dict[str, Any]:
    """Resolve artifact refs in a RunDetail and populate UI-facing fields.

    Mutates and returns ``detail``.  The ``workflowData`` key is consumed
    and removed — it is an internal transport field, not part of the API.
    """
    wd = detail.pop("workflowData", None)
    if not wd:
        return detail

    first_error: dict[str, str] | None = None
    store = phi_store or artifacts_store

    def _record_error(step_key: str, message: str) -> None:
        nonlocal first_error
        if not first_error:
            first_error = {"stepKey": step_key, "message": message}

    # --- Patient data → PatientSummary ---
    patient_raw = wd.get("patientData") or {}
    if patient_raw:
        demographics = patient_raw.get("patient_demographics") or {}
        detail["patient"] = {
            "name": demographics.get("name", ""),
            "birthDate": demographics.get("birth_date", ""),
            "gender": demographics.get("gender", ""),
            "patientReference": patient_raw.get("patient_reference", ""),
            "conditions": _to_coded_items(patient_raw.get("condition_codes")),
            "medications": _to_coded_items(patient_raw.get("medication_codes")),
            "allergies": _to_coded_items(patient_raw.get("allergy_codes")),
            "observations": _to_coded_items(patient_raw.get("observation_codes")),
        }

    # --- DMN results ---
    dmn_raw = wd.get("dmnData") or {}
    if "error" in dmn_raw:
        _record_error("execute_dmn", str(dmn_raw["error"]))

    # --- Recommendations ---
    rec_raw = wd.get("recData") or {}
    if "error" in rec_raw:
        _record_error("retrieve_recommendations", str(rec_raw["error"]))

    # --- Planning brief (from ComposePlan) → CarePlanView source ---
    composer_raw = wd.get("composerData") or {}
    planning_brief = None
    if "error" in composer_raw:
        _record_error("compose_plan", str(composer_raw["error"]))
    elif composer_raw:
        brief = composer_raw
        ref = composer_raw.get("planning_brief_ref")
        if ref and phi_store:
            resolved = _fetch_ref(phi_store, ref)
            if resolved:
                brief = resolved
        planning_brief = brief.get("planning_brief", brief)

    # --- FHIR generation → CarePlanView ---
    fhir_gen_raw = wd.get("fhirGenData") or {}
    if "error" in fhir_gen_raw:
        _record_error("generate_bundle", str(fhir_gen_raw["error"]))
    elif fhir_gen_raw:
        bundle = fhir_gen_raw
        ref = fhir_gen_raw.get("fhir_bundle_ref")
        if ref and phi_store:
            resolved = _fetch_ref(phi_store, ref)
            if resolved:
                bundle = resolved
        fhir_bundle = bundle.get("fhir_bundle", bundle)

        care_plan_view: dict[str, Any] = {"fhirBundle": fhir_bundle}
        if planning_brief and isinstance(planning_brief, dict):
            care_plan_view["goals"] = [
                plan_goal_from_entry(g, i)
                for i, g in enumerate(planning_brief.get("goals", []))
                if isinstance(g, dict)
            ]
            care_plan_view["activities"] = [
                plan_activity_from_entry(a, i)
                for i, a in enumerate(planning_brief.get("activities", []))
                if isinstance(a, dict)
            ]
            shared_activity_ids: dict[str, list[str]] = {}
            for activity in care_plan_view["activities"]:
                for automation in activity.get("automations", []):
                    shared_activity_ids.setdefault(automation["id"], []).append(activity["id"])
            for activity in care_plan_view["activities"]:
                for automation in activity.get("automations", []):
                    automation["activityIds"] = shared_activity_ids[automation["id"]]
            care_plan_view["conflicts"] = [
                plan_conflict_from_entry(c)
                for c in planning_brief.get("conflicts", [])
                if isinstance(c, dict)
            ]
        detail["carePlan"] = care_plan_view

    # --- Write result ---
    write_raw = wd.get("writeResult") or {}
    if "error" in write_raw:
        _record_error("write_fhir", str(write_raw["error"]))
    elif write_raw:
        detail["careplanId"] = write_raw.get("careplan_id")

    if first_error:
        detail["error"] = first_error

    return detail
