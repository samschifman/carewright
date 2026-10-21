"""Coordinate durable FHIR approval and automation publication jobs."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import logging
import os
import threading
import time
from typing import Any

import mlflow
import requests

from cpg_contracts.automation.instances import PublicationPayload, PublishedAutomation, TemplateRef
from acp_writer.services.ai_transparency import (
    ACP_CS_BASE,
    AUTOMATION_ID_SYSTEM,
    AUTOMATION_REVISION_SYSTEM,
    AUTOMATION_TEMPLATE_EXT,
)
from acp_writer.services.job_store import JobStore

logger = logging.getLogger(__name__)

FHIR_SERVER_URL = os.environ.get("FHIR_SERVER_URL", "http://localhost:8103/fhir/R4")
AUTOMATION_SERVICE_URL = os.environ.get("AUTOMATION_SERVICE_URL", "")
AUTOMATION_SERVICE_TOKEN = os.environ.get("AUTOMATION_SERVICE_TOKEN", "")
TERMINAL_PUBLICATION_STATES = frozenset({
    "published", "withdrawn", "unconfigured", "not-applicable",
})
_store = JobStore()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_optional(value: str | None) -> str | None:
    normalized = value.strip() if isinstance(value, str) else ""
    return normalized or None


def is_terminal_publication(job: dict) -> bool:
    return job.get("state") in TERMINAL_PUBLICATION_STATES


def publication_status_view(job: dict) -> dict:
    view = {
        "state": job.get("state"),
        "publish_attempts": job.get("publish_attempts", 0),
    }
    if job.get("configuration_reason"):
        view["configuration_reason"] = job["configuration_reason"]
    return view


@mlflow.trace(name="automation.publication.entry_index")
def _entry_index(bundle: dict) -> dict[str, dict]:
    return {
        entry.get("fullUrl", ""): entry.get("resource", {})
        for entry in bundle.get("entry", [])
        if entry.get("fullUrl") and entry.get("resource")
    }


@mlflow.trace(name="automation.publication.resource_ids")
def _resource_ids(bundle: dict, transaction_response: dict) -> dict[str, str]:
    result = {}
    for source, response in zip(bundle.get("entry", []), transaction_response.get("entry", [])):
        full_url = source.get("fullUrl")
        location = (response.get("response") or {}).get("location", "")
        parts = location.split("/")
        if full_url and len(parts) >= 2 and parts[0] and parts[1]:
            result[full_url] = f"{parts[0]}/{parts[1]}"
    return result


def _rewrite_references(value: Any, references: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite_references(item, references) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_references(item, references) for item in value]
    if isinstance(value, str):
        return references.get(value, value)
    return value


def _identifier(resource: dict, system: str) -> str | None:
    return next(
        (item.get("value") for item in resource.get("identifier", []) if item.get("system") == system),
        None,
    )


@mlflow.trace(name="automation.publication.template_refs")
def _template_refs(task: dict) -> list[TemplateRef]:
    refs = []
    for block in task.get("extension", []):
        if block.get("url") != AUTOMATION_TEMPLATE_EXT:
            continue
        fields = {item.get("url"): item for item in block.get("extension", [])}
        template_id = fields.get("templateId", {}).get("valueString")
        version = fields.get("version", {}).get("valueString")
        if template_id and version:
            source_cpg = fields.get("sourceCpg", {}).get("valueString")
            refs.append(TemplateRef(
                template_id=template_id,
                version=version,
                source_cpg=source_cpg,
            ))
    return refs


@mlflow.trace(name="automation.publication.build_payload")
def _build_payload(job: dict, references: dict[str, str]) -> dict:
    bundle = job["bundle"]
    entries = _entry_index(bundle)
    careplan_entry = next(
        (entry for entry in bundle.get("entry", [])
         if (entry.get("resource") or {}).get("resourceType") == "CarePlan"),
        None,
    )
    patient_entry = next(
        (entry for entry in bundle.get("entry", [])
         if (entry.get("resource") or {}).get("resourceType") == "Patient"),
        None,
    )
    if careplan_entry is None or patient_entry is None:
        raise ValueError("approved transaction must contain a CarePlan and Patient")
    careplan_urn = careplan_entry["fullUrl"]
    patient_urn = patient_entry["fullUrl"]
    careplan_server_id = references.get(careplan_urn)
    patient_server_id = references.get(patient_urn)
    if not careplan_server_id or not patient_server_id:
        raise ValueError("FHIR transaction response is missing CarePlan or Patient server ids")

    careplan = careplan_entry["resource"]
    activity_ids_by_task: dict[str, list[str]] = {}
    for activity in careplan.get("activity", []):
        activity_id = activity.get("id")
        for extension in activity.get("extension", []):
            if not extension.get("url", "").endswith("/activity-automation"):
                continue
            task_ref = next(
                (item.get("valueReference", {}).get("reference")
                 for item in extension.get("extension", []) if item.get("url") == "instance"),
                None,
            )
            if task_ref and activity_id:
                activity_ids_by_task.setdefault(task_ref, []).append(activity_id)

    automations = []
    for task_entry in bundle.get("entry", []):
        task = task_entry.get("resource") or {}
        if task.get("resourceType") != "Task":
            continue
        automation_id = _identifier(task, AUTOMATION_ID_SYSTEM)
        revision = _identifier(task, AUTOMATION_REVISION_SYSTEM)
        if not automation_id or not revision:
            continue
        process_input = next(
            (
                item for item in task.get("input", [])
                if any(coding.get("code") == "process-definition"
                       for coding in (item.get("type") or {}).get("coding", []))
            ),
            None,
        )
        if process_input is None:
            raise ValueError(f"automation Task {automation_id} has no process-definition input")
        docref_urn = process_input.get("valueReference", {}).get("reference")
        document = entries.get(docref_urn)
        if not document or document.get("resourceType") != "DocumentReference":
            raise ValueError(f"automation Task {automation_id} references a missing DocumentReference")
        activity_ids = activity_ids_by_task.get(task_entry["fullUrl"], [])
        if not activity_ids:
            raise ValueError(f"automation Task {automation_id} is not linked from a CarePlan activity")
        task_copy = _rewrite_references(deepcopy(task), references)
        document_copy = _rewrite_references(deepcopy(document), references)
        task_copy["id"] = references.get(task_entry["fullUrl"], task_copy.get("id", "")).split("/", 1)[-1]
        document_copy["id"] = references.get(docref_urn, document_copy.get("id", "")).split("/", 1)[-1]
        automations.append(PublishedAutomation(
            automation_id=automation_id,
            activity_ids=sorted(set(activity_ids)),
            revision=revision,
            template_refs=_template_refs(task),
            task=task_copy,
            document_reference=document_copy,
        ).model_dump(mode="json"))

    reviewer = job.get("reviewer") or "Unknown reviewer"
    if isinstance(reviewer, dict):
        reviewer = reviewer.get("display") or reviewer.get("reference") or "Unknown reviewer"
    payload = PublicationPayload(
        job_id=job["job_id"],
        careplan_id=job["careplan_id"],
        careplan_server_id=careplan_server_id,
        patient_server_id=patient_server_id,
        approved_at=job["approved_at"],
        reviewer=str(reviewer),
        automations=automations,
    )
    return payload.model_dump(mode="json")


class AutomationPublisher:
    """Persist approval before FHIR write and publish with idempotent retries."""

    def __init__(
        self,
        store: JobStore | None = None,
        *,
        fhir_url: str | None = None,
        automation_url: str | None = None,
        automation_token: str | None = None,
    ) -> None:
        self.store = store or _store
        self.fhir_url = (fhir_url or FHIR_SERVER_URL).rstrip("/")
        url_value = AUTOMATION_SERVICE_URL if automation_url is None else automation_url
        token_value = AUTOMATION_SERVICE_TOKEN if automation_token is None else automation_token
        normalized_url = _normalized_optional(url_value)
        self.automation_url = normalized_url.rstrip("/") if normalized_url else None
        self.automation_token = _normalized_optional(token_value)

    def _post_write_state(self, job: dict) -> tuple[str, str | None]:
        if not job.get("revisions"):
            return "not-applicable", None
        if not self.automation_url:
            return "unconfigured", "missing-url"
        if not self.automation_token:
            return "unconfigured", "missing-token"
        return "pending", None

    def _mark_withdrawn(self, job: dict) -> dict:
        current = self.store.get(job["careplan_id"], job["job_id"]) or job
        if current.get("state") == "withdrawn":
            return current
        updated = {**current, "state": "withdrawn", "updated_at": _now()}
        updated.pop("configuration_reason", None)
        self.store.put(updated)
        return updated

    def _mark_post_write_state(self, job: dict) -> dict:
        state, reason = self._post_write_state(job)
        updated = {**job, "state": state, "updated_at": _now()}
        updated.pop("last_error", None)
        if reason:
            updated["configuration_reason"] = reason
        else:
            updated.pop("configuration_reason", None)
        self.store.put(updated)
        return updated

    @mlflow.trace(name="automation.publication.start_approval")
    def start_approval(
        self,
        careplan_id: str,
        revisions: list[dict[str, str]],
        reviewer: Any,
        approved_at: str | datetime,
        *,
        bundle: dict,
    ) -> dict:
        job, _was_existing = self.start_approval_with_status(
            careplan_id, revisions, reviewer, approved_at, bundle=bundle,
        )
        return job

    def start_approval_with_status(
        self,
        careplan_id: str,
        revisions: list[dict[str, str]],
        reviewer: Any,
        approved_at: str | datetime,
        *,
        bundle: dict,
    ) -> tuple[dict, bool]:
        """Persist the approval and report whether this call reused a prior job."""
        revision_key = sorted(
            (item.get("automation_id", ""), item.get("revision", ""))
            for item in revisions
        )
        job_id = hashlib.sha256(
            f"{careplan_id}:{revision_key}".encode("utf-8")
        ).hexdigest()[:32]
        existing = self.store.get(careplan_id, job_id)
        if existing is not None:
            return existing, True
        approved = approved_at.isoformat() if isinstance(approved_at, datetime) else approved_at
        reviewer_value = reviewer.model_dump(mode="json") if hasattr(reviewer, "model_dump") else reviewer
        job = {
            "job_id": job_id,
            "careplan_id": careplan_id,
            "revisions": revision_key,
            "reviewer": reviewer_value,
            "approved_at": approved,
            "state": "approving",
            "attempts": 0,
            "publish_attempts": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "bundle": deepcopy(bundle),
        }
        self.store.put(job)
        return job, False

    @mlflow.trace(name="automation.publication.record_write")
    def record_write(self, job: dict, transaction_response: dict) -> dict:
        """Persist server-id mappings and a reference-rewritten publication."""
        references = _resource_ids(job["bundle"], transaction_response)
        payload = _build_payload(job, references)
        updated = {
            **job,
            "server_ids": references,
            "publication_payload": payload,
            "attempts": job.get("attempts", 0) + 1,
            "transaction_response": transaction_response,
            "updated_at": _now(),
        }
        return self._mark_post_write_state(updated)

    @mlflow.trace(name="automation.publication.record_recovered_write")
    def record_recovered_write(self, job: dict, careplan_resource: dict) -> dict:
        """Rebuild transaction references from an existing CarePlan search hit."""
        bundle = job["bundle"]
        original_cp_entry = next(
            entry for entry in bundle.get("entry", [])
            if (entry.get("resource") or {}).get("resourceType") == "CarePlan"
        )
        original_patient_entry = next(
            entry for entry in bundle.get("entry", [])
            if (entry.get("resource") or {}).get("resourceType") == "Patient"
        )
        references = {
            original_cp_entry["fullUrl"]: f"CarePlan/{careplan_resource['id']}",
            original_patient_entry["fullUrl"]: careplan_resource.get("subject", {}).get("reference", ""),
        }
        old_activities = original_cp_entry["resource"].get("activity", [])
        new_activities = careplan_resource.get("activity", [])
        for old, current in zip(old_activities, new_activities):
            old_exts = {
                item.get("url"): item for item in old.get("extension", [])
                if item.get("url", "").endswith("/activity-automation")
            }
            current_exts = {
                item.get("url"): item for item in current.get("extension", [])
                if item.get("url", "").endswith("/activity-automation")
            }
            for name in ("definition", "instance"):
                old_ref = next((
                    item.get("valueReference", {}).get("reference")
                    for extension in old_exts.values()
                    for item in extension.get("extension", []) if item.get("url") == name
                ), None)
                new_ref = next((
                    item.get("valueReference", {}).get("reference")
                    for extension in current_exts.values()
                    for item in extension.get("extension", []) if item.get("url") == name
                ), None)
                if old_ref and new_ref:
                    references[old_ref] = new_ref
        payload = _build_payload(job, references)
        updated = {
            **job,
            "server_ids": references,
            "publication_payload": payload,
            "attempts": job.get("attempts", 0) + 1,
            "updated_at": _now(),
        }
        return self._mark_post_write_state(updated)

    def recover_approval_if_written(
        self,
        job: dict,
        fhir_client: Any = requests,
    ) -> tuple[dict | None, bool]:
        """Search before replaying an interrupted approval transaction.

        The boolean is false when the lookup could not be trusted; callers must
        leave the job pending recovery rather than risk a duplicate transaction.
        """
        if job.get("state") != "approving":
            return None, True
        from acp_writer.nodes.fhir_server_writer import _get_auth_headers

        identifier = f"{ACP_CS_BASE}/careplan-id|{job['careplan_id']}"
        try:
            response = fhir_client.get(
                f"{self.fhir_url}/CarePlan",
                params={"identifier": identifier},
                headers=_get_auth_headers(),
                timeout=5,
            )
            response.raise_for_status()
            entries = response.json().get("entry", [])
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Could not search for approval job %s: %s", job["job_id"], exc)
            return None, False
        if not entries:
            return None, True
        careplan = entries[0].get("resource", {})
        if not careplan.get("id"):
            logger.warning("FHIR search returned a CarePlan without an id for job %s", job["job_id"])
            return None, False
        try:
            return self.record_recovered_write(job, careplan), True
        except (KeyError, StopIteration, ValueError) as exc:
            logger.warning("Could not reconcile approval job %s: %s", job["job_id"], exc)
            return None, False

    @mlflow.trace(name="automation.publication.publish")
    def publish(self, job: dict, client: Any = requests) -> dict:
        """Make one idempotent publication attempt, honoring withdrawal tombstones."""
        if self.store.get_withdrawal(job["careplan_id"]) is not None:
            return self._mark_withdrawn(job)
        if is_terminal_publication(job):
            return job
        if job.get("state") not in {"pending", "failed"}:
            return job
        state, reason = self._post_write_state(job)
        if state != "pending":
            updated = {**job, "state": state, "updated_at": _now()}
            updated.pop("last_error", None)
            if reason:
                updated["configuration_reason"] = reason
            else:
                updated.pop("configuration_reason", None)
            self.store.put(updated)
            return updated
        payload = job.get("publication_payload")
        if not payload:
            return {**job, "state": "failed", "last_error": "publication payload missing"}
        current = {
            **job,
            "publish_attempts": job.get("publish_attempts", 0) + 1,
            "last_attempt_at": _now(),
            "updated_at": _now(),
        }
        self.store.put(current)
        try:
            response = client.post(
                f"{self.automation_url}/api/v1/automations",
                json=payload,
                headers={"Idempotency-Key": job["job_id"]},
                timeout=5,
            )
            if response.status_code in {200, 201}:
                try:
                    response_body = response.json() if response.content else {}
                except ValueError:
                    response_body = {"status": "accepted"}
                current.update({
                    "state": "published",
                    "publication_response": response_body,
                    "updated_at": _now(),
                })
            else:
                current.update({
                    "state": "failed" if current["publish_attempts"] >= 4 else "pending",
                    "last_error": f"automation service returned {response.status_code}: {response.text[:500]}",
                    "updated_at": _now(),
                })
        except requests.RequestException as exc:
            current.update({
                "state": "failed" if current["publish_attempts"] >= 4 else "pending",
                "last_error": str(exc)[:500],
                "updated_at": _now(),
            })
        self.store.put(current)
        return current

    @mlflow.trace(name="automation.publication.schedule_retries")
    def schedule_retries(self, job: dict, client: Any = requests) -> None:
        """Schedule up to three delayed attempts after the inline publication."""
        if self.store.get_withdrawal(job["careplan_id"]) is not None:
            self._mark_withdrawn(job)
            return
        if job.get("state") != "pending" or is_terminal_publication(job):
            return
        if not self.automation_url or not self.automation_token:
            self._mark_post_write_state(job)
            return
        thread = threading.Thread(
            target=self._retry_worker,
            args=(job["careplan_id"], job["job_id"], client),
            daemon=True,
            name=f"automation-publish-{job['job_id'][:8]}",
        )
        thread.start()

    @mlflow.trace(name="automation.publication.retry_worker")
    def _retry_worker(self, careplan_id: str, job_id: str, client: Any) -> None:
        for delay in (2, 8, 30):
            time.sleep(delay)
            job = self.store.get(careplan_id, job_id)
            if job is None:
                return
            if self.store.get_withdrawal(careplan_id) is not None:
                self._mark_withdrawn(job)
                return
            if is_terminal_publication(job):
                return
            updated = self.publish(job, client)
            if is_terminal_publication(updated):
                return
        job = self.store.get(careplan_id, job_id)
        if job and job.get("state") in {"pending", "failed"} and not is_terminal_publication(job):
            self.store.put({**job, "state": "failed", "updated_at": _now()})

    @mlflow.trace(name="automation.publication.withdraw")
    def withdraw(self, careplan_id: str, client: Any = requests) -> dict:
        """Write a durable tombstone before asking the receiver to withdraw."""
        marker = self.store.get_withdrawal(careplan_id)
        if marker is None:
            marker = {"careplan_id": careplan_id, "status": "withdrawn", "withdrawn_at": _now()}
        marker = {**marker, "careplan_id": careplan_id, "status": "withdrawn"}
        self.store.put_withdrawal(careplan_id, marker)
        for job in self.store.list(careplan_id):
            self._mark_withdrawn(job)
        if not self.automation_url or not self.automation_token:
            return {**marker, "remote_status": "unconfigured"}
        try:
            response = client.post(
                f"{self.automation_url}/api/v1/automations/{careplan_id}/withdraw",
                timeout=5,
            )
            marker = {**marker, "receiver_status": response.status_code}
        except requests.RequestException as exc:
            marker = {**marker, "receiver_error": str(exc)[:500]}
        return marker

    @mlflow.trace(name="automation.publication.retry")
    def retry(self, careplan_id: str, client: Any = requests) -> dict | None:
        jobs = self.store.list(careplan_id)
        if not jobs:
            return None
        if self.store.get_withdrawal(careplan_id) is not None:
            jobs = [self._mark_withdrawn(job) for job in jobs]
        retryable = [job for job in jobs if job.get("state") in {"pending", "failed"}]
        if retryable:
            job = max(retryable, key=lambda item: item.get("updated_at", ""))
        else:
            terminal = [job for job in jobs if is_terminal_publication(job)]
            job = max(terminal or jobs, key=lambda item: item.get("updated_at", ""))
            return job
        result = self.publish(job, client)
        if result.get("state") == "pending":
            self.schedule_retries(result, client)
        return result

    @mlflow.trace(name="automation.publication.reconcile")
    def reconcile_on_start(self, fhir_client: Any = requests, automation_client: Any = requests) -> None:
        """Resume interrupted FHIR writes and queued automation publication jobs."""
        for job in self.store.list():
            if self.store.get_withdrawal(job["careplan_id"]):
                self._mark_withdrawn(job)
                continue
            if is_terminal_publication(job):
                continue
            if job.get("state") == "approving":
                recovered, lookup_ok = self.recover_approval_if_written(job, fhir_client)
                if not lookup_ok:
                    continue
                if recovered is not None:
                    job = recovered
                else:
                    from acp_writer.nodes.fhir_server_writer import _get_auth_headers

                    try:
                        write_response = fhir_client.post(
                            self.fhir_url,
                            json=job["bundle"],
                            headers={"Content-Type": "application/fhir+json", **_get_auth_headers()},
                            timeout=30,
                        )
                        if write_response.status_code not in {200, 201}:
                            continue
                        job = self.record_write(job, write_response.json())
                    except (requests.RequestException, ValueError) as exc:
                        logger.warning("Could not resume approval job %s: %s", job["job_id"], exc)
                        continue
            if (
                job.get("state") in {"pending", "failed"}
                and job.get("publish_attempts", 0) < 4
            ):
                result = self.publish(job, automation_client)
                if result.get("state") == "pending":
                    self.schedule_retries(result, automation_client)


_publisher = AutomationPublisher()


def get_publisher() -> AutomationPublisher:
    """Return the process-wide publisher used by approval endpoints."""
    return _publisher
