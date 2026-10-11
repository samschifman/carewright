"""Offline API and durable-directory tests for automation publication."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from automation_service import app as app_module
from automation_service import store as store_module
from automation_service.store import AutomationStore


REPO_ROOT = Path(__file__).parents[2]
BPMN = (REPO_ROOT / "shared/tests/fixtures/automation/home-bp-monitoring.bpmn").read_text()


def test_health_endpoint_is_not_traced():
    assert not getattr(app_module.health, "__mlflow_traced__", False)


def _payload(
    *, job_id="job-1", xml=BPMN, careplan_id="careplan-1",
    automation_id="automation-1", revision="a" * 12,
    approved_at="2026-10-09T00:00:00+00:00", replaces_careplan_id=None,
):
    from cpg_contracts.automation.instances import PublicationPayload

    attachment = base64.b64encode(xml.encode()).decode()
    return PublicationPayload.model_validate({
        "job_id": job_id,
        "careplan_id": careplan_id,
        "careplan_server_id": f"server-{careplan_id}",
        "patient_server_id": "patient-1",
        "approved_at": approved_at,
        "reviewer": "Practitioner/reviewer-1",
        "replaces_careplan_id": replaces_careplan_id,
        "automations": [{
            "automation_id": automation_id,
            "activity_ids": ["activity-1"],
            "revision": revision,
            "template_refs": [{"template_id": "home-bp-monitoring", "version": "b" * 12}],
            "task": {
                "resourceType": "Task",
                "status": "requested",
                "intent": "plan",
                "for": {"reference": "Patient/patient-1"},
                "input": [{
                    "type": {"coding": [{"code": "process-definition"}]},
                    "valueReference": {"reference": "urn:uuid:docref-1"},
                }],
            },
            "document_reference": {
                "resourceType": "DocumentReference",
                "status": "current",
                "content": [{
                    "attachment": {
                        "contentType": "application/xml",
                        "data": attachment,
                    },
                }],
            },
        }],
    })


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(store_module, "get_phi_store", lambda: None)
    store = AutomationStore(directory=tmp_path)
    monkeypatch.setattr(app_module, "_store", store)
    return TestClient(app_module.app), store


def test_publish_replay_conflict_get_and_withdraw(monkeypatch, tmp_path):
    client, _store = _client(monkeypatch, tmp_path)
    payload = _payload()
    headers = {"Idempotency-Key": payload.job_id}

    first = client.post("/api/v1/automations", json=payload.model_dump(mode="json"), headers=headers)
    replay = client.post("/api/v1/automations", json=payload.model_dump(mode="json"), headers=headers)
    changed = payload.model_copy(update={"reviewer": "Practitioner/other"})
    conflict = client.post("/api/v1/automations", json=changed.model_dump(mode="json"), headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert conflict.status_code == 409
    listed = client.get("/api/v1/automations", params={"careplan_id": "careplan-1"})
    fetched = client.get("/api/v1/automations/careplan-1/automation-1")
    assert len(listed.json()) == 1
    assert fetched.json()["bpmn_xml"] == BPMN

    withdrawn = client.post("/api/v1/automations/careplan-1/withdraw")
    repeated = client.post("/api/v1/automations/careplan-1/withdraw")
    after_withdrawal = client.post(
        "/api/v1/automations",
        json=payload.model_dump(mode="json"),
        headers=headers,
    )
    assert withdrawn.status_code == 201
    assert repeated.status_code == 200
    assert after_withdrawal.status_code == 409
    assert after_withdrawal.json()["error"] == "careplan-withdrawn"
    assert client.get("/api/v1/automations", params={"careplan_id": "careplan-1"}).json()[0]["status"] == "withdrawn"


def test_older_approved_at_is_rejected_as_stale(monkeypatch, tmp_path):
    client, _store = _client(monkeypatch, tmp_path)
    newer = _payload(job_id="job-newer", approved_at="2026-10-10T00:00:00+00:00")
    older = _payload(job_id="job-older", approved_at="2026-10-09T00:00:00+00:00")

    assert client.post(
        "/api/v1/automations", json=newer.model_dump(mode="json"),
        headers={"Idempotency-Key": newer.job_id},
    ).status_code == 201
    stale = client.post(
        "/api/v1/automations", json=older.model_dump(mode="json"),
        headers={"Idempotency-Key": older.job_id},
    )

    assert stale.status_code == 409
    assert stale.json()["error"] == "stale-approval"


def test_absent_automations_are_marked_superseded(monkeypatch, tmp_path):
    client, _store = _client(monkeypatch, tmp_path)
    first = _payload()
    second = _payload(
        job_id="job-2",
        automation_id="automation-2",
        revision="b" * 12,
        approved_at="2026-10-10T00:00:00+00:00",
    )
    for payload in (first, second):
        assert client.post(
            "/api/v1/automations", json=payload.model_dump(mode="json"),
            headers={"Idempotency-Key": payload.job_id},
        ).status_code == 201

    previous = client.get("/api/v1/automations/careplan-1/automation-1")
    current = client.get("/api/v1/automations/careplan-1/automation-2")
    assert previous.json()["status"] == "superseded"
    assert current.json()["status"] == "active"


def test_replaces_careplan_marks_old_records_superseded(monkeypatch, tmp_path):
    client, _store = _client(monkeypatch, tmp_path)
    old = _payload()
    replacement = _payload(
        job_id="job-replacement",
        careplan_id="careplan-2",
        approved_at="2026-10-10T00:00:00+00:00",
        replaces_careplan_id="careplan-1",
    )
    for payload in (old, replacement):
        assert client.post(
            "/api/v1/automations", json=payload.model_dump(mode="json"),
            headers={"Idempotency-Key": payload.job_id},
        ).status_code == 201

    replaced = client.get("/api/v1/automations/careplan-1/automation-1")
    assert replaced.json()["status"] == "superseded"
    assert replaced.json()["replaced_by_careplan_id"] == "careplan-2"


def test_same_key_concurrent_replays_write_once(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    payload = _payload()
    body = payload.model_dump(mode="json")
    headers = {"Idempotency-Key": payload.job_id}

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(
            lambda _index: client.post("/api/v1/automations", json=body, headers=headers),
            range(6),
        ))

    assert sorted(response.status_code for response in responses) == [200, 200, 200, 200, 200, 201]
    assert len(store.list_records("careplan-1")) == 1


def test_publish_trace_contains_only_job_and_careplan_ids(monkeypatch, tmp_path):
    client, _store = _client(monkeypatch, tmp_path)
    payload = _payload()
    spans = []

    @contextmanager
    def start_span(*, name, attributes):
        spans.append((name, attributes))
        yield

    monkeypatch.setattr(app_module.mlflow, "start_span", start_span)
    response = client.post(
        "/api/v1/automations", json=payload.model_dump(mode="json"),
        headers={"Idempotency-Key": payload.job_id},
    )

    assert response.status_code == 201
    assert spans == [("automation.publish", {
        "job_id": payload.job_id,
        "careplan_id": payload.careplan_id,
    })]


def test_oversized_publication_body_is_rejected(monkeypatch, tmp_path):
    client, _store = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module.app.state, "max_request_bytes", 128)
    payload = _payload()

    response = client.post(
        "/api/v1/automations", json=payload.model_dump(mode="json"),
        headers={"Idempotency-Key": payload.job_id},
    )

    assert response.status_code == 413


def test_invalid_bpmn_and_malformed_task_are_rejected_with_422(monkeypatch, tmp_path):
    client, _store = _client(monkeypatch, tmp_path)
    bad_payload = _payload(xml="not BPMN XML")
    response = client.post(
        "/api/v1/automations",
        json=bad_payload.model_dump(mode="json"),
        headers={"Idempotency-Key": bad_payload.job_id},
    )
    assert response.status_code == 422

    malformed = _payload()
    malformed_data = malformed.model_dump(mode="json")
    malformed_data["job_id"] = "job-2"
    malformed_data["automations"][0]["task"]["input"] = [{"type": "bad-shape"}]
    malformed = type(malformed).model_validate(malformed_data)
    response = client.post(
        "/api/v1/automations",
        json=malformed.model_dump(mode="json"),
        headers={"Idempotency-Key": malformed.job_id},
    )
    assert response.status_code == 422


def test_directory_store_survives_a_new_store_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(store_module, "get_phi_store", lambda: None)
    store = AutomationStore(directory=tmp_path)
    record = {"automation_id": "automation-1", "revision": "a" * 12, "approved_at": "2026-10-09"}
    store.put_record("careplan-1", "automation-1", "a" * 12, record, BPMN)
    store.put_idempotency("job-1", {"payload_hash": "hash", "response": {"status": "accepted"}})

    restarted = AutomationStore(directory=tmp_path)

    assert restarted.get_record("careplan-1", "automation-1") == record
    assert restarted.get_idempotency("job-1")["response"]["status"] == "accepted"


def test_minio_artifact_store_branch_persists_records_markers_and_idempotency():
    class FakeArtifactStore:
        bucket = "phi"

        def __init__(self):
            self.objects = {}

        def put(self, key, value):
            self.objects[key] = value

        def put_raw(self, key, data, content_type):
            self.objects[key] = (data, content_type)

        def get(self, uri):
            bucket, key = uri.split(":", 1)
            assert bucket == self.bucket
            return self.objects[key]

        def _get_client(self):
            store = self

            class Client:
                def list_objects_v2(self, *, Bucket, Prefix):
                    assert Bucket == store.bucket
                    return {"Contents": [
                        {"Key": key} for key in store.objects if key.startswith(Prefix)
                    ]}

            return Client()

    artifact_store = FakeArtifactStore()
    store = AutomationStore(artifact_store=artifact_store)
    record = {
        "automation_id": "automation-1",
        "revision": "a" * 12,
        "approved_at": "2026-10-09T00:00:00+00:00",
        "bpmn_xml": BPMN,
    }

    store.put_record("careplan-1", "automation-1", "a" * 12, record, BPMN)
    store.put_idempotency("job-1", {"payload_hash": "hash", "response": {"status": "accepted"}})
    store.put_withdrawal("careplan-1", {"status": "withdrawn"})
    restarted = AutomationStore(artifact_store=artifact_store)

    assert restarted.get_record("careplan-1", "automation-1") == record
    assert restarted.list_records("careplan-1") == [record]
    assert restarted.get_idempotency("job-1")["response"]["status"] == "accepted"
    assert restarted.get_withdrawal("careplan-1")["status"] == "withdrawn"


def test_store_rejects_path_traversal_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(store_module, "get_phi_store", lambda: None)
    store = AutomationStore(directory=tmp_path)

    try:
        store.put_record("../outside", "automation-1", "a" * 12, {}, BPMN)
    except ValueError as exc:
        assert "unsupported storage characters" in str(exc)
    else:
        raise AssertionError("unsafe care plan ids must be rejected")
