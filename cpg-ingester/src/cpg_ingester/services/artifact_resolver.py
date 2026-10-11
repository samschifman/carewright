"""Resolve artifact refs in run details by fetching data from MinIO.

Keeps artifact-store concerns out of the SonataFlow mapping layer.
The sonataflow_client produces a RunDetail with raw ``workflowData``;
this module hydrates that into the fields the UI expects.
"""

import logging
from typing import Any

from cpg_contracts.artifact_store import ArtifactStore

from cpg_ingester.artifact_id import make_artifact_id

logger = logging.getLogger(__name__)

_ANALYSIS_FIELDS = {
    "cpg_metadata": "metadata",
    "metadata": "metadata",
    "section_map": "sectionMap",
    "item_manifest": "manifest",
}

_GENERATE_FIELDS = {
    "dmn_results": "decisions",
    "recommendation_results": "recommendations",
    "bpmn_results": "automations",
    "recommendation_escalations": "recommendationEscalations",
    "escalated_items": "escalatedItems",
}


def _fetch_ref(store: ArtifactStore, ref: str) -> dict | None:
    try:
        return store.get(ref)
    except Exception as exc:
        logger.warning("Could not resolve artifact ref %s: %s", ref, exc)
        return None


def enrich_run_detail(
    detail: dict[str, Any], store: ArtifactStore | None,
) -> dict[str, Any]:
    """Resolve artifact refs in a RunDetail and populate UI-facing fields.

    Mutates and returns ``detail``.  The ``workflowData`` key is consumed
    and removed — it is an internal transport field, not part of the API.
    """
    wd = detail.pop("workflowData", None)
    if not wd:
        return detail

    errors: list[dict[str, str]] = []

    # --- Analysis ---
    analysis_raw = wd.get("analysisResult") or {}
    if "error" in analysis_raw:
        errors.append({"step": "Analyze", "message": str(analysis_raw["error"])})
    elif analysis_raw:
        analysis = analysis_raw
        ref = analysis_raw.get("analysis_result_ref")
        if ref and store:
            resolved = _fetch_ref(store, ref)
            if resolved:
                analysis = resolved
        for src_key, dest_key in _ANALYSIS_FIELDS.items():
            if src_key in analysis and dest_key not in detail:
                detail[dest_key] = analysis[src_key]

    # --- Generate ---
    gen_raw = wd.get("generateResult") or {}
    if "error" in gen_raw:
        errors.append({"step": "Generate", "message": str(gen_raw["error"])})
    elif gen_raw:
        gen = gen_raw
        ref = gen_raw.get("generate_result_ref")
        if ref and store:
            resolved = _fetch_ref(store, ref)
            if resolved:
                gen = resolved
        for src_key, dest_key in _GENERATE_FIELDS.items():
            if src_key in gen:
                detail[dest_key] = gen[src_key]

    # --- Artifact IDs (deterministic, computed from cpg_id + name + section) ---
    cpg_id = (detail.get("metadata") or {}).get("cpg_id", "")
    if cpg_id:
        for d in detail.get("decisions", []):
            if isinstance(d, dict) and "item" in d:
                d["artifact_id"] = make_artifact_id(
                    cpg_id, "dmn",
                    d["item"].get("name", ""),
                    d["item"].get("section", ""),
                )
        for r in detail.get("recommendations", []):
            if isinstance(r, dict):
                r["artifact_id"] = make_artifact_id(
                    cpg_id, "rec",
                    r.get("title", ""),
                    r.get("section", ""),
                )
        for automation in detail.get("automations", []):
            if not isinstance(automation, dict) or automation.get("artifact_id"):
                continue
            summary = automation.get("template_summary", {})
            item = automation.get("item", {})
            automation["artifact_id"] = make_artifact_id(
                cpg_id,
                "bpmn",
                summary.get("name") or item.get("name", ""),
                summary.get("section") or item.get("section", ""),
            )

    # --- Assembly ---
    assembly_raw = wd.get("assemblyResult") or {}
    if "error" in assembly_raw:
        errors.append({"step": "Assemble", "message": str(assembly_raw["error"])})
    elif assembly_raw:
        assembly = assembly_raw
        ref = assembly_raw.get("assembly_result_ref")
        if ref and store:
            resolved = _fetch_ref(store, ref)
            if resolved:
                assembly = resolved
        if "assembly_report" in assembly:
            detail["assemblyReport"] = assembly["assembly_report"]
        elif "report" in assembly:
            detail["assemblyReport"] = assembly["report"]

    # --- Delivery (always inline, no ref) ---
    delivery_raw = wd.get("deliveryResult") or {}
    if delivery_raw:
        ds = delivery_raw.get("delivery_status", delivery_raw)
        # Normalize: support both old ("delivered") and new ("published") shapes
        if "published" not in ds and "delivered" in ds:
            ds["published"] = ds.pop("delivered")
        detail["deliveryStatus"] = ds

    if errors:
        detail["errors"] = errors

    return detail
