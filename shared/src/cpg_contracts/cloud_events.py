"""CloudEvent helper for SonataFlow async callbacks.

Services that run LLM work asynchronously use this to POST a CloudEvent
back to the SonataFlow callback URL when the work is done. The
kogitoprocrefid extension correlates the event to the waiting workflow
instance.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import requests

logger = logging.getLogger(__name__)


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
            )
            response.raise_for_status()
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
