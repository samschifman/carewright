"""Mock API router for BFF development. Not shipped in production images."""

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Request, UploadFile

from cpg_ingester.mocks.data import (
    RUNS,
    RUN_DETAILS,
    _make_steps,
    load_run_directory,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _configured_run() -> tuple[dict[str, Any], dict[str, Any]] | None:
    directory = os.getenv("CPG_INGESTER_MOCK_RUN_DIR")
    if not directory:
        return None
    return load_run_directory(directory)


@router.get("/api/v1/runs")
def list_runs():
    configured = _configured_run()
    if configured:
        return [configured[0]]
    return list(RUNS.values())


@router.get("/api/v1/runs/{run_id}")
def get_run(run_id: str):
    configured = _configured_run()
    if configured:
        run, detail = configured
        if run_id == run["id"]:
            return detail
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    detail = RUN_DETAILS.get(run_id)
    if not detail:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
    return detail


@router.post("/api/v1/upload")
async def upload_cpg(pdf: UploadFile = File(...)):
    run_id = f"run-{uuid4().hex[:8]}"
    logger.info("Mock upload: %s -> %s", pdf.filename, run_id)
    RUNS[run_id] = {
        "id": run_id,
        "status": "parsing",
        "cpgName": pdf.filename or "Uploaded CPG",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "currentStep": "Parse",
    }
    RUN_DETAILS[run_id] = {
        **RUNS[run_id],
        "steps": _make_steps("Parse"),
    }
    return {"runId": run_id}


@router.post("/api/v1/runs/{run_id}/review/{gate}")
async def submit_review(run_id: str, gate: str, request: Request):
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    logger.info("Mock review: run=%s gate=%s action=%s", run_id, gate, body)
    return {"status": "accepted"}


@router.post("/api/v1/runs/{run_id}/rerun")
async def rerun_pipeline(run_id: str):
    new_id = f"run-{uuid4().hex[:8]}"
    original = RUNS.get(run_id, {})
    RUNS[new_id] = {
        "id": new_id,
        "status": "parsing",
        "cpgName": original.get("cpgName", "Rerun"),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "currentStep": "Parse",
    }
    RUN_DETAILS[new_id] = {
        **RUNS[new_id],
        "steps": _make_steps("Parse"),
    }
    return {"runId": new_id}


@router.get("/api/v1/runs/{run_id}/artifacts/{path:path}")
def get_artifact(run_id: str, path: str):
    return {"mock": True, "run_id": run_id, "path": path}
