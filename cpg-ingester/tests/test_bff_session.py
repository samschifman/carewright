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
