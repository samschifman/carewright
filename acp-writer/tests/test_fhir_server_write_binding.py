"""No write entry point may trust a token alone or an unsigned object."""
import copy
import importlib
import json
from unittest.mock import Mock

import pytest
from acp_writer.services.reviewer import default_reviewer
from cpg_contracts.approvals import artifact_revision, sign_record
from fastapi.testclient import TestClient

BUNDLE = {'resourceType': 'Bundle', 'type': 'transaction', 'entry': [{'resource': {
    'resourceType': 'CarePlan', 'id': 'synthetic', 'subject': {'reference': 'Patient/synthetic'},
}}]}


class Store:
    def __init__(self, record): self.record = record; self.bundle = copy.deepcopy(BUNDLE)
    def get_raw_in_bucket(self, bucket, key):
        assert bucket == 'cpg-phi'
        if key == 'run-1/approvals/0.json':
            if self.record is None: raise KeyError(key)
            return json.dumps(self.record).encode()
        if key in {'run-1/fhir_bundle.json', 'other/equal_bundle.json'}:
            return json.dumps(self.bundle).encode()
        raise KeyError(key)


@pytest.fixture(params=['acp_writer.services.fhir_server', 'acp_writer.api'])
def context(request, monkeypatch):
    monkeypatch.setenv('APPROVAL_SIGNING_KEY', 'test-signing-key')
    record = sign_record({
        'runId': 'run-1', 'workflowInstanceId': 'workflow-1', 'reviewRound': 0,
        'fhirBundleRef': 'cpg-phi:run-1/fhir_bundle.json', 'artifactRevision': artifact_revision(BUNDLE),
        'decision': 'approve', 'reviewer': default_reviewer().model_dump(),
    }, 'test-signing-key')
    store = Store(record)
    module = importlib.import_module(request.param)
    writer = Mock(return_value={'delivery_status': 'delivered'})
    if request.param.endswith('.api'):
        monkeypatch.setattr('cpg_contracts.get_phi_store', lambda: store)
        monkeypatch.setattr('acp_writer.nodes.fhir_server_writer.fhir_server_writer', writer)
    else:
        monkeypatch.setattr(module, '_phi_store', store)
        monkeypatch.setattr(module, 'fhir_server_writer', writer)
    body = {'approval_ref': 'cpg-phi:run-1/approvals/0.json', 'careplan_id': 'workflow-1', 'fhir_bundle_ref': 'cpg-phi:run-1/fhir_bundle.json'}
    return TestClient(module.app), store, writer, body


def test_matching_approval_writes_exact_reviewed_artifact(context):
    client, store, writer, body = context
    response = client.post('/api/v1/write', json={**body, 'approved': False, 'reviewer': {'display': 'untrusted'}})
    assert response.status_code == 200
    state = writer.call_args.args[0]
    assert state['fhir_bundle'] == BUNDLE
    assert state['approved'] is True
    assert state['reviewer'] == default_reviewer().model_dump()
    assert state['patient_reference'] == 'Patient/synthetic'
    assert response.json()['artifactRevision'] == artifact_revision(BUNDLE)
    assert response.json()['reviewRound'] == 0


@pytest.mark.parametrize('case,status', [('hash',409),('reference',409),('workflow',409),('missing',403),('tampered',403),('wrong-key',403),('unconfigured',503),('outside',400),('malformed',400),('denied',403)])
def test_rejects_before_any_write(context, case, status, monkeypatch):
    client, store, writer, body = context
    if case == 'hash': store.bundle['entry'][0]['resource']['id'] = 'changed'
    if case == 'reference': body['fhir_bundle_ref'] = 'cpg-phi:other/equal_bundle.json'
    if case == 'workflow': body['careplan_id'] = 'another-workflow'
    if case == 'missing': store.record = None
    if case == 'tampered': store.record['artifactRevision'] = 'forged'
    if case == 'wrong-key': store.record = sign_record(store.record, 'another-key')
    if case == 'unconfigured': monkeypatch.delenv('APPROVAL_SIGNING_KEY')
    if case == 'outside': body['approval_ref'] = 'cpg-artifacts:run-1/approvals/0.json'
    if case == 'malformed': body['approval_ref'] = 'cpg-phi:run-1/approvals/../0.json'
    if case == 'denied': store.record = sign_record({**store.record, 'decision': 'request_changes'}, 'test-signing-key')
    response = client.post('/api/v1/write', json=body)
    assert response.status_code == status, response.text
    writer.assert_not_called()


def test_legacy_activation_cannot_bypass_approval(context):
    client, store, writer, body = context
    assert client.put('/api/v1/careplans/workflow-1/status', json={'status': 'active', 'reviewer': {'display': 'forged'}}).status_code == 403
    writer.assert_not_called()
    assert client.put('/api/v1/careplans/workflow-1/status', json={**body, 'status': 'active'}).status_code == 200
    writer.assert_called_once()
