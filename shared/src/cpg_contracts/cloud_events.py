"""CloudEvent helper for SonataFlow async callbacks.

Services that run LLM work asynchronously use this to POST a CloudEvent
back to the SonataFlow callback URL when the work is done. The
kogitoprocrefid extension correlates the event to the waiting workflow
instance.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)

_warned_missing_origin = False


@trace_safe
def callback_origin_allowed(callback_url: str) -> bool:
    """Match an HTTP(S) callback against configured scheme, host and port.

    Request values are never captured by telemetry.
    """
    global _warned_missing_origin
    configured = os.environ.get("WORKFLOW_CALLBACK_ORIGIN", "")
    if not configured.strip():
        if not _warned_missing_origin:
            logger.warning("WORKFLOW_CALLBACK_ORIGIN not set; callbacks disabled")
            _warned_missing_origin = True
        return False
    if not isinstance(callback_url, str):
        return False

    target = None
    for index, value in enumerate([callback_url, *configured.split(",")]):
        if index:
            value = value.strip()
        try:
            # Reject parser ambiguities that requests may normalize differently.
            if not value or "\\" in value or any(
                c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value
            ):
                raise ValueError("invalid URL")
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("invalid origin")
            if url.username is not None or url.password is not None:
                raise ValueError("userinfo is not allowed")
            if index and (url.path not in {"", "/"} or url.query or url.fragment):
                raise ValueError("configured value must be an origin")
            port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
            if not 1 <= port <= 65535:
                raise ValueError("invalid port")
            origin = (url.scheme, url.hostname, port)
        except ValueError:
            if not index:
                return False
            continue
        if not index:
            target = origin
        elif origin == target:
            return True
    return False


@trace_safe
def require_callback_url(data: dict) -> str:
    """Consume the callback URL or reject before a handler schedules work."""
    callback_url = data.pop("callback_url", "")
    if not callback_origin_allowed(callback_url):
        # FastAPI stays optional for consumers of the shared contracts.
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail={"error": "callback-origin"})
    return callback_url


@trace_safe
def post_callback(
    callback_url: str,
    process_instance_id: str,
    event_type: str,
    data: dict,
    *,
    session: Any | None = None,
) -> bool:
    """POST a CloudEvent to the SonataFlow callback URL.

    Returns True if the callback was delivered successfully.
    """
    if not callback_origin_allowed(callback_url):
        logger.warning("callback refused: origin not allowed")
        return False

    data.setdefault("completed_at", datetime.now(timezone.utc).isoformat())
    cloud_event = {
        "specversion": "1.0",
        "id": str(uuid4()),
        "source": "",
        "type": event_type,
        "kogitoprocrefid": process_instance_id,
        "datacontenttype": "application/json",
        "data": data,
    }
    client = session if session is not None else requests
    backoff = (2, 10)
    for attempt in range(3):
        try:
            response = client.post(
                callback_url,
                json=cloud_event,
                headers={"Content-Type": "application/cloudevents+json"},
                timeout=(30, 60),
                allow_redirects=False,
            )
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                logger.warning("callback refused: redirect")
                return False
            logger.info("Callback delivered: type=%s instance=%s", event_type, process_instance_id)
            return True
        except (requests.ConnectionError, requests.Timeout):
            logger.warning("Callback attempt failed: type=%s instance=%s", event_type, process_instance_id)
            if attempt == 2:
                return False
            time.sleep(backoff[attempt])
        except Exception:
            logger.error("Callback failed: type=%s instance=%s", event_type, process_instance_id)
            return False
    return False
