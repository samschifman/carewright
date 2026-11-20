"""BFF (Backend-for-Frontend) service for the cpg-ingester React UI.

Mediates between the SPA and SonataFlow/MinIO. Each backend dependency
is independently optional — MinIO can be configured without SonataFlow
and vice versa. Endpoints that lack their backing service fall back to
mock data when available.
"""

import logging
import math
import os
import re
from uuid import uuid4

from cpg_contracts.artifact_paths import validated_run_artifact_key
from cpg_contracts.artifact_store import ArtifactStore
from cpg_contracts.service_auth import require_service_token
from cpg_contracts.tracing import trace_safe
from cpg_ingester.services.artifact_resolver import enrich_run_detail
from cpg_ingester.services.review_binding import (
    add_review_revision,
    submit_bound_review,
)
from cpg_ingester.services.sonataflow_client import (
    SonataFlowClient,
    map_to_run_detail,
    map_to_run_summary,
)
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

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
    allow_origins=[os.environ["CPG_PUBLIC_ORIGIN"]] if os.getenv("CPG_PUBLIC_ORIGIN") else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/api/v1/notify-review", dependencies=[Depends(require_service_token('SERVICE_TOKEN_CPG_BFF', workflow_route=True))])
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
    value_type = parameter.get("type")
    if value is None or value == "":
        if parameter.get("required"):
            return "parameter-required"
        return None
    if value_type == "boolean" and not isinstance(value, bool):
        return "parameter-type"
    if value_type == "integer" and (
        isinstance(value, bool) or not isinstance(value, int)
    ):
        return "parameter-type"
    if value_type in {"decimal", "quantity"} and (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return "parameter-type"
    if value_type in {"string", "duration"} and not isinstance(value, str):
        return "parameter-type"
    if value_type == "duration" and not re.fullmatch(r"P(?=.*\d)[0-9YMWDTHS.]+", value):
        return "parameter-duration"

    constraints = parameter.get("constraints") or {}
    allowed = constraints.get("enum")
    if allowed and value not in allowed:
        return "parameter-enum"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = constraints.get("min")
        maximum = constraints.get("max")
        if minimum is not None and value < minimum:
            return "parameter-minimum"
        if maximum is not None and value > maximum:
            return "parameter-maximum"
    pattern = constraints.get("pattern")
    if pattern and isinstance(value, str):
        try:
            if not re.fullmatch(pattern, value):
                return "parameter-format"
        except re.error:
            return "parameter-constraint"
    return None


def validate_review_payload(run_detail: dict, gate: str, review: dict) -> str | None:
    """Return a client-safe validation message for structured review edits."""
    edits = review.get("parameterEdits") or []
    acknowledgements = review.get("acknowledgements") or []
    if (edits or acknowledgements) and gate != "pre-delivery":
        return "review-gate"
    automations = run_detail.get("automations") or []
    # Key automations by the summary id and by the manifest item's template id:
    # an escalated template may carry no summary, but its item always has one.
    by_template_id: dict[str, dict] = {}
    for automation in automations:
        if not isinstance(automation, dict):
            continue
        for key in (
            (automation.get("template_summary") or {}).get("id"),
            (automation.get("item") or {}).get("template_id"),
        ):
            if key:
                by_template_id.setdefault(str(key), automation)

    for edit in edits:
        if not isinstance(edit, dict):
            return "invalid-parameter-edit"
        template_id = str(edit.get("templateId", ""))
        automation = by_template_id.get(template_id)
        if automation is None:
            return "unknown-template"
        process = (automation.get("process_ir") or {}).get("process") or {}
        parameters = (process.get("acp") or {}).get("parameters") or (
            automation.get("template_summary") or {}
        ).get("parameters") or []
        parameter_name = str(edit.get("parameter", ""))
        parameter = next((item for item in parameters if item.get("name") == parameter_name), None)
        if parameter is None:
            return "unknown-parameter"
        error = _validate_parameter_value(parameter, edit.get("value"))
        if error:
            return error
        if "unit" in edit and edit.get("unit") != parameter.get("unit"):
            return "parameter-unit"

    for acknowledgement in acknowledgements:
        if not isinstance(acknowledgement, dict):
            return "invalid-acknowledgement"
        template_id = str(acknowledgement.get("templateId", ""))
        automation = by_template_id.get(template_id)
        if automation is None:
            return "unknown-template"
        if automation.get("escalation_reason") not in _L7_ESCALATION_REASONS:
            return "acknowledgement-not-escalated"
        if not str(acknowledgement.get("note", "")).strip():
            return "acknowledgement-note-required"
    return None


# ---------------------------------------------------------------------------
# MinIO-backed endpoints are registered first so they take priority over
# mock router paths when configured.
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
    try:
        key = validated_run_artifact_key(run_id, path)
    except ValueError:
        raise HTTPException(400, detail={'error':'artifact-path'}) from None
    store = _artifact_store
    if not store:
        raise HTTPException(503, detail={'error':'artifact-store-unavailable'})
    try:
        data = store.get_raw_in_bucket("cpg-uploads", key)
    except Exception as exc:
        response = getattr(exc, 'response', None)
        missing = isinstance(exc, KeyError) or (isinstance(response, dict) and response.get('Error', {}).get('Code') in {'NoSuchKey', 'NotFound', '404'})
        if missing:
            raise HTTPException(404, detail={'error':'artifact-not-found'}) from None
        raise HTTPException(503, detail={'error':'artifact-store-unavailable'}) from None
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

    @trace_safe
    def _load_detail(run_id: str):
        try:
            instance = _sonataflow.get_instance(run_id)
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status == 404:
                raise HTTPException(404, detail={'error':'run-not-found'}) from None
            raise HTTPException(503, detail={'error':'workflow-unavailable'}) from None
        detail = map_to_run_detail(instance)
        return add_review_revision(enrich_run_detail(detail, _artifact_store))

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str):
        return _load_detail(run_id)

    @app.post("/api/v1/runs/{run_id}/review/{gate}")
    async def submit_review(run_id: str, gate: str, request: Request):
        try:
            review = await request.json()
        except ValueError:
            raise HTTPException(400, detail={'error':'approval-binding'}) from None
        if not isinstance(review, dict):
            raise HTTPException(400, detail={'error':'approval-binding'})
        return await submit_bound_review(run_id, gate, review, _artifact_store, _sonataflow,
            _load_detail, validate_review_payload, session_subject=request.state.session.subject)

    @app.delete("/api/v1/runs/{run_id}")
    async def cancel_run(run_id: str, request: Request):
        import asyncio
        detail = await asyncio.to_thread(_load_detail, run_id)
        gate = detail.get('awaitingReview')
        if gate in {'manifest', 'pre-delivery'}:
            review = {'action':'cancel', 'reviewRound':detail['reviewIteration'],
                      'artifactRevision':detail['artifactRevision']}
            await submit_bound_review(run_id, gate, review, _artifact_store, _sonataflow,
                _load_detail, validate_review_payload, session_subject=request.state.session.subject)
            return {'status':'cancelled'}
        try:
            await asyncio.to_thread(_sonataflow.cancel_instance, run_id)
        except Exception as exc:
            logger.error("Failed to cancel run run_id=%s exception_type=%s", run_id, type(exc).__name__)
            if getattr(getattr(exc, 'response', None), 'status_code', None) == 404:
                raise HTTPException(404, detail={'error':'run-not-found'}) from None
            raise HTTPException(503, detail={'error':'workflow-unavailable'}) from None
        return {"status": "cancelled"}

    @app.post("/api/v1/runs/{run_id}/rerun")
    def rerun_pipeline(run_id: str):
        try:
            original = _sonataflow.get_instance(run_id)
        except Exception as exc:
            if getattr(getattr(exc, 'response', None), 'status_code', None) == 404:
                raise HTTPException(404, detail={'error':'run-not-found'}) from None
            raise HTTPException(503, detail={'error':'workflow-unavailable'}) from None
        data = original.get("workflowdata", {})
        pdf_ref = data.get("pdf_ref", "")
        cpg_name = data.get("cpg_name", "Rerun")
        instance = _sonataflow.start_workflow(pdf_ref, cpg_name)
        return {"runId": instance["id"]}


from cpg_ingester.services.browser_auth import install_browser_auth

install_browser_auth(app, _artifact_store, mock_mode=_mock_mode)
