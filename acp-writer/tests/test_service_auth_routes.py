"""Each protected route must reject before reading its request body."""
import importlib
import re

import pytest
from fastapi.testclient import TestClient

ROUTES = [('acp_writer.services.patient_data',
  'post',
  '/api/v1/scan',
  'SERVICE_TOKEN_ACP_PATIENT_DATA',
  True),
 ('acp_writer.services.patient_data',
  'post',
  '/api/v1/scan-async',
  'SERVICE_TOKEN_ACP_PATIENT_DATA',
  True),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/guidelines',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/knowledge/recommendations/batch',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/knowledge/search',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/execute-dmn',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/execute-dmn-async',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  True),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/resolve',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/resolve-async',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  True),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/retrieve',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/retrieve-async',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  True),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/compose',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/compose-async',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  True),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/review-fhir',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.services.llm_reasoning',
  'post',
  '/api/v1/review-fhir-async',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  True),
 ('acp_writer.services.fhir_generation',
  'post',
  '/api/v1/generate-bundle',
  'SERVICE_TOKEN_ACP_FHIR_GENERATION',
  False),
 ('acp_writer.services.fhir_generation',
  'post',
  '/api/v1/generate-bundle-async',
  'SERVICE_TOKEN_ACP_FHIR_GENERATION',
  True),
 ('acp_writer.services.fhir_server',
  'post',
  '/api/v1/write',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  True),
 ('acp_writer.services.fhir_server',
  'get',
  '/api/v1/careplans',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  False),
 ('acp_writer.services.fhir_server',
  'get',
  '/api/v1/careplans/{careplan_id}',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  False),
 ('acp_writer.services.fhir_server',
  'put',
  '/api/v1/careplans/{careplan_id}/status',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  False),
 ('acp_writer.services.fhir_server',
  'post',
  '/api/v1/careplans/{careplan_id}/automations/publish',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  False),
 ('acp_writer.services.decision_engine',
  'post',
  '/api/v1/decisions/models',
  'SERVICE_TOKEN_ACP_DECISION_ENGINE',
  False),
 ('acp_writer.services.decision_engine',
  'post',
  '/api/v1/evaluate',
  'SERVICE_TOKEN_ACP_DECISION_ENGINE',
  False),
 ('acp_writer.services.decision_engine',
  'post',
  '/api/v1/execute',
  'SERVICE_TOKEN_ACP_DECISION_ENGINE',
  False),
 ('acp_writer.services.decision_engine',
  'post',
  '/api/v1/automation/templates',
  'SERVICE_TOKEN_ACP_DECISION_ENGINE',
  False),
 ('acp_writer.services.decision_engine',
  'post',
  '/api/v1/automation/validate',
  'SERVICE_TOKEN_ACP_DECISION_ENGINE',
  False),
 ('acp_writer.services.bff',
  'post',
  '/api/v1/notifications/artifacts-available',
  'SERVICE_TOKEN_ACP_BFF',
  True),
 ('acp_writer.services.bff', 'post', '/api/v1/notify-review', 'SERVICE_TOKEN_ACP_BFF', True),
 ('acp_writer.api', 'post', '/api/v1/careplans', 'SERVICE_TOKEN_ACP_FHIR_SERVER', False),
 ('acp_writer.api', 'get', '/api/v1/careplans', 'SERVICE_TOKEN_ACP_FHIR_SERVER', False),
 ('acp_writer.api',
  'get',
  '/api/v1/careplans/{careplan_id}',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  False),
 ('acp_writer.api',
  'put',
  '/api/v1/careplans/{careplan_id}/status',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  False),
 ('acp_writer.api',
  'post',
  '/api/v1/careplans/{careplan_id}/automations/publish',
  'SERVICE_TOKEN_ACP_FHIR_SERVER',
  False),
 ('acp_writer.api', 'post', '/api/v1/decisions/models', 'SERVICE_TOKEN_ACP_DECISION_ENGINE', False),
 ('acp_writer.api',
  'delete',
  '/api/v1/decisions/models/{model_id}',
  'SERVICE_TOKEN_ACP_DECISION_ENGINE',
  False),
 ('acp_writer.api',
  'post',
  '/api/v1/decisions/evaluate/{model_id}',
  'SERVICE_TOKEN_ACP_DECISION_ENGINE',
  False),
 ('acp_writer.api', 'post', '/api/v1/guidelines', 'SERVICE_TOKEN_ACP_LLM_REASONING', False),
 ('acp_writer.api',
  'delete',
  '/api/v1/guidelines/{cpg_id}',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.api',
  'post',
  '/api/v1/knowledge/recommendations',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.api',
  'post',
  '/api/v1/knowledge/recommendations/batch',
  'SERVICE_TOKEN_ACP_LLM_REASONING',
  False),
 ('acp_writer.api', 'post', '/api/v1/knowledge/search', 'SERVICE_TOKEN_ACP_LLM_REASONING', False)]


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
