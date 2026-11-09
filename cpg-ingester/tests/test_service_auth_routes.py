"""Each protected route must reject before reading its request body."""
import importlib
import re

import pytest
from fastapi.testclient import TestClient

ROUTES = [('cpg_ingester.services.ingestion', 'post', '/api/v1/parse', 'SERVICE_TOKEN_CPG_INGESTION', False),
 ('cpg_ingester.services.ingestion',
  'post',
  '/api/v1/parse-async',
  'SERVICE_TOKEN_CPG_INGESTION',
  True),
 ('cpg_ingester.services.llm_analysis',
  'post',
  '/api/v1/analyze',
  'SERVICE_TOKEN_CPG_LLM_ANALYSIS',
  False),
 ('cpg_ingester.services.llm_analysis',
  'post',
  '/api/v1/analyze-async',
  'SERVICE_TOKEN_CPG_LLM_ANALYSIS',
  True),
 ('cpg_ingester.services.llm_analysis',
  'post',
  '/api/v1/generate',
  'SERVICE_TOKEN_CPG_LLM_ANALYSIS',
  False),
 ('cpg_ingester.services.llm_analysis',
  'post',
  '/api/v1/generate-async',
  'SERVICE_TOKEN_CPG_LLM_ANALYSIS',
  True),
 ('cpg_ingester.services.assembly_svc',
  'post',
  '/api/v1/assemble',
  'SERVICE_TOKEN_CPG_ASSEMBLY',
  True),
 ('cpg_ingester.services.delivery_svc',
  'post',
  '/api/v1/deliver',
  'SERVICE_TOKEN_CPG_DELIVERY',
  True),
 ('cpg_ingester.services.bff', 'post', '/api/v1/notify-review', 'SERVICE_TOKEN_CPG_BFF', True)]


@pytest.mark.parametrize('module,method,path,env_name,workflow', ROUTES)
def test_service_route_credential_boundary(module, method, path, env_name, workflow, monkeypatch):
    app = importlib.import_module(module).app
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop('X-Service-Token', None)
    url = re.sub(r'\{[^}]+\}', 'auth-test-id', path)
    monkeypatch.delenv(env_name, raising=False)
    response = client.request(method, url)
    assert response.status_code == 503, (module, path, response.text)
    assert response.json() == {'detail': {'error': 'service-credential-unconfigured'}}
    monkeypatch.setenv(env_name, 'receiver-test-credential')
    assert client.request(method, url).status_code == 401
    assert client.request(method, url, headers={'X-Service-Token': 'wrong'}).status_code == 401
    # Malformed bodies are deliberate: admission must progress past auth to
    # ordinary validation/handler behavior without scheduling meaningful work.
    response = client.request(method, url, headers={'X-Service-Token': 'receiver-test-credential'}, json={})
    assert response.status_code != 401
    assert 'service-credential-unconfigured' not in response.text
    monkeypatch.setenv('WORKFLOW_CALLER_UNAUTHENTICATED', 'true')
    response = client.request(method, url, json={})
    if workflow:
        assert response.status_code != 401
        assert 'service-credential-unconfigured' not in response.text
    else:
        assert response.status_code == 401
    assert client.request(method, url, headers={'X-Service-Token': 'wrong'}).status_code == 401


@pytest.mark.parametrize('module,method,path,env_name,workflow', ROUTES)
def test_token_rejection_never_schedules_background(module, method, path, env_name, workflow, monkeypatch):
    from unittest.mock import Mock

    from starlette.background import BackgroundTasks
    schedule = Mock()
    monkeypatch.setattr(BackgroundTasks, 'add_task', schedule)
    monkeypatch.setenv(env_name, 'expected-receiver-token')
    monkeypatch.setenv('WORKFLOW_CALLER_UNAUTHENTICATED', 'false')
    client = TestClient(importlib.import_module(module).app, raise_server_exceptions=False)
    client.headers.pop('X-Service-Token', None)
    url = re.sub(r'\{[^}]+\}', 'auth-test-id', path)
    for headers in ({}, {'X-Service-Token':'wrong'}):
        assert client.request(method, url, json={'callback_url':'http://workflow.test/wait'}, headers=headers).status_code == 401
    schedule.assert_not_called()
