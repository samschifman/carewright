"""FHIR Server pod service — Writer + status management.

Consumes: fhir_bundle_ref.
Security profile: FHIR server access only.
"""

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")

from fastapi import FastAPI, Request

from cpg_contracts import get_phi_store, resolve_ref
from acp_writer.nodes.fhir_server_writer import (
    approve_care_plan,
    fhir_server_writer,
    get_care_plan,
    list_care_plans,
    reject_care_plan,
)
from acp_writer.services.automation_publisher import get_publisher, publication_status_view

logger = logging.getLogger(__name__)

app = FastAPI(title="acp-writer-fhir-server", version="0.1.0")
_phi_store = get_phi_store()


@app.on_event("startup")
def reconcile_publications():
    """Resume approvals interrupted by a process restart."""
    get_publisher().reconcile_on_start()


@app.get("/health")
def health():
    return {"status": "UP", "service": "fhir-server"}


@app.post("/api/v1/write")
async def write(request: Request):
    """Write FHIR Bundle to HAPI FHIR server.

    In the in-run gate model, WriteFHIR only executes after the care-plan
    review gate approves, so the bundle is written as active by default.
    """
    from acp_writer.services.reviewer import reviewer_from_payload

    data = await request.json()
    fhir_bundle = resolve_ref(data, "fhir_bundle", _phi_store)
    approved = data.get("approved", True)
    state = {
        "fhir_bundle": fhir_bundle,
        "careplan_id": (
            data.get("careplan_id")
            or data.get("business_key")
            or data.get("workflow_business_key")
            or data.get("process_instance_id")
            or data.get("run_id")
        ),
        "patient_reference": data.get("patient_reference", ""),
        "approved": approved,
        # The approving clinician from the care-plan review gate. Threaded so the
        # deployed path records the verifier + acknowledges conflicts (#169 F1).
        "reviewer": reviewer_from_payload(
            data.get("reviewer"), clinician=data.get("clinician")
        ).model_dump(),
    }
    result = fhir_server_writer(state)
    return result


@app.get("/api/v1/careplans")
def careplans(patient: str | None = None, status: str | None = None):
    return list_care_plans(patient=patient, status=status)


@app.get("/api/v1/careplans/{careplan_id}")
def get_careplan(careplan_id: str):
    cp = get_care_plan(careplan_id)
    if not cp:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    return cp


@app.put("/api/v1/careplans/{careplan_id}/status")
async def update_status(careplan_id: str, request: Request):
    data = await request.json()
    new_status = data.get("status")
    if new_status == "active":
        from acp_writer.services.reviewer import reviewer_from_payload
        reviewer = reviewer_from_payload(data.get("reviewer"), clinician=data.get("clinician"))
        result = approve_care_plan(careplan_id, reviewer=reviewer)
    elif new_status == "entered-in-error":
        result = reject_care_plan(careplan_id, reason=data.get("reason", "No reason provided"))
    else:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")
    if not result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found")
    return result


@app.post("/api/v1/careplans/{careplan_id}/automations/publish")
def retry_automation_publication(careplan_id: str):
    result = get_publisher().retry(careplan_id)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No publication job found")
    return publication_status_view(result)
