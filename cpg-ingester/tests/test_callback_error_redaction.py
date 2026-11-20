"""Error callbacks retain stable diagnostics without upstream payloads."""
import importlib
import json
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('service,background,failing', [
    ('llm_analysis','_run_analyze_background','_do_analyze'),
    ('llm_analysis','_run_generate_background','_do_generate'),
    ('ingestion','_run_parse_background','_do_parse_bytes'),
])
def test_background_errors_are_safe(monkeypatch,caplog,service,background,failing):
    module=importlib.import_module('cpg_ingester.services.'+service)
    sentinel='SYNTHETIC-PHI-EXCEPTION-83c19'
    monkeypatch.setattr(module,failing,Mock(side_effect=RuntimeError(sentinel)))
    callback=Mock()
    monkeypatch.setattr(module,'post_callback',callback)
    if service=='ingestion':
        monkeypatch.setattr(module,'_store',Mock())
        getattr(module,background)('synthetic-pdf','http://workflow.test/wait','synthetic-instance')
    else:
        getattr(module,background)({},'http://workflow.test/wait','synthetic-instance')
    result=callback.call_args.args[-1]
    assert result['error']=='operation-failed:RuntimeError'
    assert sentinel not in json.dumps(result)
    assert sentinel not in caplog.text
