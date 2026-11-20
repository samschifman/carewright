"""Async handlers reject off-origin callbacks before running any pipeline work."""

from importlib import import_module
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(("service", "route", "background"), [
    ("patient_data", "scan-async", "_run_scan_background"),
    ("fhir_generation", "generate-bundle-async", "_run_generate_background"),
    ("llm_reasoning", "execute-dmn-async", "_run_execute_dmn_background"),
    ("llm_reasoning", "resolve-async", "_run_resolve_background"),
    ("llm_reasoning", "retrieve-async", "_run_retrieve_background"),
    ("llm_reasoning", "compose-async", "_run_compose_background"),
    ("llm_reasoning", "review-fhir-async", "_run_review_fhir_background"),
])
@pytest.mark.parametrize("callback", [None, "http://other.test/wait-done", "http://workflow.test:8081/wait-done"])
def test_async_callback_origin_rejected_before_work(monkeypatch, service, route, background, callback):
    monkeypatch.setenv("WORKFLOW_CALLBACK_ORIGIN", "http://workflow.test:8080")
    module = import_module(f"acp_writer.services.{service}")
    work = Mock()
    monkeypatch.setattr(module, background, work)
    payload = {} if callback is None else {"callback_url": callback}
    response = TestClient(module.app).post("/api/v1/" + route, json=payload)
    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "callback-origin"}}
    work.assert_not_called()


@pytest.mark.parametrize(("service", "route", "background"), [
    ("patient_data", "scan-async", "_run_scan_background"),
    ("fhir_generation", "generate-bundle-async", "_run_generate_background"),
    ("llm_reasoning", "compose-async", "_run_compose_background"),
])
def test_valid_callback_still_schedules_work(monkeypatch, service, route, background):
    monkeypatch.setenv("WORKFLOW_CALLBACK_ORIGIN", "http://workflow.test:8080")
    module = import_module(f"acp_writer.services.{service}")
    work = Mock()
    monkeypatch.setattr(module, background, work)
    callback = "http://workflow.test:8080/wait-done"
    response = TestClient(module.app).post("/api/v1/" + route, json={
        "callback_url": callback, "process_instance_id": "instance-1", "value": "synthetic",
    })
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    work.assert_called_once_with({"value": "synthetic"}, callback, "instance-1")
