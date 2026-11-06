import secrets

import pytest
from cpg_contracts.service_auth import require_service_token, service_token_headers
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    app = FastAPI()
    @app.get('/normal', dependencies=[Depends(require_service_token('TEST_SERVICE_TOKEN'))])
    @app.get('/workflow', dependencies=[Depends(require_service_token('TEST_SERVICE_TOKEN', workflow_route=True))])
    def endpoint():
        return {'ok': True}
    return TestClient(app)


def test_credential_states(client, monkeypatch):
    monkeypatch.delenv('TEST_SERVICE_TOKEN', raising=False)
    monkeypatch.delenv('WORKFLOW_CALLER_UNAUTHENTICATED', raising=False)
    assert client.get('/normal').status_code == 503
    monkeypatch.setenv('TEST_SERVICE_TOKEN', 'test-only-credential')
    assert client.get('/normal').status_code == 401
    assert client.get('/normal', headers={'X-Service-Token': 'wrong'}).status_code == 401
    calls = []
    original = secrets.compare_digest
    def compare(a, b):
        calls.append(True)
        return original(a, b)
    monkeypatch.setattr(secrets, 'compare_digest', compare)
    assert client.get('/normal', headers=service_token_headers('TEST_SERVICE_TOKEN')).status_code == 200
    assert calls
    assert set(service_token_headers('TEST_SERVICE_TOKEN')) == {'X-Service-Token'}
    monkeypatch.delenv('TEST_SERVICE_TOKEN')
    assert service_token_headers('TEST_SERVICE_TOKEN') == {}


def test_workflow_fallback_is_explicit_and_header_cannot_bypass(client, monkeypatch):
    monkeypatch.setenv('TEST_SERVICE_TOKEN', 'test-only-credential')
    monkeypatch.delenv('WORKFLOW_CALLER_UNAUTHENTICATED', raising=False)
    assert client.get('/workflow').status_code == 401
    monkeypatch.setenv('WORKFLOW_CALLER_UNAUTHENTICATED', 'true')
    assert client.get('/workflow').status_code == 200
    assert client.get('/normal').status_code == 401
    assert client.get('/workflow', headers={'X-Service-Token': ''}).status_code == 401
    assert client.get('/workflow', headers={'X-Service-Token': 'wrong'}).status_code == 401
    monkeypatch.delenv('TEST_SERVICE_TOKEN')
    assert client.get('/workflow').status_code == 200
    assert client.get('/workflow', headers={'X-Service-Token': 'wrong'}).status_code == 503
