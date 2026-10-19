"""BFF (Backend-for-Frontend) service for the cpg-ingester React UI.

Mediates between the SPA and SonataFlow/MinIO. Each backend dependency
is independently optional — MinIO can be configured without SonataFlow
and vice versa. Endpoints that lack their backing service fall back to
mock data when available.
"""

import logging
import os
import math
import re
from uuid import uuid4

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from cpg_contracts.artifact_store import ArtifactStore

from cpg_ingester.services.artifact_resolver import enrich_run_detail
from cpg_ingester.services.sonataflow_client import (
    SonataFlowClient,
    map_to_run_detail,
    map_to_run_summary,
)

logger = logging.getLogger(__name__)

SONATAFLOW_URL = os.getenv("SONATAFLOW_URL", "")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
DATA_INDEX_URL = os.getenv("DATA_INDEX_URL", "")

_artifact_store: ArtifactStore | None = None
if MINIO_ENDPOINT:
    _artifact_store = ArtifactStore(
        endpoint_url=MINIO_ENDPOINT,
        bucket="cpg-uploads",
        access_key=os.getenv("ARTIFACT_STORE_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("ARTIFACT_STORE_SECRET_KEY", "minioadmin"),
    )

_sonataflow: SonataFlowClient | None = None
if SONATAFLOW_URL:
    _sonataflow = SonataFlowClient(SONATAFLOW_URL)

app = FastAPI(title="cpg-ingester-bff", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/v1/notify-review")
def notify_review(payload: dict | None = None):
    """Called by SonataFlow when a review callback state is entered.

    No-op acknowledgment — the UI discovers review state by polling.
    """
    notification = payload if isinstance(payload, dict) else {}
    counts = notification.get("automation_counts")
    if not isinstance(counts, dict):
        counts = None
    logger.info(
        "Review notification: type=%s automation_counts=%s",
        notification.get("review_type"), counts,
    )
    result = {"status": "acknowledged"}
    if counts is not None:
        result["automation_counts"] = counts
    return result


@app.get("/health")
def health():
    return {
        "status": "UP",
        "service": "bff",
        "minio": bool(_artifact_store),
        "sonataflow": bool(SONATAFLOW_URL),
    }


_L7_ESCALATION_REASONS = {
    "semantic-budget-exhausted",
    "no-source-text",
    "reviewer-unparseable",
}


def _validate_parameter_value(parameter: dict, value) -> str | None:
    """Validate a pre-delivery default against its declared parameter contract."""
    name = str(parameter.get("name", "parameter"))
    value_type = parameter.get("type")
    if value is None or value == "":
        if parameter.get("required"):
            return f"Parameter {name!r} requires a value."
        return None
    if value_type == "boolean" and not isinstance(value, bool):
        return f"Parameter {name!r} requires a boolean value."
    if value_type == "integer" and (
        isinstance(value, bool) or not isinstance(value, int)
    ):
        return f"Parameter {name!r} requires an integer value."
    if value_type in {"decimal", "quantity"} and (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return f"Parameter {name!r} requires a finite numeric value."
    if value_type in {"string", "duration"} and not isinstance(value, str):
        return f"Parameter {name!r} requires a string value."
    if value_type == "duration" and not re.fullmatch(r"P(?=.*\d)[0-9YMWDTHS.]+", value):
        return f"Parameter {name!r} requires an ISO 8601 duration."

    constraints = parameter.get("constraints") or {}
    allowed = constraints.get("enum")
    if allowed and value not in allowed:
        return f"Parameter {name!r} must be one of the declared enum values."
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = constraints.get("min")
        maximum = constraints.get("max")
        if minimum is not None and value < minimum:
            return f"Parameter {name!r} is below its minimum constraint."
        if maximum is not None and value > maximum:
            return f"Parameter {name!r} exceeds its maximum constraint."
    pattern = constraints.get("pattern")
    if pattern and isinstance(value, str):
        try:
            if not re.fullmatch(pattern, value):
                return f"Parameter {name!r} does not match its declared format."
        except re.error:
            return f"Parameter {name!r} has an invalid format constraint."
    return None


def validate_review_payload(run_detail: dict, gate: str, review: dict) -> str | None:
    """Return a client-safe validation message for structured review edits."""
    edits = review.get("parameterEdits") or []
    acknowledgements = review.get("acknowledgements") or []
    if (edits or acknowledgements) and gate != "pre-delivery":
        return "Parameter edits and acknowledgements are only accepted at pre-delivery review."
    automations = run_detail.get("automations") or []
    by_template_id = {
        str((automation.get("template_summary") or {}).get("id")): automation
        for automation in automations
        if isinstance(automation, dict) and (automation.get("template_summary") or {}).get("id")
    }

    for edit in edits:
        if not isinstance(edit, dict):
            return "Each parameter edit must be an object."
        template_id = str(edit.get("templateId", ""))
        automation = by_template_id.get(template_id)
        if automation is None:
            return f"Unknown automation template {template_id!r}."
        process = (automation.get("process_ir") or {}).get("process") or {}
        parameters = (process.get("acp") or {}).get("parameters") or (
            automation.get("template_summary") or {}
        ).get("parameters") or []
        parameter_name = str(edit.get("parameter", ""))
        parameter = next((item for item in parameters if item.get("name") == parameter_name), None)
        if parameter is None:
            return f"Unknown parameter {parameter_name!r} on template {template_id!r}."
        error = _validate_parameter_value(parameter, edit.get("value"))
        if error:
            return error
        if "unit" in edit and edit.get("unit") != parameter.get("unit"):
            return f"Parameter {parameter_name!r} uses its declared unit and cannot be changed."

    for acknowledgement in acknowledgements:
        if not isinstance(acknowledgement, dict):
            return "Each acknowledgement must be an object."
        template_id = str(acknowledgement.get("templateId", ""))
        automation = by_template_id.get(template_id)
        if automation is None:
            return f"Unknown automation template {template_id!r}."
        if automation.get("escalation_reason") not in _L7_ESCALATION_REASONS:
            return f"Template {template_id!r} is not escalated at the semantic review rung."
        if not str(acknowledgement.get("note", "")).strip():
            return f"Acknowledgement for template {template_id!r} requires a note."
    return None


# ---------------------------------------------------------------------------
# MinIO-backed endpoints (upload, artifacts) — registered first so they
# take priority over mock router paths when MinIO is configured
# ---------------------------------------------------------------------------

if _artifact_store:

    @app.post("/api/v1/upload")
    async def upload_cpg(pdf: UploadFile = File(...)):
        upload_id = uuid4().hex[:8]
        filename = pdf.filename or "document.pdf"
        key = f"uploads/{upload_id}/{filename}"
        content = await pdf.read()
        content_type = pdf.content_type or "application/pdf"
        pdf_ref = _artifact_store.put_raw(key, content, content_type)
        logger.info("Uploaded %s (%d bytes) -> %s", filename, len(content), pdf_ref)

        if _sonataflow:
            instance = _sonataflow.start_workflow(pdf_ref, filename)
            run_id = instance["id"]
            logger.info("Started SonataFlow workflow %s for %s", run_id, filename)
        else:
            run_id = f"run-{upload_id}"

        return {"runId": run_id}

    @app.get("/api/v1/runs/{run_id}/artifacts/{path:path}")
    def get_artifact(run_id: str, path: str):
        key = f"{run_id}/{path}"
        try:
            data = _artifact_store.get_raw(key)
        except Exception:
            return JSONResponse(status_code=404, content={"error": f"Artifact not found: {key}"})
        return Response(content=data, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Mock fallback for SonataFlow-dependent endpoints (runs, reviews).
# Registered after MinIO endpoints so real upload/artifact routes win.
# ---------------------------------------------------------------------------
_mock_mode = False
if not SONATAFLOW_URL:
    try:
        from cpg_ingester.mocks.router import router as mock_router
        app.include_router(mock_router)
        _mock_mode = True
        logger.info("No SONATAFLOW_URL configured — serving mock run/review data")
    except ImportError:
        logger.warning("No SonataFlow configured and mocks package not available")


# ---------------------------------------------------------------------------
# SonataFlow-backed endpoints (runs, reviews) — only when not mocked
# ---------------------------------------------------------------------------

if not _mock_mode:

    @app.get("/api/v1/runs")
    def list_runs():
        instances = _sonataflow.list_instances()
        return [map_to_run_summary(inst) for inst in instances]

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        try:
            instance = _sonataflow.get_instance(run_id)
        except Exception:
            return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
        detail = map_to_run_detail(instance)
        return enrich_run_detail(detail, _artifact_store)

    @app.post("/api/v1/runs/{run_id}/review/{gate}")
    async def submit_review(run_id: str, gate: str, request: Request):
        if gate not in ("manifest", "pre-delivery"):
            return JSONResponse(status_code=400, content={"error": f"Unknown gate: {gate}"})
        review = await request.json()
        review = review if isinstance(review, dict) else {}
        if review.get("parameterEdits") or review.get("acknowledgements"):
            try:
                instance = _sonataflow.get_instance(run_id)
            except Exception:
                return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
            detail = enrich_run_detail(map_to_run_detail(instance), _artifact_store)
            error = validate_review_payload(detail, gate, review)
            if error:
                return JSONResponse(status_code=400, content={"error": error})
        _sonataflow.send_review(run_id, gate, review or {})
        return {"status": "accepted"}

    @app.delete("/api/v1/runs/{run_id}")
    def cancel_run(run_id: str):
        try:
            _sonataflow.cancel_instance(run_id)
        except Exception:
            logger.exception("Failed to cancel run %s", run_id)
            return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
        return {"status": "cancelled"}

    @app.post("/api/v1/runs/{run_id}/rerun")
    def rerun_pipeline(run_id: str):
        try:
            original = _sonataflow.get_instance(run_id)
        except Exception:
            return JSONResponse(status_code=404, content={"error": f"Run {run_id} not found"})
        data = original.get("workflowdata", {})
        pdf_ref = data.get("pdf_ref", "")
        cpg_name = data.get("cpg_name", "Rerun")
        instance = _sonataflow.start_workflow(pdf_ref, cpg_name)
        return {"runId": instance["id"]}
