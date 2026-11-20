"""Operator sessions fail closed and one-time launch links cannot be replayed."""
from cpg_ingester.services.browser_auth import install_browser_auth
from fastapi import FastAPI
from fastapi.testclient import TestClient


def client(monkeypatch):
    monkeypatch.setenv('CPG_PUBLIC_ORIGIN','http://localhost')
    monkeypatch.setenv('SESSION_COOKIE_SECURE','false')
    app = FastAPI()
    @app.get('/api/v1/runs')
    def runs(): return []
    @app.post('/api/v1/upload')
    def upload(): return {}
    install_browser_auth(app,None)
    return TestClient(app,base_url='http://localhost')


def test_single_use_session_csrf_and_logout(monkeypatch):
    c = client(monkeypatch)
    assert c.get('/api/v1/runs').status_code == 401
    link = c.post('/api/v1/admin/launch-links').json()['url']
    assert c.get(link,follow_redirects=False).status_code == 302
    assert c.get(link).status_code == 403
    session = c.get('/api/v1/session')
    assert session.status_code == 200
    assert c.get('/api/v1/runs').status_code == 200
    c.headers.pop('X-CSRF-Token',None)
    assert c.post('/api/v1/upload').status_code == 403
    c.headers['X-CSRF-Token'] = session.json()['csrfToken']
    assert c.post('/api/v1/upload',headers={'Origin':'https://untrusted.example'}).status_code == 403
    assert c.post('/api/v1/upload').status_code == 200
    old_cookie = c.cookies.get('cpg_session')
    assert c.post('/api/v1/session/logout').status_code == 200
    assert c.get('/api/v1/session').status_code == 401
    c.cookies.set('cpg_session', old_cookie)
    assert c.get('/api/v1/session').status_code == 401


def test_admin_credentials_and_expiry(monkeypatch):
    c = client(monkeypatch)
    c.headers.clear()
    monkeypatch.delenv('SERVICE_TOKEN_CPG_BFF')
    assert c.post('/api/v1/admin/launch-links').status_code == 503
    monkeypatch.setenv('SERVICE_TOKEN_CPG_BFF','synthetic-admin-token')
    assert c.post('/api/v1/admin/launch-links').status_code == 401
    c.headers['X-Service-Token']='synthetic-admin-token'
    assert c.post('/api/v1/admin/launch-links').status_code == 200
    expired = c.app.state.launch_links.issue({'kind':'operator'},-1)
    assert c.get('/api/v1/launch/'+expired).status_code == 403


def test_options_bypasses_session_and_secure_cookie_default(monkeypatch):
    monkeypatch.setenv('CPG_PUBLIC_ORIGIN', 'https://ingester.test')
    monkeypatch.delenv('SESSION_COOKIE_SECURE', raising=False)
    app = FastAPI()
    @app.options('/api/v1/runs')
    def options(): return {}
    install_browser_auth(app, None)
    c = TestClient(app, base_url='https://ingester.test')
    assert c.options('/api/v1/runs').status_code == 200
    link = c.post('/api/v1/admin/launch-links').json()['url']
    response = c.get(link, follow_redirects=False)
    assert response.status_code == 302
    for attr in ('Secure', 'HttpOnly', 'SameSite=lax', 'Path=/'):
        assert attr in response.headers['set-cookie']


def _app(monkeypatch, *, origin, auto, mock_mode, secure='false'):
    import pytest  # noqa: F401
    monkeypatch.setenv('CPG_PUBLIC_ORIGIN', origin)
    monkeypatch.setenv('SESSION_COOKIE_SECURE', secure)
    if auto is None:
        monkeypatch.delenv('CPG_DEV_AUTO_SESSION', raising=False)
    else:
        monkeypatch.setenv('CPG_DEV_AUTO_SESSION', auto)
    app = FastAPI()
    @app.get('/api/v1/runs')
    def runs(): return []
    @app.post('/api/v1/upload')
    def upload(): return {}
    install_browser_auth(app, None, mock_mode=mock_mode)
    return TestClient(app, base_url=origin or 'http://localhost')


def test_dev_auto_session_reaches_upload_on_localhost_mock_mode(monkeypatch):
    """The local quickstart: the documented settings give the UI an operator session."""
    c = _app(monkeypatch, origin='http://localhost:3003', auto='true', mock_mode=True)
    c.headers.clear()  # a browser sends no service credential
    session = c.get('/api/v1/session')
    assert session.status_code == 200 and session.json()['kind'] == 'operator'
    assert 'cpg_session=' in session.headers['set-cookie'] and 'Secure' not in session.headers['set-cookie']
    assert c.get('/api/v1/runs').status_code == 200
    headers = {'X-CSRF-Token': session.json()['csrfToken'], 'Origin': 'http://localhost:3003'}
    assert c.post('/api/v1/upload', headers=headers).status_code == 200


def test_dev_auto_session_is_off_by_default_and_outside_mock_mode(monkeypatch):
    for auto, mock_mode in ((None, True), ('false', True), ('true', False)):
        c = _app(monkeypatch, origin='http://localhost:3003', auto=auto, mock_mode=mock_mode)
        assert c.get('/api/v1/session').status_code == 401, (auto, mock_mode)
        assert c.get('/api/v1/runs').status_code == 401


def test_dev_auto_session_refuses_to_start_off_localhost(monkeypatch):
    import pytest
    for origin in ('https://ingester.apps.cluster.example', 'http://localhost.evil.example', ''):
        for mock_mode in (True, False):
            with pytest.raises(RuntimeError, match='CPG_DEV_AUTO_SESSION requires a localhost'):
                _app(monkeypatch, origin=origin, auto='true', mock_mode=mock_mode, secure='true')
    # Without the flag a cluster origin starts normally.
    assert _app(monkeypatch, origin='https://ingester.apps.cluster.example', auto=None, mock_mode=False, secure='true')


def test_local_launch_link_gives_a_session_through_the_ui_origin(monkeypatch):
    """The other documented local path: mint a link with the BFF service token."""
    monkeypatch.setenv('SERVICE_TOKEN_CPG_BFF', 'synthetic-admin-token')
    c = _app(monkeypatch, origin='http://localhost:3003', auto=None, mock_mode=False)
    c.headers.clear()
    link = c.post('/api/v1/admin/launch-links', headers={'X-Service-Token': 'synthetic-admin-token'}).json()['url']
    assert link.startswith('/api/v1/launch/')
    assert c.get(link, follow_redirects=False).status_code == 302
    assert c.get('/api/v1/session').status_code == 200
