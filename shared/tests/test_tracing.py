"""Exercise actual MLflow serialization, including exception events and status."""
import asyncio
import json

import pytest
from cpg_contracts import tracing


def test_shapes_do_not_include_values_or_keys():
    secret = 'SENTINEL-PATIENT-DATA'
    assert secret not in json.dumps(tracing.shape_summary({secret: secret, 'status': secret}))
    assert tracing.shape_summary({'resourceType': 'Bundle', 'entry': [secret]}) == {'type': 'Bundle', 'keys': 2, 'entries': 1}
    with pytest.raises(ValueError):
        tracing.trace_safe(ids=('patient_demographics',))


@pytest.fixture
def trace_backend(tmp_path):
    mlflow = pytest.importorskip('mlflow')
    old_uri = mlflow.get_tracking_uri()
    from mlflow.tracking.fluent import _get_experiment_id
    old_experiment_id = _get_experiment_id()
    mlflow.set_tracking_uri('sqlite:///' + str(tmp_path / 'traces.db'))
    experiment = mlflow.set_experiment('safe-tracing')
    yield mlflow, experiment.experiment_id
    mlflow.flush_trace_async_logging()
    mlflow.set_tracking_uri(old_uri)
    mlflow.set_experiment(experiment_id=old_experiment_id)


def test_sync_async_errors_and_identifiers(trace_backend):
    mlflow, experiment_id = trace_backend
    sentinel = 'SENTINEL-PATIENT-DATA'

    @tracing.trace_safe(ids=('run_id',))
    def sync(data, run_id, fail=False):
        if fail:
            raise ValueError(sentinel)
        return {sentinel: sentinel, 'status': 'completed'}

    @tracing.trace_safe
    async def async_func(payload, fail=False):
        if fail:
            raise RuntimeError(sentinel)
        return payload

    sync({sentinel: sentinel}, 'safe-run-123')
    with pytest.raises(ValueError, match=sentinel):
        sync(sentinel, 'safe-run-123', True)
    asyncio.run(async_func(sentinel))
    with pytest.raises(RuntimeError, match=sentinel):
        asyncio.run(async_func(sentinel, True))
    mlflow.flush_trace_async_logging()
    traces = mlflow.search_traces(experiment_ids=[experiment_id], return_type='list')
    assert len(traces) == 4
    serialized = json.dumps([t.to_dict() for t in traces], default=str)
    assert sentinel not in serialized
    assert 'safe-run-123' in serialized
    assert '<redacted>' in serialized
    assert 'ValueError' in serialized


def test_optional_mlflow(monkeypatch):
    monkeypatch.setattr(tracing, 'mlflow', None)
    @tracing.trace_safe
    def target(data):
        return data
    assert target('unchanged') == 'unchanged'


def test_processor_recursive_redaction_and_truncation():
    class Span:
        inputs = {'nested': {'ips_bundle': 'sentinel', 'safe': 'x' * 2100}, 'access_token': 'sentinel'}
        outputs = {'state': 'sentinel', 'count': 1}
        attributes = {'payload': 'sentinel', 'label': 'x' * 2100}
        def set_inputs(self, value): self.inputs = value
        def set_outputs(self, value): self.outputs = value
        def set_attribute(self, key, value): self.attributes[key] = value
    span = Span()
    tracing._phi_span_processor(span)
    assert 'sentinel' not in json.dumps([span.inputs, span.outputs, span.attributes])
    assert len(span.inputs['nested']['safe']) == 2000
    assert len(span.attributes['label']) == 2000
