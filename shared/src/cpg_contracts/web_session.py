"""Server-side browser sessions, CSRF and short-lived single-use launch tokens.

Cookies contain 256-bit random IDs; persistence contains only their SHA-256.
Object-store consumption is serialized within the BFF process. Deploy one BFF
replica/worker until a distributed atomic token store is introduced.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return verifier, challenge


@dataclass
class Session:
    id_hash: str
    kind: Literal['patient', 'operator']
    subject: str
    run_ids: list[str]
    csrf_token: str
    created_at: float
    expires_at: float
    meta: dict = field(default_factory=dict)


class SessionStore(Protocol):
    def get(self, id_hash: str) -> Session | None: ...
    def put(self, session: Session) -> None: ...
    def delete(self, id_hash: str) -> None: ...


class MemorySessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    @trace_safe
    def get(self, id_hash: str) -> Session | None:
        with self._lock:
            return self._sessions.get(id_hash)

    @trace_safe
    def put(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.id_hash] = session

    @trace_safe
    def delete(self, id_hash: str) -> None:
        with self._lock:
            self._sessions.pop(id_hash, None)

    @trace_safe
    def sweep(self) -> None:
        with self._lock:
            for key in list(self._sessions):
                if self._sessions[key].expires_at <= time.time():
                    self._sessions.pop(key)


def _missing(exc) -> bool:
    if isinstance(exc, KeyError):
        return True
    response = getattr(exc, 'response', {})
    return response.get('Error', {}).get('Code') in {'NoSuchKey', 'NotFound', '404'}


def _delete(store, key):
    if hasattr(store, 'delete'):
        store.delete(key)
    else:
        store._get_client().delete_object(Bucket=store.bucket, Key=key)


def _keys(store, prefix):
    if hasattr(store, 'list_keys'):
        yield from store.list_keys(prefix)
    else:
        paginator = store._get_client().get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=store.bucket, Prefix=prefix):
            yield from (item['Key'] for item in page.get('Contents', []))


@trace_safe
def sign_browser_record(record, signing_key):
    if not signing_key:
        raise ValueError('Persisted browser records require a signing key')
    record = dict(record)
    record.pop('signature', None)
    canonical = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
    record['signature'] = hmac.new(signing_key.encode(), canonical, hashlib.sha256).hexdigest()
    return record


@trace_safe
def verify_browser_record(record, signing_key):
    if not isinstance(record, dict) or not isinstance(record.get('signature'), str):
        return None
    expected = sign_browser_record(record, signing_key)['signature']
    if not hmac.compare_digest(record['signature'], expected):
        return None
    return {key: value for key, value in record.items() if key != 'signature'}


class ObjectStoreSessionStore:
    def __init__(self, store, prefix='sessions/', *, signing_key, cache_ttl=5, cache_limit=1000):
        self.store = store
        self.prefix = prefix
        self.signing_key = signing_key
        self.cache_ttl = cache_ttl
        self.cache_limit = cache_limit
        self._cache = OrderedDict()
        self._lock = threading.RLock()

    def _key(self, id_hash):
        # IDs always come from SHA-256; never treat a cookie as an object key.
        if len(id_hash) != 64 or any(c not in '0123456789abcdef' for c in id_hash):
            raise ValueError('Invalid session hash')
        return self.prefix + id_hash + '.json'

    @trace_safe
    def get(self, id_hash: str) -> Session | None:
        with self._lock:
            cached = self._cache.get(id_hash)
            if cached and cached[0] > time.time():
                self._cache.move_to_end(id_hash)
                return cached[1]
            self._cache.pop(id_hash, None)
            try:
                record = self.store.get(self._key(id_hash))
            except Exception as exc:
                if _missing(exc):
                    return None
                raise
            record = verify_browser_record(record, self.signing_key)
            if record is None:
                return None
            session = Session(**record)
            if session.id_hash != id_hash:
                raise ValueError('Session record mismatch')
            self._remember(session)
            return session

    def _remember(self, session):
        self._cache[session.id_hash] = (time.time() + self.cache_ttl, session)
        self._cache.move_to_end(session.id_hash)
        while len(self._cache) > self.cache_limit:
            self._cache.popitem(last=False)

    @trace_safe
    def put(self, session: Session) -> None:
        with self._lock:
            self.store.put(self._key(session.id_hash), sign_browser_record(asdict(session), self.signing_key))
            self._remember(session)

    @trace_safe
    def delete(self, id_hash: str) -> None:
        with self._lock:
            _delete(self.store, self._key(id_hash))
            self._cache.pop(id_hash, None)

    @trace_safe
    def sweep(self) -> None:
        with self._lock:
            for key in _keys(self.store, self.prefix):
                try:
                    record = self.store.get(key)
                except Exception as exc:
                    if _missing(exc):
                        continue
                    raise
                if record.get('expires_at', 0) <= time.time():
                    _delete(self.store, key)
                    self._cache.pop(record.get('id_hash'), None)


class OneTimeStore:
    """Single-process one-time records; tokens are never persisted raw.

    Multiple replicas can read the same record before deletion and permit replay.
    Deploy one replica/worker until consumption uses a distributed atomic store.
    """
    def __init__(self, store=None, prefix='pending/', *, signing_key=''):
        self.store = store
        self.prefix = prefix
        self.signing_key = signing_key
        self._records = {}
        self._lock = threading.RLock()

    @trace_safe
    def issue(self, payload: dict, ttl_seconds: int = 600) -> str:
        token = new_state()
        record = {'payload': payload, 'expires_at': time.time() + ttl_seconds}
        key = self.prefix + token_hash(token) + '.json'
        with self._lock:
            if self.store is None:
                self._records[key] = record
            else:
                self.store.put(key, sign_browser_record(record, self.signing_key))
        return token

    @trace_safe
    def consume(self, token: str) -> dict | None:
        if not token or len(token) > 512:
            return None
        key = self.prefix + token_hash(token) + '.json'
        with self._lock:
            if self.store is None:
                record = self._records.pop(key, None)
            else:
                try:
                    record = self.store.get(key)
                except Exception as exc:
                    if _missing(exc):
                        return None
                    raise
                # Delete must succeed before releasing the protected payload.
                _delete(self.store, key)
                record = verify_browser_record(record, self.signing_key)
        if not record or record['expires_at'] <= time.time():
            return None
        return record['payload']

    @trace_safe
    def sweep(self) -> None:
        with self._lock:
            if self.store is None:
                for key in list(self._records):
                    if self._records[key]['expires_at'] <= time.time():
                        self._records.pop(key)
            else:
                for key in _keys(self.store, self.prefix):
                    try:
                        record = self.store.get(key)
                    except Exception as exc:
                        if _missing(exc):
                            continue
                        raise
                    if record.get('expires_at', 0) <= time.time():
                        _delete(self.store, key)


class SessionManager:
    def __init__(self, store: SessionStore, *, cookie_name: str, ttl_hours=8, secure=True, same_site='lax', public_origin=''):
        if not secure:
            origin = urlsplit(public_origin)
            if origin.scheme != 'http' or origin.hostname not in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError('Insecure session cookies require a localhost HTTP origin')
            logger.warning('Insecure session cookies enabled for localhost development')
        self.store = store
        self.cookie_name = cookie_name
        self.ttl_seconds = int(ttl_hours * 3600)
        self.secure = secure
        self.same_site = same_site
        self.public_origin = public_origin.rstrip('/')
        self._last_error_log = 0.0

    @trace_safe
    def create(self, kind, subject, run_ids=None, meta=None) -> tuple[Session, str]:
        raw_id = new_state()
        now = time.time()
        session = Session(token_hash(raw_id), kind, subject, list(run_ids or []), new_state(), now, now + self.ttl_seconds, dict(meta or {}))
        self.store.put(session)
        return session, raw_id

    def set_cookie(self, response, raw_id, max_age=None):
        response.set_cookie(self.cookie_name, raw_id, max_age=self.ttl_seconds if max_age is None else max_age,
                            httponly=True, secure=self.secure, samesite=self.same_site, path='/')

    def clear_cookie(self, response):
        response.delete_cookie(self.cookie_name, httponly=True, secure=self.secure, samesite=self.same_site, path='/')

    @trace_safe
    def load(self, request) -> Session | None:
        raw = request.cookies.get(self.cookie_name)
        if not raw or len(raw) > 512:
            return None
        session = self.store.get(token_hash(raw))
        if session is not None and session.expires_at <= time.time():
            self.store.delete(session.id_hash)
            return None
        return session

    def unavailable(self):
        from fastapi import HTTPException
        now = time.monotonic()
        if now - self._last_error_log >= 60:
            logger.error('Session store unavailable')
            self._last_error_log = now
        return HTTPException(503, detail={'error': 'session-store-unavailable'})

    def require(self, mutating=False):
        from fastapi import HTTPException, Request

        @trace_safe(name='require_session')
        async def dependency(request: Request):
            try:
                session = self.load(request)
            except Exception:
                raise self.unavailable() from None
            if session is None:
                raise HTTPException(401, detail={'error': 'session-required'})
            if mutating:
                given = request.headers.get('X-CSRF-Token', '')
                if not given or not secrets.compare_digest(given.encode(), session.csrf_token.encode()):
                    raise HTTPException(403, detail={'error': 'csrf'})
                supplied = request.headers.get('origin') or request.headers.get('referer')
                if supplied:
                    url = urlsplit(supplied)
                    origin = f'{url.scheme}://{url.netloc}'
                    if not self.public_origin or origin != self.public_origin:
                        raise HTTPException(403, detail={'error': 'csrf'})
            request.state.session = session
            return session
        dependency.__wrapped__.__annotations__['request'] = Request
        dependency.__annotations__['request'] = Request
        return dependency

    @trace_safe
    def bind_run(self, session: Session, run_id: str):
        if run_id not in session.run_ids:
            session.run_ids.append(run_id)
            self.store.put(session)


class AsyncKeyedLocks:
    """Serialize operations per key on one event loop, retaining only active users.

    Counts include waiters so releasing a holder cannot create a second lock
    while another coroutine is waiting. Cancellation also releases its reference.
    """
    def __init__(self):
        self._entries = {}

    def hold(self, key):
        import asyncio
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def held():
            entry = self._entries.setdefault(key, [asyncio.Lock(), 0])
            entry[1] += 1
            try:
                async with entry[0]:
                    yield
            finally:
                entry[1] -= 1
                if not entry[1]:
                    del self._entries[key]
        return held()
