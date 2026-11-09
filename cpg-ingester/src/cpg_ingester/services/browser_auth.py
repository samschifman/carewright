"""Operator browser sessions and single-use launch credentials (one BFF worker)."""
import asyncio
import os

from cpg_contracts.service_auth import require_service_token
from cpg_contracts.tracing import trace_safe
from cpg_contracts.web_session import (
    MemorySessionStore,
    ObjectStoreSessionStore,
    OneTimeStore,
    SessionManager,
)
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse


def install_browser_auth(app, store, *, mock_mode=False):
    signing_key = os.getenv('CPG_SIGNING_KEY', '')
    sessions = ObjectStoreSessionStore(store, signing_key=signing_key) if store is not None else MemorySessionStore()
    manager = SessionManager(sessions, cookie_name='cpg_session',
        ttl_hours=float(os.getenv('CPG_SESSION_TTL_HOURS', '8')),
        secure=os.getenv('SESSION_COOKIE_SECURE', 'true').lower() != 'false',
        public_origin=os.getenv('CPG_PUBLIC_ORIGIN', ''))
    links = OneTimeStore(store, prefix='launch-links/', signing_key=signing_key)
    app.state.sessions = manager
    app.state.launch_links = links

    @app.middleware('http')
    async def protect_browser(request: Request, call_next):
        path = request.url.path
        if request.method != 'OPTIONS' and (path.startswith('/api/v1/runs') or path == '/api/v1/upload'):
            try:
                await manager.require(mutating=request.method not in {'GET','HEAD','OPTIONS'})(request)
                if request.state.session.kind != 'operator':
                    raise HTTPException(403, detail={'error':'operator-session-required'})
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={'detail':exc.detail})
        response = await call_next(request)
        response.headers['Referrer-Policy'] = 'no-referrer'
        if path.startswith('/api/v1/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/api/v1/admin/launch-links', dependencies=[Depends(require_service_token('SERVICE_TOKEN_CPG_BFF'))])
    @trace_safe
    def mint_link():
        try:
            token = links.issue({'kind':'operator'}, 600)
        except Exception:
            raise manager.unavailable() from None
        return {'url':'/api/v1/launch/'+token, 'expiresIn':600}

    @app.get('/api/v1/launch/{token}')
    @trace_safe
    def launch(token: str):
        try:
            payload = links.consume(token)
            if not payload or payload.get('kind') != 'operator':
                raise HTTPException(403, detail={'error':'launch-invalid'})
            _, raw = manager.create('operator', 'operator')
        except HTTPException:
            raise
        except Exception:
            raise manager.unavailable() from None
        response = RedirectResponse('/', status_code=302, headers={'Referrer-Policy':'no-referrer','Cache-Control':'no-store'})
        manager.set_cookie(response, raw)
        return response

    @app.get('/api/v1/session')
    @trace_safe
    def get_session(request: Request):
        raw = None
        try:
            session = manager.load(request)
            if session is None and mock_mode and os.getenv('CPG_DEV_AUTO_SESSION','false').lower() == 'true':
                import logging
                logging.getLogger(__name__).warning('Development operator auto-session enabled')
                session, raw = manager.create('operator','operator')
        except Exception:
            raise manager.unavailable() from None
        if session is None:
            raise HTTPException(401, detail={'error':'session-required'})
        response = JSONResponse({'kind':session.kind,'csrfToken':session.csrf_token}, headers={'Cache-Control':'no-store'})
        if raw:
            manager.set_cookie(response, raw)
        return response

    @app.post('/api/v1/session/logout')
    @trace_safe
    async def logout(request: Request):
        session = await manager.require(mutating=True)(request)
        try:
            manager.store.delete(session.id_hash)
        except Exception:
            raise manager.unavailable() from None
        response = JSONResponse({'status':'signed-out'})
        manager.clear_cookie(response)
        return response

    async def sweep():
        while True:
            try:
                sessions.sweep()
                links.sweep()
            except Exception:
                manager.unavailable()
            await asyncio.sleep(3600)

    @app.on_event('startup')
    async def start_sweep():
        app.state.session_sweep = asyncio.create_task(sweep())

    @app.on_event('shutdown')
    async def stop_sweep():
        task = getattr(app.state,'session_sweep',None)
        if task:
            task.cancel()
