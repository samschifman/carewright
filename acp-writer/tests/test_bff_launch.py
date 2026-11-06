"""SMART authorization-code, PKCE, browser nonce and patient binding."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from acp_writer.services import bff
from cpg_contracts.web_session import (
    MemorySessionStore,
    OneTimeStore,
    SessionManager,
    token_hash,
)
from fastapi.testclient import TestClient


class FakeAuthorizationServer:
    def __init__(self):
        self.codes = {}
        self.patient = 'synthetic-patient'
        self.summary_status = 200
        self.calls = []

    def authorize(self, location):
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert query['code_challenge_method'] == 'S256'
        assert query['scope'] == 'launch openid fhirUser patient/*.read user/Practitioner.read'
        self.codes['synthetic-code-' + query['state']] = query['code_challenge']
        return 'synthetic-code-' + query['state'], query['state']

    def handle(self, request):
        self.calls.append(request)
        if request.url.path.endswith('.well-known/smart-configuration'):
            return httpx.Response(200, json={'authorization_endpoint': 'https://ehr.example/oauth2/authorize', 'token_endpoint': 'https://ehr.example/oauth2/token'})
        if request.url.path == '/oauth2/token':
            assert request.headers['authorization'] == 'Basic ' + base64.b64encode(b'client:client-secret').decode()
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            challenge = self.codes.pop(form['code'], None)
            actual = base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'].encode()).digest()).rstrip(b'=').decode()
            if actual != challenge:
                return httpx.Response(400, json={'error': 'invalid_grant'})
            return httpx.Response(200, json={'access_token': 'synthetic-access-token', 'patient': self.patient})
        if request.url.path.endswith('/$summary'):
            assert request.headers['authorization'] == 'Bearer synthetic-access-token'
            return httpx.Response(self.summary_status, json={'resourceType': 'Bundle', 'entry': [{'resource': {'resourceType': 'Patient', 'id': self.patient}}]})
        raise AssertionError('Unexpected server request')


@pytest.fixture
def browser(monkeypatch):
    server = FakeAuthorizationServer()
    for key, value in {
        'SMART_CLIENT_ID': 'client', 'SMART_CLIENT_SECRET': 'client-secret',
        'SMART_ISS_ALLOWLIST': 'https://ehr.example/fhir/R4/',
        'SMART_REDIRECT_URI': 'https://writer.example/api/v1/launch/callback',
        'ACP_PUBLIC_ORIGIN': 'https://writer.example', 'SMART_FHIR_INTERNAL_BASE': '',
        'ACP_ALLOW_DIRECT_IPS': False,
    }.items():
        monkeypatch.setattr(bff, key, value)
    monkeypatch.setattr(bff, '_http', httpx.AsyncClient(transport=httpx.MockTransport(server.handle)))
    monkeypatch.setattr(bff, '_sessions', SessionManager(MemorySessionStore(), cookie_name='acp_session', public_origin='https://writer.example'))
    monkeypatch.setattr(bff, '_launch_states', OneTimeStore())
    monkeypatch.setattr(bff, '_phi_store', None)
    monkeypatch.setattr(bff, '_pending_runs', {})
    monkeypatch.setattr(bff, '_run_metadata_cache', {})
    monkeypatch.setattr(bff, '_discovery_cache', {})
    monkeypatch.setattr(bff, '_sonataflow', None)
    client = TestClient(bff.app, base_url='https://writer.example', follow_redirects=False)
    client.server = server
    return client


def begin(browser):
    response = browser.get('/api/v1/launch', params={'iss': 'https://ehr.example/fhir/R4', 'launch': 'synthetic-launch'})
    assert response.status_code == 302
    assert 'HttpOnly' in response.headers['set-cookie']
    return browser.server.authorize(response.headers['location'])


def callback(browser, code, state):
    return browser.get('/api/v1/launch/callback', params={'code': code, 'state': state})


def test_launch_session_and_logout(browser):
    assert browser.get('/api/v1/session').status_code == 401
    code, state = begin(browser)
    response = callback(browser, code, state)
    assert response.status_code == 302
    assert response.headers['location'].startswith('https://writer.example/runs/')
    assert 'acp_session=' in response.headers['set-cookie']
    for attribute in ('HttpOnly', 'Secure', 'SameSite=lax', 'Path=/'):
        assert attribute in response.headers['set-cookie']
    old_cookie = browser.cookies.get('acp_session')
    session = browser.get('/api/v1/session').json()
    assert session['subject'] == 'synthetic-patient'
    assert session['kind'] == 'patient'
    run_id = session['runIds'][0]
    assert browser.get('/api/v1/runs/' + run_id).status_code == 200
    assert browser.get('/api/v1/runs').json()[0]['runId'] == run_id
    assert browser.get('/api/v1/status').status_code == 200
    assert browser.post('/api/v1/session/logout').status_code == 403
    assert browser.post('/api/v1/session/logout', headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert browser.post('/api/v1/session/logout', headers={'X-CSRF-Token': session['csrfToken']}).status_code == 204
    assert browser.get('/api/v1/session').status_code == 401
    browser.cookies.set('acp_session', old_cookie)
    assert browser.get('/api/v1/session').status_code == 401
    assert 'synthetic-access-token' not in json.dumps(bff._pending_runs)
    assert 'synthetic-access-token' not in json.dumps(bff._sessions.store._sessions, default=lambda s: s.__dict__)


def test_issuer_state_and_login_csrf(browser):
    assert browser.get('/api/v1/launch', params={'iss': 'https://untrusted.example/', 'launch': 'x'}).status_code == 400
    assert callback(browser, 'unknown', 'unknown').status_code == 400
    code, state = begin(browser)
    browser.cookies.clear()
    assert callback(browser, code, state).status_code == 400
    code, state = begin(browser)
    browser.cookies.set('acp_launch', 'different-browser', domain='writer.example', path='/')
    assert callback(browser, code, state).status_code == 400


def test_replay_missing_patient_and_bad_pkce(browser):
    code, state = begin(browser)
    assert callback(browser, code, state).status_code == 302
    _, fresh_state = begin(browser)
    assert callback(browser, code, fresh_state).status_code == 403
    code, state = begin(browser)
    bff._launch_states._records['pending/' + token_hash(state) + '.json']['payload']['verifier'] = 'wrong-verifier'
    assert callback(browser, code, state).status_code == 403
    code, state = begin(browser)
    browser.server.patient = None
    assert callback(browser, code, state).status_code == 403


def test_summary_failure_and_oauth_error(browser):
    code, state = begin(browser)
    browser.server.summary_status = 503
    assert callback(browser, code, state).status_code == 502
    _, state = begin(browser)
    assert browser.get('/api/v1/launch/callback', params={'state': state, 'error': 'access_denied'}).status_code == 403


def test_direct_submission_is_opt_in_and_scoped(browser, monkeypatch):
    bundle = {'resourceType': 'Bundle', 'entry': [{'resource': {'resourceType': 'Patient', 'id': 'synthetic-patient'}}]}
    assert browser.post('/api/v1/runs', json={'ipsBundle': bundle}).status_code == 404
    monkeypatch.setattr(bff, 'ACP_ALLOW_DIRECT_IPS', True)
    response = browser.post('/api/v1/runs', json={'ipsBundle': bundle})
    assert response.status_code == 202
    assert bff.run_metadata(response.json()['runId'])['patientId'] == 'synthetic-patient'
    assert browser.get('/api/v1/session').json()['directIpsEnabled'] is True


def test_session_store_failure_is_503(browser, monkeypatch):
    code, state = begin(browser)
    assert callback(browser, code, state).status_code == 302
    def failed(*args): raise ConnectionError('internal details')
    monkeypatch.setattr(bff._sessions.store, 'get', failed)
    response = browser.get('/api/v1/runs')
    assert response.status_code == 503
    assert response.json() == {'detail': {'error': 'session-store-unavailable'}}


def test_summary_allows_repeated_patient_but_rejects_other_identity():
    patient = {'resource': {'resourceType': 'Patient', 'id': 'synthetic'}}
    assert bff._patient_id({'entry': [patient, patient]}) == 'synthetic'
    assert bff._patient_id({'entry': [patient, {'resource': {'resourceType': 'Patient', 'id': 'other'}}]}) is None
    assert bff._patient_id({'entry': [patient, {'resource': {'resourceType': 'Patient'}}]}) is None


def test_discovery_failure_does_not_guess_provider_endpoints(browser, monkeypatch):
    monkeypatch.setattr(bff, '_http', httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(503))))
    response = browser.get('/api/v1/launch', params={'iss':'https://ehr.example/fhir/R4/', 'launch':'synthetic'})
    assert response.status_code == 503
    assert response.json() == {'detail':{'error':'smart-unconfigured'}}


def test_persistent_launch_requires_session_key(browser, monkeypatch):
    monkeypatch.setattr(bff, '_phi_store', object())
    monkeypatch.setattr(bff, 'SESSION_SIGNING_KEY', '')
    response = browser.get('/api/v1/launch', params={'iss':'https://ehr.example/fhir/R4/', 'launch':'synthetic'})
    assert response.status_code == 503
    assert response.json() == {'detail':{'error':'signing-key-unconfigured'}}


def test_unsigned_ips_cannot_become_signed_run_metadata(browser, monkeypatch):
    from unittest.mock import Mock
    store = Mock()
    store.get.side_effect = KeyError('no run.json')
    monkeypatch.setattr(bff, '_phi_store', store)
    monkeypatch.setattr(bff, 'SESSION_SIGNING_KEY', 'synthetic-session-key')
    assert bff.run_metadata('planted-run') is None
    assert store.get.call_args.args == ('planted-run/run.json',)
    store.put.assert_not_called()


def test_writer_module_defaults_to_secure_cookies(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    from starlette.responses import Response
    monkeypatch.delenv('SESSION_COOKIE_SECURE', raising=False)
    monkeypatch.delenv('MINIO_ENDPOINT', raising=False)
    monkeypatch.delenv('SONATAFLOW_URL', raising=False)
    monkeypatch.setenv('ACP_PUBLIC_ORIGIN','https://writer.example')
    spec=importlib.util.spec_from_file_location('writer_secure_cookie_bff',Path(bff.__file__))
    module=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,spec.name,module)
    spec.loader.exec_module(module)
    assert module.SESSION_COOKIE_SECURE is True
    _,cookie=module._sessions.create('patient','synthetic')
    response=Response()
    module._sessions.set_cookie(response,cookie)
    for attribute in ('Secure','HttpOnly','SameSite=lax','Path=/'):
        assert attribute in response.headers['set-cookie']
