"""FHIR Server Writer — POST transaction Bundle to a FHIR R4 server.

Validates OperationOutcome response and stores care plan reference.
Supports approve/reject workflow with AI Transparency tag transitions.
Care plans are POSTed as "draft" and updated to "active" or
"entered-in-error" on the server on approve/reject.

Auth: If FHIR_CLIENT_ID and FHIR_CLIENT_SECRET are set, uses OAuth
client_credentials to obtain a Bearer token. Otherwise sends
unauthenticated requests (for HAPI FHIR or local dev).
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import mlflow
import requests

from acp_writer.output import write_artifact
from acp_writer.services.ai_transparency import (
    ACP_CS_BASE,
    AUTOMATION_ID_SYSTEM,
    AUTOMATION_REVISION_SYSTEM,
    apply_approval_transition,
)
from acp_writer.state import CarePlanComposerState

if TYPE_CHECKING:
    from acp_writer.services.reviewer import ReviewerContext

logger = logging.getLogger(__name__)

FHIR_SERVER_URL = os.environ.get("FHIR_SERVER_URL", "http://localhost:8103/fhir/R4")
FHIR_CLIENT_ID = os.environ.get("FHIR_CLIENT_ID", "")
FHIR_CLIENT_SECRET = os.environ.get("FHIR_CLIENT_SECRET", "")

_care_plans: dict[str, dict] = {}

_token_cache: dict[str, str | float] = {"token": "", "expires_at": 0.0}


def _get_auth_headers() -> dict[str, str]:
    """Get authorization headers for FHIR server requests.

    Uses client_credentials OAuth if FHIR_CLIENT_ID is configured.
    Returns empty dict for unauthenticated servers.
    """
    if not FHIR_CLIENT_ID or not FHIR_CLIENT_SECRET:
        return {}

    now = time.time()
    if _token_cache["token"] and float(_token_cache["expires_at"]) > now + 60:
        return {"Authorization": f"Bearer {_token_cache['token']}"}

    token_url = FHIR_SERVER_URL.rstrip("/").rsplit("/fhir", 1)[0] + "/oauth2/token"
    try:
        r = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": FHIR_CLIENT_ID,
                "client_secret": FHIR_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = now + data.get("expires_in", 3600)
        logger.info("Acquired FHIR server OAuth token (expires in %ds)", data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {_token_cache['token']}"}
    except Exception as e:
        logger.warning("Failed to acquire FHIR OAuth token: %s", e)
        return {}


def _parse_server_ids(bundle: dict, response_data: dict) -> dict[str, str]:
    """Map urn:uuid: fullUrls to server-assigned IDs from a transaction response."""
    id_map: dict[str, str] = {}
    request_entries = bundle.get("entry", [])
    response_entries = response_data.get("entry", [])
    for req_entry, resp_entry in zip(request_entries, response_entries):
        full_url = req_entry.get("fullUrl", "")
        location = resp_entry.get("response", {}).get("location", "")
        if full_url and location:
            parts = location.split("/")
            if len(parts) >= 2:
                server_ref = f"{parts[0]}/{parts[1]}"
                id_map[full_url] = server_ref
    return id_map


def _find_careplan_server_id(id_map: dict[str, str]) -> str | None:
    """Find the server-assigned CarePlan reference from the ID map."""
    for urn, server_ref in id_map.items():
        if server_ref.startswith("CarePlan/"):
            return server_ref
    return None


def _update_on_server(server_ref: str, resource: dict) -> bool:
    """PUT an updated resource to the FHIR server. Returns True on success."""
    url = f"{FHIR_SERVER_URL}/{server_ref}"
    try:
        headers = {"Content-Type": "application/fhir+json", **_get_auth_headers()}
        r = requests.put(
            url,
            json=resource,
            headers=headers,
            timeout=30,
        )
        if r.status_code in (200, 201):
            logger.info("Updated %s on FHIR server", server_ref)
            return True
        else:
            logger.warning("FHIR server PUT %s returned %d: %s", server_ref, r.status_code, r.text[:200])
            return False
    except requests.RequestException as e:
        logger.warning("FHIR server unavailable for PUT %s: %s", server_ref, e)
        return False


@mlflow.trace(name="fhir_server_writer")
def fhir_server_writer(state: CarePlanComposerState) -> dict:
    """Store a draft locally or publish an already-approved FHIR Bundle."""
    logger.info("── FHIR Server Writer ──")
    bundle = state.get("fhir_bundle", {})
    output_dir = state.get("output_dir", "")

    if not bundle.get("entry"):
        logger.info("Empty FHIR bundle — skipping server write")
        return {"delivery_status": "skipped", "careplan_id": ""}

    careplan_id = state.get("careplan_id") or str(uuid.uuid4())
    _set_careplan_identifier(bundle, careplan_id)
    initial_status = "active" if state.get("approved", False) else "draft"

    approval_job = None
    if initial_status == "active":
        # Deployed (SonataFlow) approval lands here with approved=True. Apply the
        # SAME full transition the monolith approve endpoint uses so the split
        # path also records the human verifier and acknowledges conflicts — not
        # just the security-tag swap it did before (issue #169 F1).
        reviewer = _state_reviewer(state)
        apply_approval_transition(bundle, reviewer)
        approval_job, _was_existing = _start_approval_job(careplan_id, bundle, reviewer)

    _care_plans[careplan_id] = {
        "id": careplan_id,
        "bundle": bundle,
        "status": initial_status,
        "patient_reference": state.get("patient_reference", ""),
        "patient_name": _extract_patient_name(bundle),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server_ids": {},
    }

    if output_dir:
        write_artifact(output_dir, "fhir-careplan-bundle.json", bundle)

    if initial_status == "draft":
        logger.info("Care plan %s stored as a draft; awaiting clinician approval", careplan_id)
        return {
            "careplan_id": careplan_id,
            "delivery_status": "draft",
        }

    publication = None
    resumed = _resume_existing_approval(approval_job) if approval_job and _was_existing else None
    if resumed is None:
        delivery = _post_bundle(bundle)
    else:
        delivery, publication = resumed
    _care_plans[careplan_id].update(delivery)
    if approval_job is not None and delivery.get("delivery_status") == "delivered" and publication is None:
        publication = _record_and_publish(approval_job, delivery)
        _care_plans[careplan_id]["automation_publication"] = publication
    elif publication is not None:
        _care_plans[careplan_id]["automation_publication"] = publication
    if output_dir and delivery.get("fhir_server_response"):
        write_artifact(output_dir, "fhir-server-response.json", delivery["fhir_server_response"])
    return {
        "careplan_id": careplan_id,
        **delivery,
        **({"automation_publication": publication} if publication is not None else {}),
    }


@mlflow.trace(name="fhir_server_writer.post_bundle")
def _post_bundle(bundle: dict) -> dict:
    """Post one transaction Bundle and return delivery metadata."""
    try:
        headers = {"Content-Type": "application/fhir+json", **_get_auth_headers()}
        r = requests.post(
            FHIR_SERVER_URL,
            json=bundle,
            headers=headers,
            timeout=30,
        )

        try:
            response_data = r.json() if r.content else {}
        except ValueError:
            if r.status_code in (200, 201):
                logger.warning("FHIR transaction succeeded without a JSON response; approval requires reconciliation")
                return {
                    "fhir_server_response": {},
                    "delivery_status": "stored_locally",
                    "server_status_code": r.status_code,
                    "server_ids": {},
                    "error": "FHIR transaction response was not valid JSON",
                }
            response_data = {}

        if r.status_code in (200, 201):
            logger.info("FHIR Bundle posted successfully (status %d)", r.status_code)
            if response_data.get("entry"):
                for i, entry in enumerate(response_data["entry"][:10]):
                    resp_info = entry.get("response", {})
                    logger.info(
                        "  entry[%d]: status=%s location=%s",
                        i, resp_info.get("status", "?"), resp_info.get("location", "?")[:80],
                    )
            elif response_data.get("issue"):
                for iss in response_data["issue"][:5]:
                    logger.warning(
                        "  OperationOutcome: %s — %s",
                        iss.get("severity", "?"), iss.get("diagnostics", "?")[:120],
                    )
            else:
                logger.info("  Response body: %s", json.dumps(response_data)[:500])
            server_ids = _parse_server_ids(bundle, response_data)
            return {
                "fhir_server_response": response_data,
                "delivery_status": "delivered",
                "server_status_code": r.status_code,
                "server_ids": server_ids,
            }
        elif r.status_code in (401, 403):
            logger.warning(
                "FHIR server auth failed (%d) — CarePlan saved locally but not delivered to server. "
                "Set FHIR_CLIENT_ID and FHIR_CLIENT_SECRET to enable authenticated writes.",
                r.status_code,
            )
            return {
                "fhir_server_response": response_data,
                "delivery_status": "stored_locally",
                "server_status_code": r.status_code,
                "server_ids": {},
            }
        else:
            logger.warning("FHIR server returned %d: %s", r.status_code, r.text[:200])
            return {
                "fhir_server_response": response_data,
                "delivery_status": "error",
                "server_status_code": r.status_code,
                "error": r.text[:500],
                "server_ids": {},
            }

    except requests.RequestException as e:
        logger.warning("FHIR server unavailable: %s — CarePlan saved locally only", e)
        return {
            "fhir_server_response": {},
            "delivery_status": "stored_locally",
            "server_ids": {},
        }


@mlflow.trace(name="fhir_server_writer.automation_revisions")
def _automation_revisions(bundle: dict) -> list[dict[str, str]]:
    revisions = []
    for entry in bundle.get("entry", []):
        task = entry.get("resource") or {}
        if task.get("resourceType") != "Task":
            continue
        automation_id = next(
            (item.get("value") for item in task.get("identifier", [])
             if item.get("system") == AUTOMATION_ID_SYSTEM),
            None,
        )
        revision = next(
            (item.get("value") for item in task.get("identifier", [])
             if item.get("system") == AUTOMATION_REVISION_SYSTEM),
            None,
        )
        if automation_id and revision:
            revisions.append({"automation_id": automation_id, "revision": revision})
    return sorted(revisions, key=lambda item: item["automation_id"])


@mlflow.trace(name="fhir_server_writer.start_approval_job")
def _start_approval_job(careplan_id: str, bundle: dict, reviewer: "ReviewerContext") -> tuple[dict, bool]:
    """Persist the approved bundle before any FHIR transaction is sent."""
    from acp_writer.services.automation_publisher import get_publisher

    return get_publisher().start_approval_with_status(
        careplan_id,
        _automation_revisions(bundle),
        reviewer,
        datetime.now(timezone.utc),
        bundle=bundle,
    )


@mlflow.trace(name="fhir_server_writer.resume_existing_approval")
def _resume_existing_approval(job: dict) -> tuple[dict, dict] | None:
    """Recover an approving job before re-sending its transaction bundle."""
    from acp_writer.services.automation_publisher import (
        get_publisher,
        is_terminal_publication,
    )

    publisher = get_publisher()
    if publisher.store.get_withdrawal(job["careplan_id"]) is not None:
        result = publisher.publish(job)
        delivery = {
            "delivery_status": "delivered" if result.get("server_ids") else "stored_locally",
            "fhir_server_response": result.get("transaction_response", {}),
            "server_ids": result.get("server_ids", {}),
        }
        return delivery, _publication_view(result)

    if job.get("state") == "approving":
        recovered, lookup_ok = publisher.recover_approval_if_written(job)
        if not lookup_ok:
            return (
                {
                    "delivery_status": "stored_locally",
                    "fhir_server_response": {},
                    "server_ids": {},
                    "error": "FHIR recovery lookup failed; transaction was not re-sent",
                },
                {"status": "approving", "attempts": job.get("publish_attempts", 0)},
            )
        if recovered is None:
            return None
        delivery = {
            "delivery_status": "delivered",
            "fhir_server_response": {},
            "server_ids": recovered.get("server_ids", {}),
        }
        return delivery, _publish_pending(recovered)

    if job.get("server_ids"):
        delivery = {
            "delivery_status": "delivered",
            "fhir_server_response": job.get("transaction_response", {}),
            "server_ids": job["server_ids"],
        }
        if job.get("state") in {"pending", "failed"} and job.get("publication_payload"):
            return delivery, _publish_pending(job)
        return delivery, _publication_view(job)
    if is_terminal_publication(job):
        delivery = {
            "delivery_status": "stored_locally",
            "fhir_server_response": job.get("transaction_response", {}),
            "server_ids": {},
        }
        return delivery, _publication_view(job)
    return None


@mlflow.trace(name="fhir_server_writer.record_and_publish")
def _record_and_publish(job: dict, delivery: dict) -> dict:
    """Record a successful transaction and attempt automation publication."""
    from acp_writer.services.automation_publisher import get_publisher

    publisher = get_publisher()
    response = delivery.get("fhir_server_response") or {}
    try:
        pending = publisher.record_write(job, response)
    except ValueError as exc:
        logger.warning(
            "Approval job %s awaits recovery after incomplete FHIR response: %s",
            job["job_id"], exc,
        )
        return {
            "status": "approving",
            "attempts": job.get("publish_attempts", 0),
            "lastError": str(exc),
        }
    return _publish_pending(pending)


@mlflow.trace(name="fhir_server_writer.publish_pending")
def _publish_pending(pending: dict) -> dict:
    from acp_writer.services.automation_publisher import get_publisher

    publisher = get_publisher()
    result = publisher.publish(pending)
    if result.get("state") == "pending":
        publisher.schedule_retries(result)
    return _publication_view(result)


def _publication_view(result: dict) -> dict:
    view = {
        "status": result.get("state", "pending"),
        "attempts": result.get("publish_attempts", 0),
    }
    if result.get("configuration_reason"):
        view["configurationReason"] = result["configuration_reason"]
    if result.get("last_attempt_at"):
        view["lastAttemptAt"] = result["last_attempt_at"]
    if result.get("last_error") and result.get("state") not in {
        "withdrawn", "unconfigured", "not-applicable",
    }:
        view["lastError"] = result["last_error"]
    return view


def _extract_patient_name(bundle: dict) -> str:
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Patient":
            for name in resource.get("name", []):
                if isinstance(name, dict):
                    text = name.get("text")
                    if text:
                        return text
                    parts = []
                    if name.get("given"):
                        parts.extend(name["given"])
                    if name.get("family"):
                        parts.append(name["family"])
                    if parts:
                        return " ".join(parts)
    return ""


def get_care_plan(careplan_id: str) -> dict | None:
    return _care_plans.get(careplan_id)


def list_care_plans(patient: str | None = None, status: str | None = None) -> list[dict]:
    results = list(_care_plans.values())
    if patient:
        results = [cp for cp in results if cp.get("patient_reference") == patient]
    if status:
        results = [cp for cp in results if cp.get("status") == status]
    return [{
        "id": cp["id"],
        "patient_name": cp.get("patient_name", ""),
        "patient_reference": cp.get("patient_reference", ""),
        "status": cp.get("status", ""),
        "generated_at": cp.get("generated_at"),
    } for cp in results]


def _state_reviewer(state: CarePlanComposerState) -> "ReviewerContext":
    """Reviewer for a writer-node approval — from state, else the config default.

    The deployed WriteFHIR path threads the approving clinician through
    ``state["reviewer"]`` (a ``ReviewerContext`` or its dict form); absent that
    we fall back to the configured demo reviewer.
    """
    from acp_writer.services.reviewer import ReviewerContext, default_reviewer

    reviewer = state.get("reviewer")
    if isinstance(reviewer, ReviewerContext):
        return reviewer
    if isinstance(reviewer, dict) and reviewer:
        return ReviewerContext(**reviewer)
    return default_reviewer()


def _find_careplan_resource(bundle: dict) -> dict | None:
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "CarePlan":
            return resource
    return None


@mlflow.trace(name="fhir_server_writer.set_careplan_identifier")
def _set_careplan_identifier(bundle: dict, careplan_id: str) -> None:
    """Set the stable business identifier and transaction upsert condition."""
    careplan_entry = next(
        (
            entry for entry in bundle.get("entry", [])
            if (entry.get("resource") or {}).get("resourceType") == "CarePlan"
        ),
        None,
    )
    if careplan_entry is None:
        return
    system = f"{ACP_CS_BASE}/careplan-id"
    resource = careplan_entry["resource"]
    identifiers = [
        item for item in resource.get("identifier", [])
        if item.get("system") != system
    ]
    identifiers.append({"system": system, "value": careplan_id})
    resource["identifier"] = identifiers
    careplan_entry.setdefault("request", {})["ifNoneExist"] = (
        f"identifier={system}|{careplan_id}"
    )


def approve_care_plan(
    careplan_id: str,
    reviewer: "ReviewerContext | None" = None,
) -> dict | None:
    """Approve a care plan: full approval transition + update on FHIR server.

    ``reviewer`` is a ``ReviewerContext`` (defaults to the configured reviewer).
    The transition itself — AIAST→CLINAST_AIRPT, CarePlan→active, verifier
    Humanagent on every AI-Provenance, conflicts→acknowledged — is delegated to
    ``apply_approval_transition`` so this path and the deployed WriteFHIR path
    stay identical (issue #169 F1).
    """
    from acp_writer.services.reviewer import default_reviewer

    cp = _care_plans.get(careplan_id)
    if not cp:
        return None

    if cp.get("status") == "active" and cp.get("delivery_status") == "delivered":
        return {"id": careplan_id, "status": "active", "delivery_status": "delivered"}

    bundle = cp.get("bundle", {})
    reviewer_context = reviewer or default_reviewer()
    apply_approval_transition(bundle, reviewer_context)
    cp["status"] = "active"
    job, was_existing = _start_approval_job(careplan_id, bundle, reviewer_context)
    resumed = _resume_existing_approval(job) if was_existing else None
    if resumed is None:
        delivery = _post_bundle(bundle)
        publication = None
    else:
        delivery, publication = resumed
    cp.update(delivery)
    if publication is None:
        publication = {"status": "approving", "attempts": 0}
    if delivery.get("delivery_status") == "delivered" and publication.get("status") == "approving":
        publication = _record_and_publish(job, delivery)
    cp["automation_publication"] = publication
    return {
        "id": careplan_id,
        "status": "active",
        "delivery_status": delivery["delivery_status"],
        "fhir_server_status": delivery.get("server_status_code"),
        "automation_publication": publication,
    }


def reject_care_plan(careplan_id: str, reason: str) -> dict | None:
    """Reject a care plan: status→entered-in-error, update on FHIR server."""
    cp = _care_plans.get(careplan_id)
    if not cp:
        return None

    from acp_writer.services.automation_publisher import get_publisher

    get_publisher().withdraw(careplan_id)

    cp["status"] = "entered-in-error"
    bundle = cp.get("bundle", {})

    careplan_resource = None
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "CarePlan":
            resource["status"] = "entered-in-error"
            notes = resource.get("note", [])
            notes.append({"text": f"Rejected: {reason}"})
            resource["note"] = notes
            careplan_resource = resource

    server_ref = _find_careplan_server_id(cp.get("server_ids", {}))
    if server_ref and careplan_resource:
        _update_on_server(server_ref, careplan_resource)
    elif careplan_resource:
        logger.info("No server ID for CarePlan — server-side update skipped (local only)")

    return {"id": careplan_id, "status": "entered-in-error", "reason": reason}
