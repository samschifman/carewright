"""Session-authenticated reviews bind one immutable artifact and current round."""
import copy
import json
from unittest.mock import AsyncMock

import pytest
from acp_writer.services import bff
from cpg_contracts.approvals import artifact_revision, verify_record
from fastapi.testclient import TestClient

BUNDLE = {'resourceType': 'Bundle', 'entry': [{'resource': {'resourceType': 'CarePlan', 'subject': {'reference': 'Patient/p1'}}}]}


class Store:
    def __init__(self): self.records = {'run-1/fhir_bundle.json': BUNDLE}
    def get(self, ref): return copy.deepcopy(self.records[ref.removeprefix('cpg-phi:')])
    def put(self, key, value): self.records[key] = copy.deepcopy(value); return 'cpg-phi:' + key


class FakeSonataFlow:
    def __init__(self):
        self.instance = {'id': 'sf-1', 'status': 'ACTIVE', 'workflowdata': {
            'fhirGenData': {'fhir_bundle_ref': 'cpg-phi:run-1/fhir_bundle.json'},
            'fhirReviewData': {'completed_at': '2026-01-01T01:00:00Z'}, 'careplanReviewCount': 0,
        }}
        self.send_review = AsyncMock()
    async def get_instance(self, sf_id): return copy.deepcopy(self.instance)
    async def get_instance_by_business_key(self, run_id): return copy.deepcopy(self.instance)


@pytest.fixture
def client(monkeypatch):
    fake, store = FakeSonataFlow(), Store()
    monkeypatch.setattr(bff, '_sonataflow', fake)
    monkeypatch.setattr(bff, '_phi_store', store)
    monkeypatch.setattr(bff, '_pending_runs', {})
    monkeypatch.setattr(bff, '_review_locks', bff.AsyncKeyedLocks())
    monkeypatch.setattr(bff, 'APPROVAL_SIGNING_KEY', 'test-signing-key')
    tc = TestClient(bff.app)
    tc.fake, tc.store = fake, store
    return tc


def review(client, **overrides):
    return client.post('/api/v1/runs/run-1/review/careplan', json={
        'decision': 'approve', 'reviewRound': 0, 'artifactRevision': artifact_revision(BUNDLE), **overrides,
    })


def test_matching_review_records_then_sends_one_event(client):
    assert review(client, clinician='forged', reviewer={'display': 'forged'}, completed_at='forged').status_code == 202
    record = client.store.records['run-1/approvals/0.json']
    assert verify_record(record, 'test-signing-key')
    assert record['workflowInstanceId'] == 'sf-1'
    assert record['fhirBundleRef'] == 'cpg-phi:run-1/fhir_bundle.json'
    assert record['sessionSubject'] == 'p1'
    assert record['reviewer']['display'] != 'forged'
    event = client.fake.send_review.await_args.args[2]
    assert event['approvalRef'] == 'cpg-phi:run-1/approvals/0.json'
    assert event['completed_at'] != 'forged'
    assert 'clinician' not in event
    assert review(client).status_code == 200
    assert review(client).json()['duplicate'] is True
    assert client.fake.send_review.await_count == 1


@pytest.mark.parametrize('overrides,status', [({'reviewRound': 1},409),({'artifactRevision': '0'*64},409),({'reviewRound': None},400),({'artifactRevision': None},400)])
def test_stale_or_missing_binding_is_refused(client, overrides, status):
    assert review(client, **overrides).status_code == status
    client.fake.send_review.assert_not_awaited()
    assert 'run-1/approvals/0.json' not in client.store.records


def test_changed_decision_under_same_round_is_stale(client):
    assert review(client).status_code == 202
    assert review(client, decision='request_changes').status_code == 409
    assert client.fake.send_review.await_count == 1


def test_event_transport_failure_can_retry_identical_signed_event(client):
    client.fake.send_review.side_effect = RuntimeError('sensitive upstream failure')
    assert review(client).status_code == 503
    saved = copy.deepcopy(client.store.records['run-1/approvals/0.json'])
    first = client.fake.send_review.await_args
    client.fake.send_review.side_effect = None
    assert review(client).status_code == 202
    assert client.store.records['run-1/approvals/0.json'] == saved
    assert client.fake.send_review.await_args == first
    assert review(client).json()['duplicate'] is True
    assert client.fake.send_review.await_count == 2


def test_unresolvable_workflow_writes_nothing(client):
    client.fake.get_instance_by_business_key = AsyncMock(return_value=None)
    assert review(client).status_code == 503
    assert 'run-1/approvals/0.json' not in client.store.records


def test_other_run_cannot_be_reviewed(client, monkeypatch):
    monkeypatch.setattr(bff, '_run_metadata_cache', {'other-run': {'patientId': 'another-patient'}})
    assert client.post('/api/v1/runs/other-run/review/careplan', json={'decision':'approve'}).status_code == 403
    client.fake.send_review.assert_not_awaited()


def test_get_run_resolves_by_business_key(client):
    response = client.get('/api/v1/runs/run-1')
    assert response.status_code == 200
    assert response.json()['artifactRevision'] == artifact_revision(BUNDLE)
    assert response.json()['artifactRef'] == 'cpg-phi:run-1/fhir_bundle.json'


def test_get_unknown_run_returns_404(client):
    assert client.get('/api/v1/runs/does-not-exist').status_code == 404
