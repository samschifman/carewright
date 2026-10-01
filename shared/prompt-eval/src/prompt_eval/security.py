"""Canonical JSON and redaction at every persistence boundary.

Tracing wraps a zero-argument, zero-output span. Raw arguments, results and
exception text never enter MLflow's automatic span serialization.
"""

import hashlib
import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

import mlflow
from mlflow.entities import MlflowExperimentLocation, SpanStatus, SpanStatusCode
from pydantic import BaseModel

_trace_destination = ContextVar("prompt_eval_trace_destination", default=None)


@contextmanager
def tracing_session(store):
    """A CLI/coordinator owns the process while this session is active.

    Trace only to an explicitly selected MLflow experiment. Local-only development
    emits no remote telemetry. Flush before restoring MLflow's process-wide URI.
    """
    if not hasattr(store, "experiment_id") or os.environ.get("MLFLOW_TRACE_SAMPLING_RATIO") == "0":
        yield
        return
    previous = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(store.tracking_uri)
    token = _trace_destination.set(MlflowExperimentLocation(experiment_id=store.experiment_id))
    try:
        yield
    finally:
        try:
            mlflow.flush_trace_async_logging()
        finally:
            _trace_destination.reset(token)
            mlflow.set_tracking_uri(previous)


def traced(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        destination = _trace_destination.get()
        if destination is None:
            return func(*args, **kwargs)
        result = []
        errors = []

        options = (
            {"trace_destination": destination} if mlflow.get_current_active_span() is None else {}
        )

        @mlflow.trace(name=f"prompt_eval.{func.__qualname__}", **options)
        def span():
            try:
                result.append(func(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 -- rethrow outside trace to prevent credential leakage
                errors.append(exc)
                active = mlflow.get_current_active_span()
                if active is not None:
                    active.set_attribute("error.type", type(exc).__name__)
                    active.set_status(SpanStatus(SpanStatusCode.ERROR))

        span()
        if errors:
            raise errors[0]
        return result[0]

    return wrapper


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


_SECRET = re.compile(
    r"authorization|cookie|password|passwd|secret|(?<![A-Za-z])token(?![A-Za-z])|api[-_]?key|credential",
    re.IGNORECASE,
)


class Redactor:
    def __init__(self, secrets=()):
        env = [v for k, v in os.environ.items() if _SECRET.search(k) and v]
        self.secrets = sorted({v for v in (*secrets, *env) if v}, key=len, reverse=True)

    def text(self, value: str) -> str:
        # A raw string has no reliable value boundary (repr, malformed JSON,
        # multiline headers). Discard the whole credential-bearing string.
        if re.search(
            r"[\"']?[\w-]*(?:authorization|cookie|password|passwd|secret|token|api[-_]?key|credential|headers)[\w-]*[\"']?\s*[:=]",
            value,
            re.IGNORECASE,
        ):
            return "[REDACTED credential-bearing text]"
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", value)
        value = re.sub(
            r"(?i)((?:api[-_]?key|access_token|token|password|secret|signature)=)[^\s&]+",
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(
            r"(?i)([\"']?(?:x-api-key|api[-_]?key|access_token|password|secret)[\"']?\s*[:=]\s*)[\"']?[^\"'\s,};&]+[\"']?",
            r"\1[REDACTED]",
            value,
        )
        return value

    @traced
    def clean(self, value: Any) -> Any:
        return self._clean(value)

    def _clean(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {
                self.text(str(k)): "[REDACTED]"
                if _SECRET.search(str(k))
                or str(k).lower().replace("_", "-")
                in ("headers", "request-headers", "response-headers")
                else self._clean(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            canonical_bytes(value)  # Reject non-finite numbers.
            return value
        raise TypeError("Artifacts must contain JSON-compatible data or UTF-8 text")
