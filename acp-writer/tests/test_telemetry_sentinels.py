"""Patient values must stay out of real pipeline traces and DEBUG logs."""
import json
import logging
from types import SimpleNamespace

import mlflow
import pytest
from acp_writer import pipeline
from acp_writer.services.automation_registry import AutomationRegistry
from cpg_contracts.tracing import trace_safe

from .test_revision_flow import ScriptedLLM

SENTINELS = ('ZEPHYRQUILL-NAME', 'ZEPHYRQUILL-NOTE', 'ZEPHYRQUILL-ACTIVITY')


@pytest.mark.parametrize('fail', [False, True])
def test_pipeline_does_not_export_patient_values(tmp_path, monkeypatch, caplog, fail):
    old_uri = mlflow.get_tracking_uri()
    from mlflow.tracking.fluent import _get_experiment_id
    old_experiment_id = _get_experiment_id()
    mlflow.set_tracking_uri('sqlite:///' + str(tmp_path / 'telemetry.db'))
    experiment = mlflow.set_experiment('pipeline-sentinels')
    brief = {
        'patient_reference': 'Patient/synthetic', 'applicable_cpgs': ['synthetic-cpg'],
        'goals': [{'description': 'Synthetic goal', 'source_cpg': 'synthetic-cpg'}],
        'activities': [{'type': 'lifestyle', 'description': SENTINELS[2], 'source_cpg': 'synthetic-cpg'}],
        'review_status': 'pending',
    }
    composer = ScriptedLLM(json.dumps(brief))
    approve = ScriptedLLM('{"verdict":"APPROVE","issues":[]}')
    conflicts = ScriptedLLM('{"conflicts":[]}')
    monkeypatch.setattr('acp_writer.nodes.plan_composer.get_llm', lambda *args: composer)
    monkeypatch.setattr('acp_writer.nodes.brief_reviewer.get_llm', lambda *args: approve)
    monkeypatch.setattr('acp_writer.nodes.fhir_semantic_reviewer.get_llm', lambda *args: approve)
    monkeypatch.setattr('acp_writer.nodes.conflict_analyst.get_llm', lambda *args: conflicts)
    if fail:
        @trace_safe
        def failed_node(state):
            raise ValueError(SENTINELS[1])
        monkeypatch.setattr(pipeline, 'fhir_bundle_generator', failed_node)
    metadata = SimpleNamespace(
        cpg_id='synthetic-cpg', title='Synthetic guideline', scope=[],
        model_dump=lambda **kwargs: {'cpg_id': 'synthetic-cpg'},
    )
    store = SimpleNamespace(list_all=lambda: [metadata])
    vector = SimpleNamespace(count=lambda: 0)
    monkeypatch.setattr('acp_writer.nodes.guideline_resolver._condition_matches_scope', lambda *args: True)
    monkeypatch.setattr('acp_writer.nodes.guideline_resolver.DECISION_ENGINE_URL', '')
    bundle = {'resourceType': 'Bundle', 'entry': [
        {'resource': {'resourceType': 'Patient', 'id': 'synthetic', 'name': [{'text': SENTINELS[0]}]}},
        {'resource': {'resourceType': 'Condition', 'id': 'synthetic-condition', 'code': {'text': 'Synthetic condition', 'coding': [{'system': 'urn:synthetic', 'code': 'synthetic'}]}}},
        {'resource': {'resourceType': 'Observation', 'id': 'synthetic-note', 'note': [{'text': SENTINELS[1]}]}},
    ]}
    graph = pipeline.build_pipeline(
        automation_registry=AutomationRegistry(), automation_validator=lambda *a, **k: {},
        guidelines_store=store, vector_store=vector, decision_models_provider=lambda: [],
    ).compile()

    @trace_safe(ids=('run_id',))
    def invoke(run_id):
        return graph.invoke({'ips_bundle': bundle, 'careplan_id': run_id})

    try:
        with caplog.at_level(logging.DEBUG):
            if fail:
                with pytest.raises(ValueError, match=SENTINELS[1]):
                    invoke('sentinel-run-123')
            else:
                result = invoke('sentinel-run-123')
                assert SENTINELS[2] in json.dumps(result['fhir_bundle'])
        assert composer.calls, 'The actual composer must consume the sentinel bundle'
        mlflow.flush_trace_async_logging()
        traces = mlflow.search_traces(experiment_ids=[experiment.experiment_id], return_type='list')
        serialized = json.dumps([trace.to_dict() for trace in traces], default=str)
        assert traces
        assert 'sentinel-run-123' in serialized
        for value in SENTINELS:
            assert value not in serialized
            assert value not in caplog.text
    finally:
        mlflow.flush_trace_async_logging()
        mlflow.set_tracking_uri(old_uri)
        mlflow.set_experiment(experiment_id=old_experiment_id)
