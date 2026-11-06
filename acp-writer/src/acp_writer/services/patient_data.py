"""Patient Data pod service — Condition Scanner + IPS extraction.

Security profile: patient data access, no LLM, no FHIR server.
Produces: ips_bundle_ref (stores original bundle for downstream use by ExecuteDMN).
"""

import logging
from uuid import uuid4

from acp_writer.nodes.condition_scanner import condition_scanner
from cpg_contracts import get_phi_store, post_callback, resolve_ref, store_artifact
from cpg_contracts.cloud_events import require_callback_url
from cpg_contracts.service_auth import require_service_token
from cpg_contracts.tracing import install_phi_span_processor
from fastapi import BackgroundTasks, Depends, FastAPI, Request

logger = logging.getLogger(__name__)

install_phi_span_processor()

app = FastAPI(title="acp-writer-patient-data", version="0.1.0")
_phi_store = get_phi_store()


@app.get("/health")
def health():
    return {"status": "UP", "service": "patient-data"}


@app.post("/api/v1/scan", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_PATIENT_DATA', workflow_route=True))])
async def scan(request: Request):
    """Extract patient conditions, medications, demographics from IPS Bundle."""
    data = await request.json()
    ips_bundle = resolve_ref(data, "ips_bundle", _phi_store)
    if not ips_bundle and "ips_ref" in data:
        ips_bundle = _phi_store.get(data["ips_ref"]) if _phi_store else {}
    result = condition_scanner({"ips_bundle": ips_bundle})

    _, ref = store_artifact(_phi_store, f"{uuid4()}/ips_bundle.json", ips_bundle)
    if ref:
        result["ips_bundle_ref"] = ref

    return result


@app.post("/api/v1/scan-async", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_PATIENT_DATA', workflow_route=True))])
async def scan_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run scan in background, POST callback."""
    data = await request.json()
    callback_url = require_callback_url(data)
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(_run_scan_background, data, callback_url, process_instance_id)
    return {"status": "accepted"}


def _run_scan_background(data: dict, callback_url: str, process_instance_id: str):
    try:
        ips_bundle = resolve_ref(data, "ips_bundle", _phi_store)
        if not ips_bundle and "ips_ref" in data:
            ips_bundle = _phi_store.get(data["ips_ref"]) if _phi_store else {}
        result = condition_scanner({"ips_bundle": ips_bundle})

        _, ref = store_artifact(_phi_store, f"{uuid4()}/ips_bundle.json", ips_bundle)
        if ref:
            result["ips_bundle_ref"] = ref
    except Exception as e:
        logger.error('Scan background task failed exception_type=%s (details redacted)', type(e).__name__)
        result = {"error": f"operation-failed:{type(e).__name__}"}

    post_callback(callback_url, process_instance_id, "scan-done", result)
