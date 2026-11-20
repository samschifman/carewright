"""Upstream exception payloads must not enter workflow data or application logs."""
import importlib
import json
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('service,background,failing', [
    ('patient_data','_run_scan_background','resolve_ref'),
    ('fhir_generation','_run_generate_background','resolve_ref'),
    ('llm_reasoning','_run_execute_dmn_background','resolve_ref'),
    ('llm_reasoning','_run_resolve_background','_resolve_guidelines'),
    ('llm_reasoning','_run_retrieve_background','_retrieve_recommendations'),
    ('llm_reasoning','_run_compose_background','resolve_ref'),
    ('llm_reasoning','_run_review_fhir_background','resolve_ref'),
])
def test_background_errors_are_safe(monkeypatch,caplog,service,background,failing):
    module=importlib.import_module('acp_writer.services.'+service)
    sentinel='SYNTHETIC-PHI-EXCEPTION-83c19'
    monkeypatch.setattr(module,failing,Mock(side_effect=RuntimeError(sentinel)))
    callback=Mock()
    monkeypatch.setattr(module,'post_callback',callback)
    getattr(module,background)({},'http://workflow.test/wait','synthetic-instance')
    result=callback.call_args.args[-1]
    assert result['error']=='operation-failed:RuntimeError'
    assert sentinel not in json.dumps(result)
    assert sentinel not in caplog.text
