"""SonataFlow REST client and workflow state mapping for the BFF."""

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import requests

logger = logging.getLogger(__name__)

ACP_WRITER_BFF_URL = os.environ.get("ACP_WRITER_BFF_URL", "")

WORKFLOW_NAME = os.environ.get("SONATAFLOW_WORKFLOW_NAME", "cpgingester")

PIPELINE_STEPS = [
    "Parse", "Analyze", "ReviewManifest",
    "Generate", "ReviewArtifacts",
    "Assemble", "Deliver", "Done",
]

_STATE_TO_RUN_STATUS = {
    "Parse": "parsing",
    "Analyze": "analyzing",
    "ReviewManifest": "awaiting_manifest_review",
    "Generate": "generating",
    "ReviewArtifacts": "awaiting_artifact_review",
    "Assemble": "assembling",
    "Deliver": "delivering",
    "Done": "completed",
    "Failed": "failed",
    "Cancelled": "cancelled",
}

_CANCEL_EVENT = {
    "Parse": ("parse-done", "wait-parse"),
    "Analyze": ("analyze-done", "wait-analyze"),
    "ReviewManifest": ("manifest-reviewed", "wait-manifest-review"),
    "Generate": ("generate-done", "wait-generate"),
    "ReviewArtifacts": ("artifacts-reviewed", "wait-artifact-review"),
}

_REVIEW_GATE_MAP = {
    "ReviewManifest": "manifest",
    "ReviewArtifacts": "pre-delivery",
}

_REVIEW_EVENT_TYPE = {
    "manifest": "manifest-reviewed",
    "pre-delivery": "artifacts-reviewed",
}

_REVIEW_WAIT_PATH = {
    "manifest": "wait-manifest-review",
    "pre-delivery": "wait-artifact-review",
}


def _infer_cancelled_step(data: dict) -> str:
    """Figure out which pipeline step was active when the run was cancelled."""
    if data.get("artifactsReview", {}).get("action") == "cancel":
        return "ReviewArtifacts"
    if data.get("generateResult", {}).get("cancelled"):
        return "Generate"
    if data.get("manifestReview", {}).get("action") == "cancel":
        return "ReviewManifest"
    if data.get("analysisResult", {}).get("cancelled"):
        return "Analyze"
    if data.get("parseResult", {}).get("cancelled"):
        return "Parse"
    return "Parse"


def infer_current_state(data: dict, instance_status: str) -> str:
    """Determine the active pipeline step from workflow data."""
    if data.get("status") == "cancelled":
        return "Cancelled"
    if instance_status == "COMPLETED" or data.get("status") == "completed":
        return "Done"
    if instance_status == "ERROR":
        return "Failed"
    if instance_status == "ABORTED":
        return "Cancelled"

    if "deliveryResult" in data:
        return "Done"
    if "assemblyResult" in data:
        return "Deliver"
    artifact_review = data.get("artifactsReview", {})
    if artifact_review.get("action") == "approve":
        return "Assemble"
    if artifact_review.get("action") == "request_changes":
        review_at = artifact_review.get("completed_at", "")
        gen_at = data.get("generateResult", {}).get("completed_at", "")
        if gen_at and gen_at > review_at:
            return "ReviewArtifacts"
        return "Generate"
    if "generateResult" in data:
        return "ReviewArtifacts"

    manifest_review = data.get("manifestReview", {})
    if manifest_review.get("action") == "approve":
        return "Generate"
    if manifest_review.get("action") == "request_changes":
        review_at = manifest_review.get("completed_at", "")
        analysis_at = data.get("analysisResult", {}).get("completed_at", "")
        if analysis_at and analysis_at > review_at:
            return "ReviewManifest"
        return "Analyze"
    if "analysisResult" in data:
        return "ReviewManifest"
    if "parseResult" in data:
        return "Analyze"
    return "Parse"


def _step_timestamps(data: dict, created_at: str) -> dict[str, dict[str, str]]:
    """Build a map of step name -> {startedAt, completedAt} from result timestamps.

    Each step starts when the previous step completed. The chain:
    created_at -> Parse -> Analyze -> ReviewManifest -> Generate ->
    ReviewArtifacts -> Assemble -> Deliver -> Done
    """
    timestamps: dict[str, dict[str, str]] = {}
    prev = created_at

    step_result_keys = [
        ("Parse", "parseResult"),
        ("Analyze", "analysisResult"),
        ("ReviewManifest", "manifestReview"),
        ("Generate", "generateResult"),
        ("ReviewArtifacts", "artifactsReview"),
        ("Assemble", "assemblyResult"),
        ("Deliver", "deliveryResult"),
    ]

    for step_name, result_key in step_result_keys:
        result = data.get(result_key)
        if not result or not isinstance(result, dict):
            if prev:
                timestamps[step_name] = {"startedAt": prev}
            break
        completed = result.get("completed_at", "")
        entry: dict[str, str] = {}
        if prev:
            entry["startedAt"] = prev
        if completed:
            entry["completedAt"] = completed
        timestamps[step_name] = entry
        prev = completed or prev

    if data.get("status") == "completed" or "deliveryResult" in data:
        delivery = data.get("deliveryResult", {})
        done_at = delivery.get("completed_at", "")
        if done_at:
            timestamps["Done"] = {"startedAt": done_at, "completedAt": done_at}

    return timestamps


def build_steps(current_state: str, data: dict | None = None, created_at: str = "") -> list[dict]:
    """Build the steps array the UI expects from the current state."""
    ts = _step_timestamps(data or {}, created_at) if data else {}
    d = data or {}
    manifest_count = d.get("manifestReviewCount", 0) or 0
    artifact_count = d.get("artifactReviewCount", 0) or 0

    effective_state = current_state
    if current_state == "Cancelled":
        effective_state = _infer_cancelled_step(d)

    in_manifest_loop = effective_state in ("Analyze", "ReviewManifest") and manifest_count > 0
    in_artifact_loop = effective_state in ("Generate", "ReviewArtifacts") and artifact_count > 0

    steps = []
    reached = False
    for name in PIPELINE_STEPS:
        if name == effective_state:
            reached = True
            step: dict = {"name": name, "status": "active"}
        elif not reached:
            step = {"name": name, "status": "completed"}
        else:
            step = {"name": name, "status": "pending"}
        if name in ts:
            step.update(ts[name])
        if name in ("Analyze", "ReviewManifest"):
            if in_manifest_loop:
                step["iteration"] = manifest_count + 1
            elif manifest_count > 1:
                step["iteration"] = manifest_count
        elif name in ("Generate", "ReviewArtifacts"):
            if in_artifact_loop:
                step["iteration"] = artifact_count + 1
            elif artifact_count > 1:
                step["iteration"] = artifact_count
        steps.append(step)
    if current_state == "Done":
        for s in steps:
            s["status"] = "completed"
    if current_state == "Failed":
        for s in steps:
            if s["status"] == "active":
                s["status"] = "failed"
                break
    if current_state == "Cancelled":
        for s in steps:
            if s["status"] == "active":
                s["status"] = "cancelled"
                break
    return steps


def map_to_run_summary(instance: dict) -> dict[str, Any]:
    """Map a SonataFlow workflow instance to a RunSummary for the dashboard."""
    data = instance.get("workflowdata", {})
    status = instance.get("status", "ACTIVE")
    current_state = infer_current_state(data, status)
    display_step = current_state
    if current_state == "Cancelled":
        display_step = _infer_cancelled_step(data)

    return {
        "id": instance["id"],
        "status": _STATE_TO_RUN_STATUS.get(current_state, "parsing"),
        "cpgName": data.get("cpg_name", "Unknown CPG"),
        "createdAt": instance.get("startDate") or data.get("created_at", ""),
        "currentStep": display_step,
    }


def map_to_run_detail(instance: dict) -> dict[str, Any]:
    """Map a SonataFlow workflow instance to a RunDetail for the detail view.

    Returns workflow data as-is — artifact refs are NOT resolved here.
    Use ``artifact_resolver.enrich_run_detail`` to hydrate refs from MinIO.
    """
    data = instance.get("workflowdata", {})
    status = instance.get("status", "ACTIVE")
    current_state = infer_current_state(data, status)

    created_at = instance.get("startDate") or data.get("created_at", "")
    detail: dict[str, Any] = {
        "id": instance["id"],
        "status": _STATE_TO_RUN_STATUS.get(current_state, "parsing"),
        "cpgName": data.get("cpg_name", "Unknown CPG"),
        "createdAt": created_at,
        "steps": build_steps(current_state, data, created_at),
        "workflowData": {
            "analysisResult": data.get("analysisResult"),
            "generateResult": data.get("generateResult"),
            "assemblyResult": data.get("assemblyResult"),
            "deliveryResult": data.get("deliveryResult"),
        },
    }

    warnings = []
    if data.get("notificationWarning"):
        warnings.append({"type": "notification", "message": data["notificationWarning"]})
    if warnings:
        detail["warnings"] = warnings

    review_type = _REVIEW_GATE_MAP.get(current_state)
    if review_type:
        detail["awaitingReview"] = review_type

    if current_state == "ReviewManifest":
        detail["reviewIteration"] = data.get("manifestReviewCount", 0)
        prev = data.get("manifestReview")
        if prev and prev.get("action") == "request_changes":
            detail["previousFeedback"] = prev
    elif current_state == "ReviewArtifacts":
        detail["reviewIteration"] = data.get("artifactReviewCount", 0)
        prev = data.get("artifactsReview")
        if prev and prev.get("action") == "request_changes":
            detail["previousFeedback"] = prev

    return detail


_GRAPHQL_LIST = """
query {
  ProcessInstances(where: {processId: {equal: "%s"}}, orderBy: {start: DESC}) {
    id state start end variables
  }
}
""" % WORKFLOW_NAME

_GRAPHQL_GET = """
query ($id: String!) {
  ProcessInstances(where: {id: {equal: $id}}) {
    id state start end variables
  }
}
"""


def _graphql_to_instance(pi: dict) -> dict:
    """Normalize a GraphQL ProcessInstance to match REST API shape."""
    variables = pi.get("variables") or {}
    if isinstance(variables, str):
        import json as _json
        variables = _json.loads(variables)
    return {
        "id": pi["id"],
        "status": pi["state"],
        "startDate": pi.get("start", ""),
        "workflowdata": variables.get("workflowdata", {}),
    }


class SonataFlowClient:
    """Client for the SonataFlow cpgingester workflow.

    Uses GraphQL (embedded Data Index) for queries so completed instances
    are included.  Uses REST for mutations (start, abort, send review).
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = requests.post(
            f"{self.base_url}/graphql",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def start_workflow(self, pdf_ref: str, cpg_name: str) -> dict:
        payload = {
            "pdf_ref": pdf_ref,
            "cpg_name": cpg_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if ACP_WRITER_BFF_URL:
            payload["acpWriterUrl"] = ACP_WRITER_BFF_URL
        resp = requests.post(
            f"{self.base_url}/{WORKFLOW_NAME}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def list_instances(self) -> list[dict]:
        result = self._graphql(_GRAPHQL_LIST)
        instances = result.get("data", {}).get("ProcessInstances", [])
        return [_graphql_to_instance(pi) for pi in instances]

    def get_instance(self, instance_id: str) -> dict:
        result = self._graphql(_GRAPHQL_GET, {"id": instance_id})
        instances = result.get("data", {}).get("ProcessInstances", [])
        if not instances:
            raise requests.HTTPError(
                f"Run {instance_id} not found", response=type("R", (), {"status_code": 404})()
            )
        return _graphql_to_instance(instances[0])

    def abort_instance(self, instance_id: str) -> None:
        resp = requests.delete(
            f"{self.base_url}/{WORKFLOW_NAME}/{instance_id}",
            timeout=10,
        )
        resp.raise_for_status()

    def cancel_instance(self, instance_id: str) -> None:
        """Cancel a running workflow by sending a cancel event for the current state."""
        instance = self.get_instance(instance_id)
        data = instance.get("workflowdata", {})
        status = instance.get("status", "ACTIVE")

        if status != "ACTIVE":
            return

        current_state = infer_current_state(data, status)
        cancel_info = _CANCEL_EVENT.get(current_state)
        if not cancel_info:
            self.abort_instance(instance_id)
            return

        event_type, wait_path = cancel_info
        if current_state in ("ReviewManifest", "ReviewArtifacts"):
            cancel_data = {"action": "cancel"}
        else:
            cancel_data = {"cancelled": True}

        cloud_event = {
            "specversion": "1.0",
            "id": str(uuid4()),
            "source": "bff",
            "type": event_type,
            "kogitoprocrefid": instance_id,
            "data": cancel_data,
        }
        resp = requests.post(
            f"{self.base_url}/{wait_path}",
            json=cloud_event,
            headers={"Content-Type": "application/cloudevents+json"},
            timeout=60,
        )
        resp.raise_for_status()

    def send_review(self, instance_id: str, gate: str, review_data: dict) -> None:
        event_type = _REVIEW_EVENT_TYPE[gate]
        wait_path = _REVIEW_WAIT_PATH[gate]
        review_data.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
        cloud_event = {
            "specversion": "1.0",
            "id": str(uuid4()),
            "source": "bff",
            "type": event_type,
            "kogitoprocrefid": instance_id,
            "data": review_data,
        }
        resp = requests.post(
            f"{self.base_url}/{wait_path}",
            json=cloud_event,
            headers={"Content-Type": "application/cloudevents+json"},
            timeout=60,
        )
        resp.raise_for_status()
