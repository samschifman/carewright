"""Async ingestion and analysis must validate callback destinations first."""

from importlib import import_module
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(("service", "route", "background"), [
    ("ingestion", "parse-async", "_run_parse_background"),
    ("llm_analysis", "analyze-async", "_run_analyze_background"),
    ("llm_analysis", "generate-async", "_run_generate_background"),
])
@pytest.mark.parametrize("callback", [None, "http://other.test/wait-done", "https://workflow.test:8080/wait-done"])
def test_async_callback_origin_rejected_before_work(monkeypatch, service, route, background, callback):
    monkeypatch.setenv("WORKFLOW_CALLBACK_ORIGIN", "http://workflow.test:8080")
    module = import_module(f"cpg_ingester.services.{service}")
    work = Mock()
    monkeypatch.setattr(module, background, work)
    payload = {} if callback is None else {"callback_url": callback}
    response = TestClient(module.app).post("/api/v1/" + route, json=payload)
    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "callback-origin"}}
    work.assert_not_called()


@pytest.mark.parametrize(("service", "route", "background"), [
    ("ingestion", "parse-async", "_run_parse_background"),
    ("llm_analysis", "analyze-async", "_run_analyze_background"),
    ("llm_analysis", "generate-async", "_run_generate_background"),
])
def test_valid_callback_still_schedules_work(monkeypatch, service, route, background):
    monkeypatch.setenv("WORKFLOW_CALLBACK_ORIGIN", "http://workflow.test:8080")
    module = import_module(f"cpg_ingester.services.{service}")
    work = Mock()
    monkeypatch.setattr(module, background, work)
    callback = "http://workflow.test:8080/wait-done"
    response = TestClient(module.app).post("/api/v1/" + route, json={
        "callback_url": callback, "process_instance_id": "instance-1", "pdf_ref": "synthetic.pdf",
    })
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    if service == "ingestion":
        work.assert_called_once_with("synthetic.pdf", callback, "instance-1")
    else:
        work.assert_called_once_with({"pdf_ref": "synthetic.pdf"}, callback, "instance-1")
