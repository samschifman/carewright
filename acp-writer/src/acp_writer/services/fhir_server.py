"""FHIR Server pod service — Writer + status management.

Consumes: fhir_bundle_ref.
Security profile: FHIR server access only.
"""

import logging

from cpg_contracts.service_auth import require_service_token
from cpg_contracts.tracing import install_phi_span_processor
from fastapi import Depends

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")

from acp_writer.nodes.fhir_server_writer import (
    approve_care_plan,
    fhir_server_writer,
    get_care_plan,
    list_care_plans,
    rehydrate_care_plans_from_jobs,
    reject_care_plan,
)
from acp_writer.services.approval_binding import write_approved_bundle
from acp_writer.services.automation_publisher import (
    get_publisher,
    publication_status_view,
)
from cpg_contracts import get_phi_store, resolve_ref
from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

install_phi_span_processor()

app = FastAPI(title="acp-writer-fhir-server", version="0.1.0")
_phi_store = get_phi_store()


@app.on_event("startup")
def reconcile_publications():
    """Resume interrupted approvals, then rebuild the care-plan index from the job store."""
    get_publisher().reconcile_on_start()
    restored = rehydrate_care_plans_from_jobs()
    if restored:
        logger.info("Rehydrated %d approved care plan(s) from the job store", restored)


@app.get("/health")
def health():
    return {"status": "UP", "service": "fhir-server"}


@app.post("/api/v1/write", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_FHIR_SERVER', workflow_route=True))])
async def write(request: Request):
    """Publish only the signed approval's exact artifact revision."""
    data = await request.json()
    return write_approved_bundle(data, _phi_store, fhir_server_writer)


@app.get("/api/v1/careplans", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_FHIR_SERVER'))])
def careplans(patient: str | None = None, status: str | None = None):
    return list_care_plans(patient=patient, status=status)


@app.get("/api/v1/careplans/{careplan_id}", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_FHIR_SERVER'))])
def get_careplan(careplan_id: str):
    cp = get_care_plan(careplan_id)
    if not cp:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    return cp


@app.put("/api/v1/careplans/{careplan_id}/status", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_FHIR_SERVER'))])
async def update_status(careplan_id: str, request: Request):
    data = await request.json()
    new_status = data.get("status")
    if new_status == "active":
        return write_approved_bundle({**data, "careplan_id": careplan_id}, _phi_store, fhir_server_writer)
    elif new_status == "entered-in-error":
        result = reject_care_plan(careplan_id, reason=data.get("reason", "No reason provided"))
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    return result


@app.post("/api/v1/careplans/{careplan_id}/automations/publish", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_FHIR_SERVER'))])
def retry_automation_publication(careplan_id: str):
    result = get_publisher().retry(careplan_id)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No publication job found")
    return publication_status_view(result)
