"""Delivery pod service — publishes artifacts to the artifact store.

Consumes: assembly_result_ref. Publishes individual artifacts to
well-known locations in MinIO/S3.
Security profile: MinIO only — no external network.
"""

import logging
import tempfile
from datetime import datetime, timezone

from fastapi import FastAPI, Request

from cpg_contracts import get_artifact_store, resolve_ref
from cpg_ingester.nodes.delivery import delivery

logger = logging.getLogger(__name__)

app = FastAPI(title="cpg-ingester-delivery", version="0.1.0")
_store = get_artifact_store()


@app.get("/health")
def health():
    return {"status": "UP", "service": "delivery"}


@app.post("/api/v1/deliver")
async def deliver(request: Request):
    """Publish assembled artifacts to the artifact store."""
    data = await request.json()

    assembly_result = resolve_ref(data, "assembly_result", _store)
    if isinstance(assembly_result, dict) and "dmn_results" in assembly_result:
        cpg_metadata = assembly_result.get("cpg_metadata", data.get("cpg_metadata", {}))
        dmn_results = assembly_result.get("dmn_results", [])
        recommendation_results = assembly_result.get("recommendation_results", [])
        bpmn_results = assembly_result.get("bpmn_results", [])
        escalated_items = assembly_result.get("escalated_items", [])
        assembly_report = assembly_result.get("assembly_report", {})
    else:
        cpg_metadata = data.get("cpg_metadata", {})
        dmn_results = data.get("dmn_results", [])
        recommendation_results = data.get("recommendation_results", [])
        bpmn_results = data.get("bpmn_results", [])
        escalated_items = data.get("escalated_items", [])
        assembly_report = data.get("assembly_report", {})

    with tempfile.TemporaryDirectory() as output_dir:
        state = {
            "cpg_metadata": cpg_metadata,
            "dmn_results": dmn_results,
            "recommendation_results": recommendation_results,
            "bpmn_results": bpmn_results,
            "escalated_items": escalated_items,
            "assembly_report": assembly_report,
            "artifact_store": _store,
            "output_dir": output_dir,
        }

        result = delivery(state)

        return {
            "delivery_status": result.get("delivery_status", {}),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
