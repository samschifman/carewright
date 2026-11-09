"""Per-receiving-service credentials for calls inside the deployment boundary."""
from __future__ import annotations

import logging
import os
import secrets

from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


def require_service_token(env_name: str, *, workflow_route: bool = False):
    """Build a dependency; read the expected credential on every request."""
    from fastapi import HTTPException, Request

    # Keep Request resolvable by FastAPI even when the shared package is used
    # without its optional web dependencies elsewhere.
    @trace_safe(name='require_service_token')
    async def dependency(request: Request):
        given = request.headers.get('X-Service-Token')
        if (workflow_route and given is None
                and os.environ.get('WORKFLOW_CALLER_UNAUTHENTICATED', '').lower() == 'true'):
            logger.info('Unauthenticated workflow caller admitted on %s', getattr(request.scope.get('route'), 'path', '?'))
            return
        expected = os.environ.get(env_name, '')
        if not expected:
            raise HTTPException(503, detail={'error': 'service-credential-unconfigured'})
        # compare_digest on bytes also handles arbitrary non-ASCII HTTP values.
        if given is None or not secrets.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(401, detail={'error': 'service-credential'})
    # Future annotations plus a local optional Request import need an explicit
    # runtime annotation before FastAPI builds its dependency graph.
    dependency.__wrapped__.__annotations__['request'] = Request
    dependency.__annotations__['request'] = Request
    return dependency


@trace_safe
def service_token_headers(env_name: str) -> dict[str, str]:
    value = os.environ.get(env_name, '')
    if not value:
        logger.warning('Service credential not configured for %s', env_name)
        return {}
    return {'X-Service-Token': value}
