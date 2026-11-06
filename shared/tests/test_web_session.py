import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest
from cpg_contracts.web_session import (
    MemorySessionStore,
    ObjectStoreSessionStore,
    OneTimeStore,
    SessionManager,
    new_pkce,
    token_hash,
)
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient


class FakeStore:
    def __init__(self): self.records = {}
    def get(self, key): return json.loads(json.dumps(self.records[key]))
    def put(self, key, value): self.records[key] = json.loads(json.dumps(value))
    def delete(self, key): self.records.pop(key, None)
    def list_keys(self, prefix): return [key for key in self.records if key.startswith(prefix)]


@pytest.mark.parametrize('persistent', [False, True])
def test_round_trip_expiry_logout_and_raw_id_not_persisted(persistent, monkeypatch):
    objects = FakeStore()
    store = ObjectStoreSessionStore(objects, signing_key='test-signing-key') if persistent else MemorySessionStore()
    manager = SessionManager(store, cookie_name='session')
    session, raw = manager.create('patient', 'synthetic', ['run-1'])
    request = Request({'type': 'http', 'headers': [(b'cookie', ('session=' + raw).encode())]})
    assert manager.load(request) == session
    if persistent:
        assert raw not in json.dumps(objects.records)
        restarted = SessionManager(ObjectStoreSessionStore(objects, signing_key='test-signing-key'), cookie_name='session')
        assert restarted.load(request) == session
    manager.bind_run(session, 'run-2')
    manager.bind_run(session, 'run-2')
    assert manager.load(request).run_ids == ['run-1', 'run-2']
    monkeypatch.setattr(time, 'time', lambda: session.expires_at + 1)
    assert manager.load(request) is None
    assert store.get(session.id_hash) is None


def test_cookie_attributes_and_csrf_and_outage(monkeypatch):
    store = MemorySessionStore()
    manager = SessionManager(store, cookie_name='session', public_origin='https://writer.example')
    session, raw = manager.create('patient', 'synthetic')
    response = Response()
    manager.set_cookie(response, raw)
    cookie = response.headers['set-cookie']
    for attribute in ('HttpOnly', 'Secure', 'SameSite=lax', 'Path=/', 'Max-Age=28800'):
        assert attribute in cookie
    manager.clear_cookie(response)
    assert any('Max-Age=0' in v for k, v in response.headers.items() if k == 'set-cookie')
    app = FastAPI()
    @app.get('/read', dependencies=[Depends(manager.require())])
    @app.post('/write', dependencies=[Depends(manager.require(mutating=True))])
    def endpoint(): return {'ok': True}
    client = TestClient(app)
    assert client.get('/read').status_code == 401
    client.cookies.set('session', 'unknown')
    assert client.get('/read').status_code == 401
    client.cookies.set('session', raw)
    assert client.get('/read').status_code == 200
    assert client.post('/write').status_code == 403
    assert client.post('/write', headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    headers = {'X-CSRF-Token': session.csrf_token, 'Origin': 'https://writer.example'}
    assert client.post('/write', headers=headers).status_code == 200
    assert client.post('/write', headers={**headers, 'Origin': 'https://other.example'}).status_code == 403
    monkeypatch.setattr(store, 'get', lambda key: (_ for _ in ()).throw(ConnectionError('sensitive upstream text')))
    assert client.get('/read').status_code == 503
    assert client.get('/read').json() == {'detail': {'error': 'session-store-unavailable'}}


@pytest.mark.parametrize('persistent', [False, True])
def test_one_time_tokens_single_use_expiry_and_concurrency(persistent, monkeypatch):
    objects = FakeStore()
    store = OneTimeStore(objects if persistent else None, signing_key='test-signing-key')
    token = store.issue({'verifier': 'synthetic'}, ttl_seconds=600)
    assert token not in json.dumps(objects.records)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(store.consume, [token] * 4))
    assert results.count({'verifier': 'synthetic'}) == 1
    assert results.count(None) == 3
    assert store.consume('unknown') is None
    expired = store.issue({'secret': 'synthetic'}, ttl_seconds=-1)
    assert store.consume(expired) is None


def test_persisted_records_require_integrity():
    objects = FakeStore()
    store = ObjectStoreSessionStore(objects, signing_key='test-signing-key', cache_ttl=0)
    manager = SessionManager(store, cookie_name='session')
    session, raw = manager.create('patient', 'synthetic')
    key = 'sessions/' + session.id_hash + '.json'
    objects.records[key]['subject'] = 'another-patient'
    assert store.get(session.id_hash) is None
    forged = dict(asdict(session), id_hash=token_hash('chosen-cookie'))
    objects.put('sessions/' + forged['id_hash'] + '.json', forged)
    assert store.get(forged['id_hash']) is None
    pending = OneTimeStore(objects, signing_key='test-signing-key')
    token = pending.issue({'kind': 'operator'})
    objects.records['pending/' + token_hash(token) + '.json']['payload']['kind'] = 'forged'
    assert pending.consume(token) is None
    with pytest.raises(ValueError):
        SessionManager(ObjectStoreSessionStore(objects, signing_key=''), cookie_name='session').create('patient', 'synthetic')


def test_pkce_and_localhost_cookie_policy():
    verifier, challenge = new_pkce()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    with pytest.raises(ValueError):
        SessionManager(MemorySessionStore(), cookie_name='session', secure=False, public_origin='http://remote.example')
    manager = SessionManager(MemorySessionStore(), cookie_name='session', secure=False, public_origin='http://localhost:5173')
    response = Response()
    manager.set_cookie(response, 'synthetic')
    assert 'Secure' not in response.headers['set-cookie']


def test_sweep_and_lru_cap():
    objects = FakeStore()
    store = ObjectStoreSessionStore(objects, signing_key='test-signing-key', cache_limit=2)
    manager = SessionManager(store, cookie_name='session', ttl_hours=-1)
    for _ in range(3): manager.create('patient', 'synthetic')
    assert len(store._cache) == 2
    store.sweep()
    assert not objects.records
    pending = OneTimeStore(objects, signing_key='test-signing-key')
    pending.issue({}, ttl_seconds=-1)
    pending.sweep()
    assert not objects.records


def test_keyed_locks_serialize_waiters_and_release_on_failure_and_cancel():
    import asyncio

    from cpg_contracts.web_session import AsyncKeyedLocks
    async def run():
        locks = AsyncKeyedLocks()
        active = 0
        peak = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        async def holder():
            nonlocal active, peak
            async with locks.hold('run'):
                active += 1
                peak = max(peak, active)
                entered.set()
                await release.wait()
                active -= 1
        first = asyncio.create_task(holder())
        await entered.wait()
        second = asyncio.create_task(holder())
        cancelled = asyncio.create_task(holder())
        await asyncio.sleep(0)
        cancelled.cancel()
        try: await cancelled
        except asyncio.CancelledError: pass
        assert locks._entries['run'][1] == 2
        release.set()
        await asyncio.gather(first, second)
        assert peak == 1
        assert not locks._entries
        with pytest.raises(ValueError):
            async with locks.hold('failure'):
                raise ValueError('synthetic')
        assert not locks._entries
    asyncio.run(run())
