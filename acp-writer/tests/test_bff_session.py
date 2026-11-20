"""Patient scoping must survive BFF memory loss and ignore caller filters."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from acp_writer.services import bff
from cpg_contracts.web_session import (
    MemorySessionStore,
    ObjectStoreSessionStore,
    SessionManager,
)
from fastapi.testclient import TestClient


class Objects:
    def __init__(self): self.records = {}
    def get(self, key): return json.loads(json.dumps(self.records[key.removeprefix('cpg-phi:')]))
    def put(self, key, value):
        self.records[key] = json.loads(json.dumps(value))
        return 'cpg-phi:' + key
    def delete(self, key): self.records.pop(key, None)
    def list_keys(self, prefix): return [key for key in self.records if key.startswith(prefix)]


@pytest.fixture
def context(monkeypatch):
    store = Objects()
    monkeypatch.setattr(bff, '_phi_store', store)
    monkeypatch.setattr(bff, 'SESSION_SIGNING_KEY', 'test-signing-key')
    monkeypatch.setattr(bff, '_run_metadata_cache', {})
    monkeypatch.setattr(bff, '_pending_runs', {})
    monkeypatch.setattr(bff, '_sonataflow', None)
    manager = SessionManager(ObjectStoreSessionStore(store, signing_key='test-signing-key'), cookie_name='acp_session', public_origin='https://writer.example')
    monkeypatch.setattr(bff, '_sessions', manager)
    def browser(patient, runs=None, issuer='https://ehr.example/fhir/R4/'):
        session, cookie = manager.create('patient', patient, runs, meta={'iss': issuer})
        client = TestClient(bff.app, base_url='https://writer.example')
        client.cookies.set('acp_session', cookie)
        client.headers['X-CSRF-Token'] = session.csrf_token
        return client, session
    return store, browser


@pytest.mark.asyncio
async def test_restart_relaunch_scopes_old_runs(context, monkeypatch):
    store, browser = context
    bundle = {'resourceType': 'Bundle', 'entry': [{'resource': {'resourceType': 'Patient', 'id': 'synthetic'}}]}
    run = await bff._create_patient_run(bundle, 'synthetic', 'https://ehr.example/fhir/R4/')
    run_id = run['runId']
    instance = {'id': 'workflow-id', 'businessKey': run_id, 'status': 'ACTIVE', 'workflowdata': {}}
    bff._persist_run_metadata({**bff.run_metadata(run_id), 'workflowInstanceId': 'workflow-id'})
    monkeypatch.setattr(bff, '_pending_runs', {})
    monkeypatch.setattr(bff, '_run_metadata_cache', {})
    monkeypatch.setattr(bff, '_sonataflow', SimpleNamespace(
        list_instances=AsyncMock(return_value=[instance]), get_instance=AsyncMock(return_value=instance),
        get_instance_by_business_key=AsyncMock(return_value=instance), abort_instance=AsyncMock(),
    ))
    client, session = browser('synthetic')
    assert session.run_ids == []
    assert client.get('/api/v1/runs').json()[0]['runId'] == run_id
    assert client.get('/api/v1/runs/' + run_id).status_code == 200
    same_id_other_issuer, _ = browser('synthetic', issuer='https://another-ehr.example/fhir/R4/')
    assert same_id_other_issuer.get('/api/v1/runs').json() == []
    assert same_id_other_issuer.get('/api/v1/runs/' + run_id).status_code == 403
    other, _ = browser('another-patient')
    assert other.get('/api/v1/runs').json() == []
    for method, suffix in [('get', ''), ('get', '/artifacts/fhir_bundle.json'), ('delete', ''), ('post', '/review/careplan')]:
        assert getattr(other, method)('/api/v1/runs/' + run_id + suffix).status_code == 403
    assert other.get('/api/v1/runs/unknown').status_code == 404


def test_careplan_scope_uses_recorded_patient_and_issuer_not_caller_filters(context, monkeypatch):
    _, browser = context
    client, _ = browser('synthetic')
    issuer = 'https://ehr.example/fhir/R4/'
    # The bundle's subject is bundle-local in real output; it must be ignored.
    bundle = {'entry': [{'resource': {'resourceType': 'CarePlan', 'subject': {'reference': 'urn:uuid:local'}}}]}
    detail = {'id': 'own', 'patient_reference': 'Patient/synthetic', 'issuer': issuer, 'bundle': bundle}
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.path == '/api/v1/careplans':
            return httpx.Response(200, json=[
                {'id': 'own', 'patient_reference': 'Patient/synthetic', 'issuer': issuer},
                {'id': 'other', 'patient_reference': 'Patient/another', 'issuer': issuer},
                {'id': 'other-issuer', 'patient_reference': 'Patient/synthetic', 'issuer': 'https://another-ehr.example/fhir/R4/'},
                {'id': 'no-issuer', 'patient_reference': 'Patient/synthetic'},
                {'id': 'local-subject', 'patient_reference': 'urn:uuid:synthetic', 'issuer': issuer},
            ])
        return httpx.Response(200, json=detail)
    monkeypatch.setattr(bff, '_http', httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    assert [plan['id'] for plan in client.get('/api/v1/careplans?patient=another').json()] == ['own']
    assert calls[-1].url.params['patient'] == 'synthetic'
    assert client.get('/api/v1/careplans/own').status_code == 200
    for changed in ({'patient_reference': 'Patient/another'}, {'patient_reference': 'Practitioner/synthetic'},
                    {'patient_reference': 'urn:uuid:synthetic'}, {'patient_reference': 'invalid'},
                    {'patient_reference': None}, {'issuer': 'https://another-ehr.example/fhir/R4/'}, {'issuer': ''}):
        detail.update({'patient_reference': 'Patient/synthetic', 'issuer': issuer, **changed})
        assert client.get('/api/v1/careplans/own').status_code == 403, changed
    # A bundle that names this patient does not grant access on its own.
    detail.update({'patient_reference': '', 'issuer': issuer,
                   'bundle': {'entry': [{'resource': {'resourceType': 'CarePlan', 'subject': {'reference': 'Patient/synthetic'}}}]}})
    assert client.get('/api/v1/careplans/own').status_code == 403


@pytest.mark.parametrize('reference, expected', [
    ('Patient/synthetic', 'synthetic'),
    ('https://ehr.example/fhir/R4/Patient/synthetic', 'synthetic'),
    ('Patient/another', 'another'),
    ('Practitioner/synthetic', None),
    ('urn:uuid:synthetic', None),
    ('https://ehr.example/fhir/R4/Patient/synthetic?x=1', None),
    ('invalid', None),
    (None, None),
])
def test_reference_patient_parsing(reference, expected):
    assert bff._reference_patient(reference) == expected


def test_expired_session_and_anonymous_routes(context, monkeypatch):
    _, browser = context
    anonymous = TestClient(bff.app)
    for path in ('/api/v1/runs', '/api/v1/status', '/api/v1/careplans', '/api/v1/runs/unknown'):
        assert anonymous.get(path).status_code == 401
    client, session = browser('synthetic')
    session.expires_at = time.time() - 1
    bff._sessions.store.put(session)
    assert client.get('/api/v1/session').status_code == 401
