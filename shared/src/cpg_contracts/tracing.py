"""Shape-only telemetry for code that handles patient data.

No values are captured implicitly, including exception messages or dictionary
keys. IDs require both an explicit opt-in and an approved parameter name.
MLflow remains an optional dependency of the shared contracts.
"""
from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

try:
    import mlflow
except ImportError:
    mlflow = None

logger = logging.getLogger(__name__)
PHI_PARAM_NAMES = frozenset({
    "bundle", "ips_bundle", "fhir_bundle", "state", "job", "brief", "brief_data",
    "prior_brief", "known_values", "phi_known_values", "patient_demographics",
    "condensed_ips", "inventory", "payload", "request", "data", "inputs", "index",
    "activity", "activities", "conflict", "conflicts", "raw", "answer", "question",
    "context", "values", "user", "messages", "prompt", "content", "xml",
})
SAFE_ID_NAMES = frozenset({"run_id", "careplan_id", "job_id", "model_id", "cpg_id", "template_id"})
_SAFE_STATUSES = frozenset({"ok", "error", "success", "failed", "pending", "completed", "approved", "rejected"})


def shape_summary(value: Any) -> dict:
    """Describe structure without traversing patient values or arbitrary keys."""
    if isinstance(value, dict):
        result = {"type": "dict", "keys": len(value)}
        if value.get("resourceType") == "Bundle" and isinstance(value.get("entry"), list):
            result.update(type="Bundle", entries=len(value["entry"]))
        status = value.get("status")
        if isinstance(status, str) and status in _SAFE_STATUSES:
            result["status"] = status
        return result
    if isinstance(value, (list, tuple, set, frozenset, str, bytes)):
        return {"type": type(value).__name__, "length": len(value)}
    return {"type": type(value).__name__}


def trace_safe(func=None, *, name=None, ids=(), span_type="UNKNOWN"):
    """Trace timing and shapes, re-raising errors only after closing the span.

    MLflow's context manager records escaping exception text automatically.
    Catching inside the context is essential, even when inputs are redacted.
    """
    if isinstance(func, str):
        name, func = func, None
    invalid = set(ids) - SAFE_ID_NAMES
    if invalid:
        raise ValueError("trace_safe ids must be approved identifier parameters")

    def decorate(target: Callable):
        signature = inspect.signature(target)

        def begin(args, kwargs):
            bound = signature.bind_partial(*args, **kwargs)
            span = mlflow.start_span(name=name or target.__qualname__, span_type=span_type)
            return span, bound.arguments

        def inputs(span, arguments):
            span.set_inputs({key: shape_summary(value) for key, value in arguments.items() if key not in {"self", "cls"}})
            for key in ids:
                value = arguments.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    span.set_attribute(key, str(value)[:200])

        def failure(span, exc):
            from mlflow.entities import SpanEvent, SpanStatus, SpanStatusCode
            span.add_event(SpanEvent(name="exception", attributes={
                "exception.type": type(exc).__name__, "exception.message": "<redacted>",
            }))
            span.set_status(SpanStatus(SpanStatusCode.ERROR, description="<redacted>"))

        if inspect.iscoroutinefunction(target):
            @functools.wraps(target)
            async def wrapped(*args, **kwargs):
                if mlflow is None:
                    return await target(*args, **kwargs)
                context, arguments = begin(args, kwargs)
                error = None
                with context as span:
                    inputs(span, arguments)
                    try:
                        result = await target(*args, **kwargs)
                    except BaseException as exc:
                        error = (exc, exc.__traceback__)
                        failure(span, exc)
                    else:
                        span.set_outputs(shape_summary(result))
                if error:
                    raise error[0].with_traceback(error[1])
                return result
        else:
            @functools.wraps(target)
            def wrapped(*args, **kwargs):
                if mlflow is None:
                    return target(*args, **kwargs)
                context, arguments = begin(args, kwargs)
                error = None
                with context as span:
                    inputs(span, arguments)
                    try:
                        result = target(*args, **kwargs)
                    except BaseException as exc:
                        error = (exc, exc.__traceback__)
                        failure(span, exc)
                    else:
                        span.set_outputs(shape_summary(result))
                if error:
                    raise error[0].with_traceback(error[1])
                return result
        wrapped.__trace_safe__ = True
        wrapped.__mlflow_traced__ = mlflow is not None
        return wrapped
    return decorate(func) if func is not None else decorate


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()
                if isinstance(key, str) and key.lower() not in PHI_PARAM_NAMES
                and not any(part in key.lower() for part in ("password", "secret", "token", "authorization"))}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return value[:2000]
    return value


def _phi_span_processor(span):
    """Backstop for accidental raw instrumentation; the decorator is primary."""
    for field in ("inputs", "outputs"):
        value = getattr(span, field, None)
        if value is not None:
            getattr(span, "set_" + field)(_redact(value))
    for key, value in span.attributes.items():
        if key in {"mlflow.spanInputs", "mlflow.spanOutputs"}:
            continue
        if key.lower() in PHI_PARAM_NAMES or any(part in key.lower() for part in ("password", "secret", "token", "authorization")):
            span.set_attribute(key, "<redacted>")
        elif isinstance(value, str) and len(value) > 2000:
            span.set_attribute(key, value[:2000])


def install_phi_span_processor():
    """Install once per process, retaining processors registered by the caller."""
    if mlflow is None:
        return
    configure = getattr(getattr(mlflow, "tracing", None), "configure", None)
    if configure is None:
        logger.warning("MLflow span processors unavailable; shape-only tracing remains enabled")
        return
    # Store the guard on the module so reloads of this helper remain idempotent.
    if getattr(mlflow, "_cpg_phi_processor_installed", False):
        return
    from mlflow.tracing.config import get_config
    processors = list(get_config().span_processors)
    configure(span_processors=[*processors, _phi_span_processor])
    mlflow._cpg_phi_processor_installed = True
