"""Crash, retry, and reference-rewrite checks for FHIR automation publication."""

from base64 import b64encode
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from acp_writer.services.ai_transparency import (
    ACP_CS_BASE,
    ACP_EXT_BASE,
    AUTOMATION_ID_SYSTEM,
    AUTOMATION_REVISION_SYSTEM,
    AUTOMATION_TEMPLATE_EXT,
    AUTOMATION_INPUT_SYSTEM,
)
from acp_writer.services.automation_publisher import AutomationPublisher
from acp_writer.services.job_store import JobStore


def _bundle():
    task_urn = "urn:uuid:task-1"
    doc_urn = "urn:uuid:doc-1"
    careplan_urn = "urn:uuid:careplan-1"
    patient_urn = "urn:uuid:patient-1"
    xml = "<bpmn:definitions xmlns:bpmn='http://www.omg.org/spec/BPMN/20100524/MODEL'/>"
    task = {
        "resourceType": "Task",
        "id": "task-1",
        "identifier": [
            {"system": AUTOMATION_ID_SYSTEM, "value": "automation-1"},
            {"system": AUTOMATION_REVISION_SYSTEM, "value": "0123456789ab"},
        ],
        "extension": [{
            "url": AUTOMATION_TEMPLATE_EXT,
            "extension": [
                {"url": "templateId", "valueString": "template-1"},
                {"url": "version", "valueString": "abcdef012345"},
                {"url": "sourceCpg", "valueString": "cpg-1"},
            ],
        }],
        "status": "requested",
        "intent": "plan",
        "for": {"reference": patient_urn},
        "focus": {"reference": careplan_urn},
        "basedOn": [{"reference": careplan_urn}],
        "input": [{
            "type": {"coding": [{"system": AUTOMATION_INPUT_SYSTEM, "code": "process-definition"}]},
            "valueReference": {"reference": doc_urn},
        }],
    }
    document = {
        "resourceType": "DocumentReference",
        "id": "doc-1",
        "status": "current",
        "content": [{"attachment": {
            "contentType": "application/xml",
            "data": b64encode(xml.encode()).decode(),
        }}],
        "subject": {"reference": patient_urn},
        "context": {"related": [{"reference": careplan_urn}, {"reference": task_urn}]},
    }
    careplan = {
        "resourceType": "CarePlan",
        "id": "careplan-1",
        "identifier": [{"system": f"{ACP_CS_BASE}/careplan-id", "value": "cp-1"}],
        "subject": {"reference": patient_urn},
        "activity": [{
            "id": "activity-1",
            "extension": [{
                "url": f"{ACP_EXT_BASE}/activity-automation",
                "extension": [
                    {"url": "definition", "valueReference": {"reference": doc_urn}},
                    {"url": "instance", "valueReference": {"reference": task_urn}},
                    {"url": "status", "valueCode": "enabled"},
                ],
            }],
        }],
    }
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"fullUrl": patient_urn, "resource": {"resourceType": "Patient", "id": "patient-1"}},
            {"fullUrl": careplan_urn, "resource": careplan},
            {"fullUrl": doc_urn, "resource": document},
            {"fullUrl": task_urn, "resource": task},
        ],
    }


def _response(bundle):
    locations = {
        "Patient": "Patient/patient-server",
        "CarePlan": "CarePlan/careplan-server",
        "DocumentReference": "DocumentReference/doc-server",
        "Task": "Task/task-server",
    }
    return {"entry": [
        {"response": {"location": locations[entry["resource"]["resourceType"]] + "/_history/1"}}
        for entry in bundle["entry"]
    ]}


class _Client:
    def __init__(self, *, get_result=None, post_status=201):
        self.get_result = get_result
        self.post_status = post_status
        self.posts = []
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: self.get_result or {"entry": []},
            raise_for_status=lambda: None,
        )

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return SimpleNamespace(
            status_code=self.post_status,
            content=b"{}",
            text="service unavailable",
            json=lambda: {"status": "accepted"},
        )


def _publisher(tmp_path):
    return AutomationPublisher(
        JobStore(directory=tmp_path),
        fhir_url="http://fhir/fhir/R4",
        automation_url="http://automation",
    )


def _job(publisher):
    return publisher.start_approval(
        "cp-1",
        [{"automation_id": "automation-1", "revision": "0123456789ab"}],
        {"display": "Dr Example"},
        datetime(2026, 10, 9, tzinfo=timezone.utc),
        bundle=_bundle(),
    )


def test_start_approval_persists_before_any_external_write(tmp_path):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    assert job["state"] == "approving"
    assert publisher.store.get("cp-1", job["job_id"])["bundle"] == _bundle()


def test_duplicate_approval_uses_same_job_without_creating_another(tmp_path):
    publisher = _publisher(tmp_path)
    first = _job(publisher)
    second = _job(publisher)
    assert first["job_id"] == second["job_id"]
    assert len(publisher.store.list("cp-1")) == 1


def test_record_write_rewrites_task_and_document_references(tmp_path):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    pending = publisher.record_write(job, _response(job["bundle"]))
    assert pending["state"] == "pending"
    item = pending["publication_payload"]["automations"][0]
    assert item["task"]["id"] == "task-server"
    assert item["task"]["for"]["reference"] == "Patient/patient-server"
    assert item["task"]["basedOn"][0]["reference"] == "CarePlan/careplan-server"
    assert item["task"]["focus"]["reference"] == "CarePlan/careplan-server"
    assert item["task"]["input"][0]["valueReference"]["reference"] == "DocumentReference/doc-server"
    assert item["document_reference"]["id"] == "doc-server"
    assert item["document_reference"]["context"]["related"] == [
        {"reference": "CarePlan/careplan-server"},
        {"reference": "Task/task-server"},
    ]
    assert item["activity_ids"] == ["activity-1"]


def test_successful_publish_uses_job_id_as_idempotency_key(tmp_path):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    pending = publisher.record_write(job, _response(job["bundle"]))
    client = _Client()
    result = publisher.publish(pending, client)
    assert result["state"] == "published"
    assert client.posts[0][1]["headers"] == {"Idempotency-Key": job["job_id"]}
    assert client.posts[0][1]["timeout"] == 5


def test_tombstone_prevents_publication_send(tmp_path):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    pending = publisher.record_write(job, _response(job["bundle"]))
    client = _Client()
    publisher.withdraw("cp-1", client)
    client.posts.clear()
    result = publisher.publish(pending, client)
    assert result["state"] == "withdrawn"
    assert client.posts == []


def test_crash_after_fhir_write_redrive_recovers_without_duplicate_transaction(tmp_path):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    recovered_careplan = deepcopy(job["bundle"]["entry"][1]["resource"])
    recovered_careplan["id"] = "careplan-server"
    recovered_careplan["subject"]["reference"] = "Patient/patient-server"
    for activity in recovered_careplan["activity"]:
        for extension in activity.get("extension", []):
            for item in extension.get("extension", []):
                reference = item.get("valueReference", {}).get("reference")
                if reference == "urn:uuid:doc-1":
                    item["valueReference"]["reference"] = "DocumentReference/doc-server"
                elif reference == "urn:uuid:task-1":
                    item["valueReference"]["reference"] = "Task/task-server"
    fhir = _Client(get_result={"entry": [{"resource": recovered_careplan}]})
    automation = _Client()
    publisher.reconcile_on_start(fhir, automation)
    current = publisher.store.get("cp-1", job["job_id"])
    assert current["state"] == "published"
    assert fhir.gets[0][1]["params"]["identifier"].endswith("|cp-1")
    assert fhir.posts == [], "the transaction already exists on FHIR and must not be posted again"
    assert len(automation.posts) == 1
    payload = current["publication_payload"]["automations"][0]
    assert payload["task"]["focus"]["reference"] == "CarePlan/careplan-server"

    def references_with_urn(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from references_with_urn(child)
        elif isinstance(value, list):
            for child in value:
                yield from references_with_urn(child)
        elif isinstance(value, str) and value.startswith("urn:uuid:"):
            yield value

    assert list(references_with_urn(payload["task"])) == []
    assert list(references_with_urn(payload["document_reference"])) == []


def test_approval_recovery_reuses_writer_auth_headers(tmp_path, monkeypatch):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    recovered_careplan = deepcopy(job["bundle"]["entry"][1]["resource"])
    recovered_careplan["id"] = "careplan-server"
    recovered_careplan["subject"]["reference"] = "Patient/patient-server"
    fhir = _Client(get_result={"entry": [{"resource": recovered_careplan}]})
    monkeypatch.setattr(
        "acp_writer.nodes.fhir_server_writer._get_auth_headers",
        lambda: {"Authorization": "Bearer test-token"},
    )

    recovered, lookup_ok = publisher.recover_approval_if_written(job, fhir)

    assert lookup_ok is True
    assert recovered is not None
    assert fhir.gets[0][1]["headers"] == {"Authorization": "Bearer test-token"}


def test_approving_job_reconcile_redrives_when_fhir_has_no_matching_careplan(tmp_path):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    transaction_response = _response(job["bundle"])

    class FhirClient(_Client):
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return SimpleNamespace(
                status_code=201,
                json=lambda: transaction_response,
                raise_for_status=lambda: None,
            )

    fhir = FhirClient(get_result={"entry": []})
    automation = _Client()
    publisher.reconcile_on_start(fhir, automation)
    current = publisher.store.get("cp-1", job["job_id"])
    assert current["state"] == "published"
    assert len(fhir.posts) == 1
    assert len(automation.posts) == 1


def test_pending_job_is_resumed_on_startup(tmp_path, monkeypatch):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    pending = publisher.record_write(job, _response(job["bundle"]))
    publisher.store.put(pending)
    scheduled = []
    monkeypatch.setattr(publisher, "schedule_retries", lambda current, client: scheduled.append(current))
    client = _Client(post_status=503)
    publisher.reconcile_on_start(_Client(), client)
    assert len(client.posts) == 1
    assert scheduled


def test_retry_worker_uses_backoff_and_marks_exhausted_job_failed(tmp_path, monkeypatch):
    publisher = _publisher(tmp_path)
    job = _job(publisher)
    pending = publisher.record_write(job, _response(job["bundle"]))
    pending["publish_attempts"] = 1
    publisher.store.put(pending)
    delays = []
    monkeypatch.setattr("acp_writer.services.automation_publisher.time.sleep", delays.append)
    client = _Client(post_status=503)
    publisher._retry_worker("cp-1", job["job_id"], client)
    current = publisher.store.get("cp-1", job["job_id"])
    assert delays == [2, 8, 30]
    assert current["publish_attempts"] == 4
    assert current["state"] == "failed"
