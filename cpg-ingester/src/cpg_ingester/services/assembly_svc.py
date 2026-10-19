"""Assembly pod service — link resolution and integrity checks for generated artifacts.

Consumes: generate_result_ref (or analysis_result_ref for legacy flow).
Produces: assembly_result_ref.
Security profile: no external network, no LLM.
"""

import logging
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, Request

from cpg_contracts import get_artifact_store, resolve_ref, store_artifact
from cpg_ingester.nodes.assembly import assembly

logger = logging.getLogger(__name__)

app = FastAPI(title="cpg-ingester-assembly", version="0.1.0")
_store = get_artifact_store()


@app.get("/health")
def health():
    return {"status": "UP", "service": "assembly"}


@app.post("/api/v1/assemble")
async def assemble(request: Request):
    """Assemble validated DMN, recommendation, and BPMN generation results."""
    data = await request.json()

    source = resolve_ref(data, "generate_result", _store)
    if not isinstance(source, dict) or "dmn_results" not in source:
        source = resolve_ref(data, "analysis_result", _store)
    if isinstance(source, dict) and "dmn_results" in source:
        cpg_metadata = source.get("cpg_metadata", data.get("cpg_metadata", {}))
        item_manifest = source.get("item_manifest", data.get("item_manifest", []))
        dmn_results = source.get("dmn_results", [])
        recommendation_results = source.get("recommendation_results", [])
        bpmn_results = source.get("bpmn_results", [])
        recommendation_escalations = source.get(
            "recommendation_escalations", data.get("recommendation_escalations", [])
        )
        artifact_review = data.get("artifact_review", {})
    else:
        cpg_metadata = data.get("cpg_metadata", {})
        item_manifest = data.get("item_manifest", [])
        dmn_results = data.get("dmn_results", [])
        recommendation_results = data.get("recommendation_results", [])
        bpmn_results = data.get("bpmn_results", [])
        recommendation_escalations = data.get("recommendation_escalations", [])
        artifact_review = data.get("artifact_review", {})

    with tempfile.TemporaryDirectory() as output_dir:
        state = {
            "cpg_metadata": cpg_metadata,
            "item_manifest": item_manifest,
            "dmn_results": dmn_results,
            "recommendation_results": recommendation_results,
            "bpmn_results": bpmn_results,
            "recommendation_escalations": recommendation_escalations,
            "artifact_review": artifact_review,
            "output_dir": output_dir,
        }

        result = assembly(state)

        output = {
            "cpg_metadata": cpg_metadata,
            "dmn_results": result.get("dmn_results", []),
            "recommendation_results": result.get("recommendation_results", []),
            "bpmn_results": result.get("bpmn_results", []),
            "escalated_items": result.get("escalated_items", []),
            "assembly_report": result.get("assembly_report", {}),
        }

        completed_at = datetime.now(timezone.utc).isoformat()
        _, ref = store_artifact(_store, f"{uuid4()}/assembly_result.json", output)
        if ref:
            return {"assembly_result_ref": ref, "completed_at": completed_at}
        output["completed_at"] = completed_at
        return output
