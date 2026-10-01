import json

import pytest
from pydantic import ValidationError


def test_contracts_and_split_validation():
    from conftest import config_dict
    from prompt_eval.models import RunConfig

    data = config_dict()
    cfg = RunConfig.model_validate(data)
    assert cfg.schema_version == "1.0.0"
    assert cfg.repetitions == 2
    for update in (
        {"repetitions": 0},
        {"cases": data["cases"] * 2},
        {"stages": data["stages"] * 2},
        {"unexpected": True},
    ):
        with pytest.raises(ValidationError):
            RunConfig.model_validate(data | update)
    data["dataset"]["digest"] = "main"
    with pytest.raises(ValidationError):
        RunConfig.model_validate(data)


def test_metrics_must_be_finite_and_errors_are_typed():
    from prompt_eval.models import CaseResult, OperationalError

    with pytest.raises(ValidationError):
        CaseResult(case_id="one", metrics={"f1": float("nan")})
    with pytest.raises(ValidationError):
        OperationalError(kind="pass", message="oops")
    with pytest.raises(ValidationError):
        CaseResult(case_id="one", usable=False)


def test_digests_are_order_independent_and_reject_nan():
    from prompt_eval.security import digest

    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})
    with pytest.raises(ValueError):
        digest({"x": float("nan")})


def test_redaction_covers_headers_urls_exceptions_and_known_values():
    from prompt_eval.security import Redactor

    r = Redactor(secrets=["sensitive-value"])
    payload = {
        "headers": {"X-Weird": "private", "Authorization": "Bearer abc"},
        "nested": [{"api_key": "secret"}],
        "error": "failed https://user:pass@host/a?api_key=abc&x=1 Bearer abc sensitive-value",
        "plain": "clinical text remains",
    }
    clean = r.clean(payload)
    serialized = json.dumps(clean)
    for secret in ("private", "sensitive-value", "user:pass", "Bearer abc", "api_key=abc"):
        assert secret not in serialized
    assert clean["plain"] == payload["plain"]
    assert clean["nested"][0]["api_key"] == "[REDACTED]"


def test_safe_trace_never_records_arguments_return_values_or_exception_text(monkeypatch):
    import mlflow
    from prompt_eval.security import traced

    spans = []

    def fake_trace(**kwargs):
        def decorate(fn):
            def wrapped(*args, **kw):
                output = fn(*args, **kw)
                spans.append((args, kw, output))
                return output

            return wrapped

        return decorate

    monkeypatch.setattr(mlflow, "trace", fake_trace)
    from prompt_eval.security import _trace_destination

    monkeypatch.setattr(mlflow, "get_current_active_span", lambda: None)
    token = _trace_destination.set("test")

    @traced
    def work(secret):
        if secret == "throw":
            raise ValueError("credential")
        return secret

    assert work("credential") == "credential"
    with pytest.raises(ValueError):
        work("throw")
    _trace_destination.reset(token)
    assert spans == [((), {}, None), ((), {}, None)]


def test_tracing_is_scoped_to_explicit_store_and_restores_tracking_uri(monkeypatch):
    from types import SimpleNamespace

    import mlflow
    from prompt_eval.security import traced, tracing_session

    called = []
    tracking = ["http://original"]
    monkeypatch.setattr(mlflow, "get_tracking_uri", lambda: tracking[0])
    monkeypatch.setattr(mlflow, "set_tracking_uri", lambda uri: tracking.__setitem__(0, uri))
    monkeypatch.setattr(mlflow, "flush_trace_async_logging", lambda: None)
    monkeypatch.delenv("MLFLOW_TRACE_SAMPLING_RATIO", raising=False)

    def fake_trace(**kwargs):
        called.append(kwargs)
        return lambda fn: fn

    monkeypatch.setattr(mlflow, "trace", fake_trace)

    @traced
    def work():
        return 1

    assert work() == 1
    assert called == []
    with tracing_session(
        SimpleNamespace(experiment_id="experiment-123", tracking_uri="https://approved")
    ):
        assert work() == 1
        assert tracking[0] == "https://approved"
    assert called[0]["trace_destination"].experiment_id == "experiment-123"
    assert tracking[0] == "http://original"


def test_nested_traces_inherit_destination_without_setting_root_only_parameter(monkeypatch):
    import mlflow
    from prompt_eval.security import _trace_destination, traced

    options = []
    monkeypatch.setattr(mlflow, "get_current_active_span", lambda: object())

    def fake_trace(**kwargs):
        options.append(kwargs)
        return lambda fn: fn

    monkeypatch.setattr(mlflow, "trace", fake_trace)
    token = _trace_destination.set("selected-experiment")
    try:

        @traced
        def work():
            return 1

        assert work() == 1
    finally:
        _trace_destination.reset(token)
    assert "trace_destination" not in options[0]
