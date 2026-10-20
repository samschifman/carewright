"""BFF (Backend-for-Frontend) service for the acp-writer React UI.

Mediates between the SPA and SonataFlow/MinIO/backend pods.
Each backend dependency is independently optional — the BFF
degrades gracefully when SonataFlow or MinIO is not configured.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import mlflow
import requests as http_requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from cpg_contracts.artifact_store import ArtifactStore

from acp_writer.services.ai_transparency import (
    ACTIVITY_AUTOMATION_EXT,
    AUTOMATION_ID_SYSTEM,
    AUTOMATION_PARAMETER_SOURCE_EXT,
    AUTOMATION_REVISION_SYSTEM,
    AUTOMATION_TEMPLATE_EXT,
    plan_conflict_from_provenance,
)
from acp_writer.services.artifact_resolver import _format_number, enrich_run_detail
from acp_writer.services.sonataflow_client import (
    SonataFlowClient,
    map_to_run_detail,
    map_to_run_summary,
)

logger = logging.getLogger(__name__)

SONATAFLOW_URL = os.getenv("SONATAFLOW_URL", "")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
LLM_REASONING_URL = os.getenv("LLM_REASONING_URL", "http://acp-llm-reasoning:8080")
DECISION_ENGINE_URL = os.getenv("DECISION_ENGINE_URL", "http://acp-decision-engine:8080")
FHIR_SERVER_URL = os.getenv("FHIR_SERVER_URL", "http://acp-fhir-server:8080")

_phi_store: ArtifactStore | None = None
_artifacts_store: ArtifactStore | None = None
if MINIO_ENDPOINT:
    _ak = os.getenv("ARTIFACT_STORE_ACCESS_KEY", "minioadmin")
    _sk = os.getenv("ARTIFACT_STORE_SECRET_KEY", "minioadmin")
    _phi_store = ArtifactStore(
        endpoint_url=MINIO_ENDPOINT, bucket="cpg-phi",
        access_key=_ak, secret_key=_sk,
    )
    _artifacts_store = ArtifactStore(
        endpoint_url=MINIO_ENDPOINT, bucket="cpg-artifacts",
        access_key=_ak, secret_key=_sk,
    )

_sonataflow: SonataFlowClient | None = None
if SONATAFLOW_URL:
    _sonataflow = SonataFlowClient(SONATAFLOW_URL)

_http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

app = FastAPI(title="acp-writer-bff", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "UP",
        "service": "bff",
        "minio": bool(_phi_store),
        "sonataflow": bool(SONATAFLOW_URL),
    }


# ---------------------------------------------------------------------------
# Notifications from cpg-ingester SonataFlow
# ---------------------------------------------------------------------------

@app.post("/api/v1/notifications/artifacts-available")
@mlflow.trace(name="bff.artifacts_available")
async def artifacts_available(request: Request):
    """Receive CPG artifact delivery notification from cpg-ingester.

    Pulls artifacts from MinIO and registers them with the appropriate
    backend pods so they are available for care plan generation.

    Expected payload (from cpg-ingester SonataFlow NotifyConsumers):
      {cpg_id, cpg_title, artifact_location, artifacts: [{type, ref, ...}]}
    """
    payload = await request.json()
    cpg_id = payload.get("cpg_id", "UNKNOWN")
    artifacts = payload.get("artifacts", [])
    logger.info("Received artifact notification for CPG %s (%d artifacts)", cpg_id, len(artifacts))

    results = {"cpg_id": cpg_id, "registered": [], "errors": []}

    for artifact in artifacts:
        art_type = artifact.get("type", "")
        ref = artifact.get("ref", "")
        if not ref:
            continue

        try:
            if art_type == "metadata":
                _register_metadata(ref)
                results["registered"].append({"type": "metadata", "ref": ref})

            elif art_type == "dmn":
                _register_dmn_model(ref, artifact.get("name", "unknown"), cpg_id)
                results["registered"].append({"type": "dmn", "ref": ref, "name": artifact.get("name")})

            elif art_type == "recommendations":
                count = _register_recommendations(ref, cpg_id)
                results["registered"].append({"type": "recommendations", "ref": ref, "count": count})

            elif art_type == "bpmn":
                _register_automation_template(ref, cpg_id)
                results["registered"].append({"type": "bpmn", "ref": ref})

            else:
                logger.debug("Skipping artifact type %s (ref=%s)", art_type, ref)
        except Exception as exc:
            msg = f"Failed to register {art_type} artifact {ref}: {exc}"
            logger.error(msg)
            results["errors"].append(msg)

    logger.info(
        "CPG %s: registered %d artifacts, %d errors",
        cpg_id, len(results["registered"]), len(results["errors"]),
    )
    return results


def _register_metadata(ref: str) -> None:
    """Pull CPG metadata from MinIO and register with the llm-reasoning pod."""
    if not _artifacts_store:
        raise RuntimeError("No artifact store configured")
    metadata = _artifacts_store.get(ref)
    resp = http_requests.post(
        f"{LLM_REASONING_URL}/api/v1/guidelines",
        json=metadata,
        timeout=10,
    )
    resp.raise_for_status()
    logger.info("Registered CPG metadata: %s", metadata.get("cpg_id", "?"))


def _register_dmn_model(ref: str, name: str, cpg_id: str | None = None) -> None:
    """Pull DMN XML from MinIO and deploy to the decision-engine pod."""
    if not _artifacts_store:
        raise RuntimeError("No artifact store configured")
    dmn_xml = _artifacts_store.get_raw(ref)
    url = f"{DECISION_ENGINE_URL}/api/v1/decisions/models"
    if cpg_id:
        url += f"?source_cpg={cpg_id}"
    resp = http_requests.post(
        url,
        data=dmn_xml,
        headers={"Content-Type": "application/xml"},
        timeout=10,
    )
    resp.raise_for_status()
    logger.info("Registered DMN model: %s (source_cpg=%s)", name, cpg_id)


def _register_recommendations(ref: str, cpg_id: str) -> int:
    """Pull recommendation bundle from MinIO and ingest into the llm-reasoning pod."""
    if not _artifacts_store:
        raise RuntimeError("No artifact store configured")
    bundle = _artifacts_store.get(ref)
    resp = http_requests.post(
        f"{LLM_REASONING_URL}/api/v1/knowledge/recommendations/batch",
        json=bundle,
        timeout=30,
    )
    resp.raise_for_status()
    count = len(bundle.get("recommendations", []))
    logger.info("Registered %d recommendations for CPG %s", count, cpg_id)
    return count


@mlflow.trace(name="bff.register_automation_template")
def _register_automation_template(ref: str, cpg_id: str) -> None:
    """Pull a published template from MinIO and register it with decision-engine."""
    if not _artifacts_store:
        raise RuntimeError("No artifact store configured")
    template = _artifacts_store.get(ref)
    response = http_requests.post(
        f"{DECISION_ENGINE_URL}/api/v1/automation/templates",
        params={"source_cpg": cpg_id},
        json=template,
        timeout=30,
    )
    response.raise_for_status()
    logger.info(
        "Registered automation template %s version %s for CPG %s",
        template.get("summary", {}).get("id", "?"),
        template.get("summary", {}).get("version", "?"),
        cpg_id,
    )


@mlflow.trace(name="bff.reload_cpg_artifacts")
def _reload_cpg_artifacts() -> None:
    """Scan MinIO published/ prefix and re-register all CPG artifacts.

    Called at startup to survive pod restarts — any previously delivered
    CPGs are reloaded into the backend pods' in-memory stores.
    """
    if not _artifacts_store:
        return
    try:
        client = _artifacts_store._get_client()
        response = client.list_objects_v2(
            Bucket="cpg-artifacts", Prefix="published/", Delimiter="/",
        )
        prefixes = [p["Prefix"] for p in response.get("CommonPrefixes", [])]
    except Exception as exc:
        logger.warning("Could not scan MinIO for published CPGs: %s", exc)
        return

    for prefix in prefixes:
        cpg_id = prefix.rstrip("/").split("/")[-1]
        logger.info("Reloading CPG artifacts for %s", cpg_id)
        metadata_ref = f"cpg-artifacts:{prefix}metadata.json"
        try:
            _register_metadata(metadata_ref)
        except Exception as exc:
            logger.warning("Failed to reload metadata for %s: %s", cpg_id, exc)

        try:
            dmn_resp = client.list_objects_v2(
                Bucket="cpg-artifacts", Prefix=f"{prefix}dmn/",
            )
            for obj in dmn_resp.get("Contents", []):
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1].removesuffix(".dmn")
                _register_dmn_model(f"cpg-artifacts:{key}", name)
        except Exception as exc:
            logger.warning("Failed to reload DMN models for %s: %s", cpg_id, exc)

        rec_ref = f"cpg-artifacts:{prefix}recommendations.json"
        try:
            _register_recommendations(rec_ref, cpg_id)
        except Exception as exc:
            logger.warning("Failed to reload recommendations for %s: %s", cpg_id, exc)

        try:
            bpmn_resp = client.list_objects_v2(
                Bucket="cpg-artifacts", Prefix=f"{prefix}bpmn/",
            )
            objects = bpmn_resp.get("Contents", [])
            latest_versions = {}
            for obj in objects:
                key = obj["Key"]
                if not key.endswith("/latest.json"):
                    continue
                try:
                    pointer = _artifacts_store.get(f"cpg-artifacts:{key}")
                    latest_versions[key.rsplit("/", 2)[-2]] = pointer.get("version")
                except Exception as exc:
                    logger.warning("Could not read latest template pointer %s: %s", key, exc)
            templates = [
                obj["Key"] for obj in objects
                if obj["Key"].endswith(".template.json")
            ]
            templates.sort(key=lambda key: (
                key.rsplit("/", 2)[-2],
                key.rsplit("/", 1)[-1].removesuffix(".template.json")
                == latest_versions.get(key.rsplit("/", 2)[-2]),
                key,
            ))
            for key in templates:
                _register_automation_template(f"cpg-artifacts:{key}", cpg_id)
        except Exception as exc:
            logger.warning("Failed to reload automation templates for %s: %s", cpg_id, exc)


# ---------------------------------------------------------------------------
# SonataFlow review notification (no-op acknowledgment)
# ---------------------------------------------------------------------------

@app.post("/api/v1/notify-review")
def notify_review(payload: dict | None = None):
    """Called by SonataFlow when a review callback state is entered."""
    logger.info("Review notification: %s", payload)
    return {"status": "acknowledged"}


# ---------------------------------------------------------------------------
# Run management (SonataFlow-backed, async start)
# ---------------------------------------------------------------------------

# Pending runs: BFF-generated ID → tracking state.  Entries are promoted
# to SonataFlow-backed once the background start_workflow call returns.
_pending_runs: dict[str, dict] = {}


async def _start_workflow_background(run_id: str, ips_ref: str, patient_name: str) -> None:
    """Start SonataFlow workflow in the background and update the pending-run mapping."""
    try:
        assert _sonataflow is not None
        instance = await _sonataflow.start_workflow(ips_ref, patient_name, business_key=run_id)
        _pending_runs[run_id]["sonataflow_id"] = instance["id"]
        _pending_runs[run_id]["status"] = "running"
        logger.info("Run %s → SonataFlow instance %s", run_id, instance["id"])
    except Exception as exc:
        _pending_runs[run_id]["status"] = "error"
        _pending_runs[run_id]["error"] = str(exc)
        logger.error("Failed to start workflow for run %s: %s", run_id, exc)


def _pending_run_summary(pr: dict) -> dict:
    return {
        "runId": pr["run_id"],
        "status": "starting" if pr["status"] == "starting" else pr["status"],
        "patientName": pr.get("patient_name", "Unknown Patient"),
        "patientReference": "",
        "currentSteps": [],
        "careplanId": None,
        "createdAt": pr["created_at"],
        "updatedAt": pr["created_at"],
    }


def _pending_run_detail(pr: dict) -> dict:
    return {
        "runId": pr["run_id"],
        "status": "starting" if pr["status"] == "starting" else pr["status"],
        "createdAt": pr["created_at"],
        "updatedAt": pr["created_at"],
        "currentSteps": [],
        "steps": [],
        "awaitingReview": None,
        "carePlan": None,
        "reviewIteration": 0,
        "previousFeedback": None,
        "careplanId": None,
        "error": pr.get("error"),
        "workflowData": None,
    }


@app.post("/api/v1/runs", status_code=202)
@mlflow.trace(name="bff.create_run")
async def create_run(request: Request):
    """Accept an IPS bundle, store in MinIO, and start a SonataFlow workflow.

    Returns immediately with a run ID. The SonataFlow workflow is started
    in the background — the UI polls GET /runs/{id} for progress.
    """
    body = await request.json()
    ips_bundle = body.get("ipsBundle")
    if not ips_bundle:
        return JSONResponse(status_code=400, content={"message": "ipsBundle is required"})

    patient_name = _extract_patient_name(ips_bundle)
    run_id = uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()

    ips_ref = ""
    if _phi_store:
        ips_ref = _phi_store.put(f"{run_id}/ips_bundle.json", ips_bundle)

    _pending_runs[run_id] = {
        "run_id": run_id,
        "status": "starting",
        "sonataflow_id": None,
        "patient_name": patient_name,
        "ips_ref": ips_ref,
        "created_at": now,
        "error": None,
    }

    if _sonataflow:
        asyncio.create_task(_start_workflow_background(run_id, ips_ref or json.dumps(ips_bundle), patient_name))

    return {"runId": run_id, "status": "starting"}


def _resolve_sonataflow_id(run_id: str) -> str | None:
    """If run_id is a BFF pending-run ID, return its SonataFlow instance ID (if available)."""
    pr = _pending_runs.get(run_id)
    if pr:
        return pr.get("sonataflow_id")
    return None


async def _resolve_sf_id(run_id: str) -> str | None:
    """Resolve a run_id (the workflow business key) to its SonataFlow instance ID.

    Prefers the in-memory pending-run mapping; falls back to a business-key
    lookup in the data-index. A raw run_id is never a valid instance ID, so the
    fallback is what lets existing runs still be found after a BFF restart (when
    the in-memory map is empty).
    """
    sf_id = _resolve_sonataflow_id(run_id)
    if sf_id:
        return sf_id
    if _sonataflow:
        try:
            instance = await _sonataflow.get_instance_by_business_key(run_id)
        except Exception:
            instance = None
        if instance:
            return instance["id"]
    return None


@app.get("/api/v1/runs")
async def list_runs(status: str | None = None, limit: int = 50):
    summaries: list[dict] = []
    sf_ids_in_list: set[str] = set()

    if _sonataflow:
        instances = await _sonataflow.list_instances()
        sf_ids_in_list = {inst["id"] for inst in instances}
        for inst in instances:
            summary = map_to_run_summary(inst)
            # If this instance was started with a business key matching a pending run, use that ID
            for pr in _pending_runs.values():
                if pr.get("sonataflow_id") == inst["id"]:
                    summary["runId"] = pr["run_id"]
                    break
            summaries.append(summary)

    # Include pending runs not yet visible in SonataFlow
    for pr in _pending_runs.values():
        if not pr.get("sonataflow_id") or (pr.get("sonataflow_id") and _sonataflow and pr["sonataflow_id"] not in sf_ids_in_list):
            summaries.append(_pending_run_summary(pr))

    if status:
        summaries = [s for s in summaries if s["status"] == status]
    summaries.sort(key=lambda s: s.get("createdAt", ""), reverse=True)
    return summaries[:limit]


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str):
    # Check pending runs first
    pr = _pending_runs.get(run_id)
    if pr:
        sf_id = pr.get("sonataflow_id")
        if _sonataflow:
            # Try direct ID lookup first, then business key for in-flight workflows
            instance = None
            if sf_id:
                try:
                    instance = await _sonataflow.get_instance(sf_id)
                except Exception:
                    pass
            if not instance:
                try:
                    instance = await _sonataflow.get_instance_by_business_key(run_id)
                except Exception:
                    pass
            if instance:
                if not sf_id:
                    pr["sonataflow_id"] = instance["id"]
                    pr["status"] = "running"
                detail = map_to_run_detail(instance)
                detail["runId"] = run_id
                return enrich_run_detail(detail, _phi_store, _artifacts_store)
        return _pending_run_detail(pr)

    # Fall through: run not in the in-memory map (e.g. after a BFF restart).
    # run_id is the workflow business key; try a direct id lookup first (legacy
    # runs addressed by instance id), then resolve by business key.
    if not _sonataflow:
        return JSONResponse(status_code=503, content={"message": "SonataFlow not configured"})
    instance = None
    try:
        instance = await _sonataflow.get_instance(run_id)
    except Exception:
        instance = None
    if not instance:
        try:
            instance = await _sonataflow.get_instance_by_business_key(run_id)
        except Exception:
            instance = None
    if not instance:
        return JSONResponse(status_code=404, content={"message": f"Run {run_id} not found"})
    detail = map_to_run_detail(instance)
    return enrich_run_detail(detail, _phi_store, _artifacts_store)


@app.post("/api/v1/runs/{run_id}/review/{gate}")
@mlflow.trace(name="bff.submit_review")
async def submit_review(run_id: str, gate: str, request: Request):
    if gate != "careplan":
        return JSONResponse(status_code=400, content={"message": f"Unknown gate: {gate}"})
    if not _sonataflow:
        return JSONResponse(status_code=503, content={"message": "SonataFlow not configured"})

    sf_id = await _resolve_sf_id(run_id)
    if not sf_id:
        return JSONResponse(status_code=404, content={"message": f"Run {run_id} not found"})
    try:
        instance = await _sonataflow.get_instance(sf_id)
    except Exception:
        return JSONResponse(status_code=404, content={"message": f"Run {run_id} not found"})

    pre_detail = map_to_run_detail(instance)
    # Fast-feedback courtesy only. The data-index this reads lags the engine, so
    # this check can be wrong either way — but harmlessly: a wrongly-passed check
    # ends in an engine-side round discard + UI retry, and a wrongly-failed one
    # shows a clear 409. The authoritative guard is round-binding in the workflow
    # (ValidateReviewRound), not this pre-check.
    if pre_detail.get("awaitingReview") != gate:
        return JSONResponse(status_code=409, content={"message": "Run is not awaiting careplan review"})

    review = await request.json()
    try:
        # Forwarded verbatim into the CloudEvent data, so the reviewRound the UI
        # sent reaches the workflow's round guard unchanged (single spelling,
        # camelCase, end to end — no translation point to disagree).
        await _sonataflow.send_review(sf_id, gate, review or {})
    except Exception:
        logger.exception("send_review failed for run %s gate %s", run_id, gate)
        return JSONResponse(
            status_code=503,
            content={"message": "Workflow engine temporarily unavailable — please try again."},
        )

    try:
        updated = await _sonataflow.get_instance(sf_id)
        updated_detail = map_to_run_detail(updated)
        updated_detail["runId"] = run_id
        enriched = enrich_run_detail(updated_detail, _phi_store, _artifacts_store)
        return JSONResponse(status_code=202, content=enriched)
    except Exception:
        return JSONResponse(status_code=202, content={"runId": run_id, "status": "running", "steps": []})


@app.delete("/api/v1/runs/{run_id}")
async def cancel_run(run_id: str):
    if not _sonataflow:
        return JSONResponse(status_code=503, content={"message": "SonataFlow not configured"})
    sf_id = await _resolve_sf_id(run_id)
    if not sf_id:
        return JSONResponse(status_code=404, content={"message": f"Run {run_id} not found"})
    try:
        await _sonataflow.abort_instance(sf_id)
    except Exception:
        return JSONResponse(status_code=404, content={"message": f"Run {run_id} not found"})
    pr = _pending_runs.get(run_id)
    if pr:
        pr["status"] = "cancelled"
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Artifact proxy (MinIO)
# ---------------------------------------------------------------------------

@app.get("/api/v1/runs/{run_id}/artifacts/{path:path}")
def get_artifact(run_id: str, path: str):
    store = _phi_store or _artifacts_store
    if not store:
        return JSONResponse(status_code=503, content={"message": "No artifact store configured"})
    key = f"{run_id}/{path}"
    try:
        data = store.get_raw(key)
    except Exception:
        return JSONResponse(status_code=404, content={"message": f"Artifact not found: {key}"})
    return Response(content=data, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Care plan proxy (fhir-server pod)
# ---------------------------------------------------------------------------

@app.get("/api/v1/careplans")
async def list_careplans(patient: str | None = None, status: str | None = None):
    params = {}
    if patient:
        params["patient"] = patient
    if status:
        params["status"] = status
    try:
        resp = await _http.get(f"{FHIR_SERVER_URL}/api/v1/careplans", params=params)
        resp.raise_for_status()
        raw_plans = resp.json()
    except Exception as exc:
        logger.error("Failed to list care plans: %s", exc)
        return JSONResponse(status_code=502, content={"message": "Failed to reach FHIR server"})
    return [_to_careplan_summary(cp) for cp in (raw_plans if isinstance(raw_plans, list) else [])]


@app.get("/api/v1/careplans/{careplan_id}")
async def get_careplan(careplan_id: str):
    try:
        resp = await _http.get(f"{FHIR_SERVER_URL}/api/v1/careplans/{careplan_id}")
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        logger.error("Failed to get care plan %s: %s", careplan_id, exc)
        return JSONResponse(status_code=502, content={"message": "Failed to reach FHIR server"})
    if not raw:
        return JSONResponse(status_code=404, content={"message": f"Care plan {careplan_id} not found"})
    detail = _to_careplan_detail(raw)
    await _merge_brief_automations(detail, careplan_id)
    return detail


async def _merge_brief_automations(detail: dict, careplan_id: str) -> None:
    """Add the planning brief's automations to a persisted care plan view.

    The FHIR bundle carries only enabled, valid automations (the
    activity-automation extension). Incomplete, escalated and disabled ones
    live in the run's planning brief, which is what the review gate shows;
    the persisted view should show the same set with the same statuses.
    """
    if not _sonataflow or not _phi_store:
        return
    try:
        instances = await _sonataflow.list_instances()
    except Exception as exc:
        logger.warning("Could not list runs to enrich care plan %s: %s", careplan_id, exc)
        return
    for inst in instances:
        summary = map_to_run_summary(inst)
        if summary.get("careplanId") != careplan_id:
            continue
        try:
            full = await _sonataflow.get_instance(inst["id"])
        except Exception as exc:
            logger.warning("Could not load run %s for care plan %s: %s", inst.get("id"), careplan_id, exc)
            return
        run_detail = enrich_run_detail(map_to_run_detail(full), _phi_store, _artifacts_store)
        if not detail.get("runId"):
            detail["runId"] = summary.get("runId") or run_detail.get("runId")
        brief_activities = (run_detail.get("carePlan") or {}).get("activities") or []
        by_id = {a.get("id"): a for a in brief_activities if a.get("id")}
        for activity in (detail.get("view") or {}).get("activities") or []:
            source = by_id.get(activity.get("id"))
            if not source or not source.get("automations"):
                continue
            merged = list(activity.get("automations") or [])
            seen = {a.get("id") for a in merged}
            for automation in source["automations"]:
                if automation.get("id") not in seen:
                    merged.append(automation)
                    seen.add(automation.get("id"))
            activity["automations"] = merged
        return


# ---------------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------------

@app.get("/api/v1/status")
async def system_status():
    result: dict = {
        "version": "0.1.0",
        "decisionEngine": {"available": False, "modelsDeployed": 0, "decisions": []},
        "knowledgeBase": {"available": False, "guidelines": 0, "recommendations": 0, "cpgs": []},
    }
    try:
        resp = await _http.get(f"{LLM_REASONING_URL}/api/v1/status", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            de = data.get("decision_engine", {})
            kb = data.get("knowledge_base", {})
            result["decisionEngine"] = {
                "available": de.get("status") == "healthy",
                "modelsDeployed": de.get("models_deployed", 0),
                "decisions": [],
            }
            result["knowledgeBase"] = {
                "available": kb.get("status") == "healthy",
                "guidelines": kb.get("guidelines_registered", 0),
                "recommendations": kb.get("recommendations_ingested", 0),
                "cpgs": [],
            }
    except Exception:
        pass

    try:
        resp = http_requests.get(f"{LLM_REASONING_URL}/api/v1/decisions/models", timeout=5)
        if resp.status_code == 200:
            result["decisionEngine"]["decisions"] = [
                {"id": m.get("id", ""), "name": m.get("name", ""), "sourceCpg": m.get("source_cpg")}
                for m in resp.json()
            ]
    except Exception:
        pass

    try:
        resp = http_requests.get(f"{LLM_REASONING_URL}/api/v1/guidelines", timeout=5)
        if resp.status_code == 200:
            result["knowledgeBase"]["cpgs"] = [
                {"cpgId": g.get("cpg_id", ""), "title": g.get("title", ""), "version": g.get("version"), "issuingBody": g.get("issuing_body")}
                for g in resp.json()
            ]
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_patient_name(ips_bundle: dict) -> str:
    """Extract patient name from a FHIR IPS bundle."""
    for entry in ips_bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Patient":
            for name in resource.get("name", []):
                parts = []
                if name.get("given"):
                    parts.extend(name["given"])
                if name.get("family"):
                    parts.append(name["family"])
                if parts:
                    return " ".join(parts)
    return "Unknown Patient"


def _extract_patient_name_from_bundle(bundle: dict) -> str:
    """Extract patient display name from a stored FHIR care-plan bundle."""
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Patient":
            for name in resource.get("name", []):
                parts = []
                if name.get("given"):
                    parts.extend(name["given"])
                if name.get("family"):
                    parts.append(name["family"])
                if parts:
                    return " ".join(parts)
    return ""


def _to_careplan_summary(raw: dict) -> dict:
    """Shape a raw care-plan record into a CarePlanSummary view-model."""
    return {
        "id": raw.get("id", ""),
        "patientName": raw.get("patient_name", ""),
        "patientReference": raw.get("patient_reference", ""),
        "status": raw.get("status", ""),
        "generatedAt": raw.get("generated_at"),
        "runId": raw.get("run_id"),
    }


def _to_careplan_detail(raw: dict) -> dict:
    """Shape a raw care-plan record into a CarePlanDetail view-model."""
    bundle = raw.get("bundle", {})
    summary = _to_careplan_summary(raw)
    patient_name = summary["patientName"] or _extract_patient_name_from_bundle(bundle)
    if patient_name:
        summary["patientName"] = patient_name

    patient = _extract_patient_from_bundle(bundle)
    goals, activities, conflicts = _extract_view_from_bundle(bundle)

    return {
        **summary,
        "patient": patient,
        "view": {
            "goals": goals,
            "activities": activities,
            "conflicts": conflicts,
            "fhirBundle": bundle if bundle.get("entry") else None,
        },
    }


def _extract_patient_from_bundle(bundle: dict) -> dict | None:
    """Extract PatientSummary from a stored FHIR care-plan bundle."""
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Patient":
            name = ""
            for n in resource.get("name", []):
                if isinstance(n, dict):
                    text = n.get("text")
                    if text:
                        name = text
                        break
                    parts = []
                    if n.get("given"):
                        parts.extend(n["given"])
                    if n.get("family"):
                        parts.append(n["family"])
                    if parts:
                        name = " ".join(parts)
                        break
            return {
                "name": name,
                "birthDate": resource.get("birthDate", ""),
                "gender": resource.get("gender", ""),
                "patientReference": "",
                "conditions": [],
                "medications": [],
                "allergies": [],
                "observations": [],
            }
    return None


@mlflow.trace(name="bff.extract_careplan_view")
def _extract_view_from_bundle(bundle: dict) -> tuple[list, list, list]:
    """Extract goals, activities, and conflicts from a FHIR CarePlan bundle."""
    entries = bundle.get("entry", [])
    resources = {}
    resource_items = []
    for entry in entries:
        resource = entry.get("resource", {})
        full_url = entry.get("fullUrl")
        canonical_ref = full_url or (
            f"{resource.get('resourceType')}/{resource.get('id')}"
            if resource.get("resourceType") and resource.get("id") else ""
        )
        if canonical_ref:
            resource_items.append((canonical_ref, resource))
        if full_url:
            resources[full_url] = resource
        if resource.get("resourceType") and resource.get("id"):
            resources[f"{resource['resourceType']}/{resource['id']}"] = resource

    source_cpgs = _extract_source_cpgs(resources)

    goals = []
    for url, r in resource_items:
        if r.get("resourceType") == "Goal":
            target_text = _format_goal_target(r.get("target", []))
            goals.append({
                "id": r.get("id", ""),
                "description": r.get("description", {}).get("text", ""),
                "target": target_text or None,
                "sourceCpgId": source_cpgs.get(url),
            })

    careplan = next((r for _url, r in resource_items if r.get("resourceType") == "CarePlan"), {})
    careplan_activities = careplan.get("activity", [])
    task_activity_ids: dict[str, list[str]] = {}
    for act in careplan_activities:
        activity_id = act.get("id")
        for extension in act.get("extension", []) or []:
            if extension.get("url") != ACTIVITY_AUTOMATION_EXT:
                continue
            fields = {item.get("url"): item for item in extension.get("extension", [])}
            task_ref = fields.get("instance", {}).get("valueReference", {}).get("reference")
            if activity_id and task_ref:
                ids = task_activity_ids.setdefault(task_ref, [])
                if activity_id not in ids:
                    ids.append(activity_id)

    activities = []
    represented_resource_refs = set()
    for act in careplan_activities:
        reference = act.get("reference", {}).get("reference")
        resource = resources.get(reference, {}) if reference else {}
        if reference:
            represented_resource_refs.add(reference)
        detail = act.get("detail")
        description = ""
        extra_detail = ""
        if resource.get("resourceType") == "MedicationRequest":
            description = resource.get("medicationCodeableConcept", {}).get("text", "")
            extra_detail = "; ".join(
                item.get("text", "") for item in resource.get("dosageInstruction", [])
            )
        elif resource.get("resourceType") == "ServiceRequest":
            description = resource.get("code", {}).get("text", "")
            extra_detail = "; ".join(item.get("text", "") for item in resource.get("note", []))
        if detail:
            description = detail.get("description") or detail.get("code", {}).get("text") or description
        activity: dict = {
            "id": act.get("id") or resource.get("id", ""),
            "description": description,
            "goalId": None,
        }
        if extra_detail:
            activity["detail"] = extra_detail
        elif detail and detail.get("description"):
            activity["detail"] = detail["description"]
        automations = []
        for extension in act.get("extension", []) or []:
            if extension.get("url") != ACTIVITY_AUTOMATION_EXT:
                continue
            automation = _automation_view_from_fhir_extension(
                extension, resources, task_activity_ids
            )
            if automation:
                automations.append(automation)
        if automations:
            activity["automations"] = automations
        activities.append(activity)

    # Preserve old bundles whose CarePlan omitted activity rows.
    for reference, resource in resource_items:
        if reference in represented_resource_refs:
            continue
        if resource.get("resourceType") == "MedicationRequest":
            activities.append({
                "id": resource.get("id", ""),
                "description": resource.get("medicationCodeableConcept", {}).get("text", ""),
                "goalId": None,
                "detail": "; ".join(
                    item.get("text", "") for item in resource.get("dosageInstruction", [])
                ),
            })
        elif resource.get("resourceType") == "ServiceRequest":
            activities.append({
                "id": resource.get("id", ""),
                "description": resource.get("code", {}).get("text", ""),
                "goalId": None,
                "detail": "; ".join(item.get("text", "") for item in resource.get("note", [])),
            })

    conflicts = []
    for _url, r in resource_items:
        if r.get("resourceType") != "Provenance":
            continue
        # plan_conflict_from_provenance returns None for any Provenance without
        # the conflict-id extension, so it doubles as the conflict filter (C6).
        pc = plan_conflict_from_provenance(r)
        if pc:
            conflicts.append(pc)

    return goals, activities, conflicts


@mlflow.trace(name="bff.read_activity_automation")
def _automation_view_from_fhir_extension(
    extension: dict, resources: dict, task_activity_ids: dict[str, list[str]]
) -> dict | None:
    """Reconstruct the UI automation summary from linked Task and DocumentReference resources."""
    fields = {item.get("url"): item for item in extension.get("extension", [])}
    task_ref = fields.get("instance", {}).get("valueReference", {}).get("reference")
    docref_ref = fields.get("definition", {}).get("valueReference", {}).get("reference")
    task = resources.get(task_ref, {}) if task_ref else {}
    docref = resources.get(docref_ref, {}) if docref_ref else {}
    if task.get("resourceType") != "Task" or docref.get("resourceType") != "DocumentReference":
        return None

    identifiers = {item.get("system"): item.get("value") for item in task.get("identifier", [])}
    template_refs = []
    for item in task.get("extension", []) or []:
        if item.get("url") != AUTOMATION_TEMPLATE_EXT:
            continue
        template_fields = {field.get("url"): field for field in item.get("extension", [])}
        ref = {
            "templateId": template_fields.get("templateId", {}).get("valueString", ""),
            "version": template_fields.get("version", {}).get("valueString", ""),
        }
        source_cpg = template_fields.get("sourceCpg", {}).get("valueString")
        if source_cpg:
            ref["sourceCpg"] = source_cpg
        template_refs.append(ref)

    bindings = []
    for item in task.get("input", []) or []:
        coding = (item.get("type", {}).get("coding") or [{}])[0]
        if coding.get("code") != "parameter":
            continue
        name = item.get("type", {}).get("text") or coding.get("text")
        if not name:
            continue
        value = next((value for key, value in item.items() if key.startswith("value") and key != "valueReference"), None)
        unit = None
        if isinstance(value, dict):
            if "valueQuantity" in item:
                unit = value.get("code") or value.get("unit")
                value = value.get("value")
            elif "valueDuration" in item:
                amount = value.get("value")
                unit = value.get("code") or value.get("unit")
                value = amount if amount is not None else ""
            elif "valueCodeableConcept" in item:
                code = (value.get("coding") or [{}])[0]
                value = f"{code.get('system', '')}|{code.get('code', '')}"
        source_extension = next((
            child for child in item.get("extension", [])
            if child.get("url") == AUTOMATION_PARAMETER_SOURCE_EXT
        ), {})
        binding = {
            "name": name,
            "value": value,
            "source": source_extension.get("valueCode", "authored"),
        }
        if unit:
            binding["unit"] = unit
        bindings.append(binding)

    attachment = ((docref.get("content") or [{}])[0].get("attachment") or {})
    encoded = attachment.get("data", "")
    try:
        bpmn_xml = base64.b64decode(encoded, validate=True).decode("utf-8") if encoded else ""
    except (ValueError, binascii.Error, UnicodeDecodeError):
        bpmn_xml = ""
    status = next((
        child.get("valueCode") for child in extension.get("extension", [])
        if child.get("url") == "status"
    ), "enabled")
    revision = identifiers.get(AUTOMATION_REVISION_SYSTEM, "")
    return {
        "id": identifiers.get(AUTOMATION_ID_SYSTEM, ""),
        "templateRefs": template_refs,
        "activityIds": task_activity_ids.get(task_ref, []),
        "enabled": status == "enabled",
        "revision": revision,
        "bpmnXml": bpmn_xml,
        "capabilitiesUsed": [],
        "bindings": bindings,
        "validation": {"status": "valid", "rungsPassed": [], "warnings": [], "errors": []},
        "reviewNotes": [],
    }


def _format_goal_target(targets: list) -> str:
    """Format Goal.target[] into a human-readable string, e.g. "HbA1c < 7 %"."""
    parts = []
    for t in targets:
        measure = t.get("measure", {}).get("text", "")
        detail_range = t.get("detailRange", {})
        low = detail_range.get("low", {})
        high = detail_range.get("high", {})
        if not measure:
            continue
        if high and low:
            parts.append(f"{measure}: {_format_number(low.get('value'))}–{_format_number(high.get('value'))} {high.get('unit', '')}".rstrip())
        elif high:
            parts.append(f"{measure} < {_format_number(high.get('value'))} {high.get('unit', '')}".rstrip())
        elif low:
            parts.append(f"{measure} > {_format_number(low.get('value'))} {low.get('unit', '')}".rstrip())
        else:
            parts.append(measure)
    return "; ".join(parts)


def _extract_source_cpgs(resources: dict) -> dict:
    """Map resource fullUrls to source CPG IDs from Provenance resources."""
    cpg_map: dict[str, str] = {}
    for r in resources.values():
        if r.get("resourceType") != "Provenance":
            continue
        cpg_names = []
        for entity in r.get("entity", []):
            if entity.get("role") == "derivation":
                display = entity.get("what", {}).get("display", "")
                if display.startswith("CPG: "):
                    cpg_names.append(display[5:])
        if cpg_names:
            cpg_str = ", ".join(cpg_names)
            for target in r.get("target", []):
                ref = target.get("reference", "")
                if ref:
                    cpg_map[ref] = cpg_str
    return cpg_map


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _on_startup():
    logger.info(
        "acp-writer BFF starting (sonataflow=%s, minio=%s)",
        bool(SONATAFLOW_URL), bool(MINIO_ENDPOINT),
    )
    _reload_cpg_artifacts()


@app.on_event("shutdown")
async def _on_shutdown():
    await _http.aclose()
    if _sonataflow:
        await _sonataflow.close()
