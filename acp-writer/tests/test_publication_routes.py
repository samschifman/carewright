from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from acp_writer import api
from acp_writer.services import automation_publisher, fhir_server
from acp_writer.services.automation_publisher import AutomationPublisher
from acp_writer.services.bff import _to_careplan_detail
from acp_writer.services.job_store import JobStore


def _route(name):
    return {
        "monolith": api.retry_automation_publication,
        "split": fhir_server.retry_automation_publication,
    }[name]


def _publisher(tmp_path):
    return AutomationPublisher(
        JobStore(directory=tmp_path),
        fhir_url="http://fhir/fhir/R4",
        automation_url="",
        automation_token="",
    )


def _add_job(publisher, state):
    job = publisher.start_approval(
        "cp-1",
        [{"automation_id": "automation-1", "revision": "0123456789ab"}],
        {"display": "Dr Example"},
        datetime(2026, 10, 9, tzinfo=timezone.utc),
        bundle={"entry": []},
    )
    job["state"] = state
    if state == "unconfigured":
        job["configuration_reason"] = "missing-token"
    publisher.store.put(job)
    return job


@pytest.mark.parametrize("route_name", ["monolith", "split"])
def test_retry_route_returns_existing_terminal_state_without_job_data(route_name, tmp_path, monkeypatch):
    publisher = _publisher(tmp_path)
    _add_job(publisher, "unconfigured")
    monkeypatch.setattr(automation_publisher, "get_publisher", lambda: publisher)
    monkeypatch.setattr(fhir_server, "get_publisher", lambda: publisher)

    result = _route(route_name)("cp-1")

    assert result == {
        "state": "unconfigured",
        "publish_attempts": 0,
        "configuration_reason": "missing-token",
    }
    assert "bundle" not in result
    assert "automation_token" not in result


@pytest.mark.parametrize("route_name", ["monolith", "split"])
def test_retry_route_reports_approving_job_unchanged(route_name, tmp_path, monkeypatch):
    publisher = _publisher(tmp_path)
    _add_job(publisher, "approving")
    monkeypatch.setattr(automation_publisher, "get_publisher", lambda: publisher)
    monkeypatch.setattr(fhir_server, "get_publisher", lambda: publisher)

    assert _route(route_name)("cp-1") == {
        "state": "approving",
        "publish_attempts": 0,
    }


@pytest.mark.parametrize("route_name", ["monolith", "split"])
def test_retry_route_returns_404_when_no_job_exists(route_name, tmp_path, monkeypatch):
    publisher = _publisher(tmp_path)
    monkeypatch.setattr(automation_publisher, "get_publisher", lambda: publisher)
    monkeypatch.setattr(fhir_server, "get_publisher", lambda: publisher)

    with pytest.raises(HTTPException) as error:
        _route(route_name)("missing")

    assert error.value.status_code == 404


def test_bff_careplan_detail_exposes_only_automation_publication_status():
    detail = _to_careplan_detail({
        "id": "cp-1",
        "bundle": {"entry": []},
        "automation_publication": {
            "status": "unconfigured",
            "lastError": "do not expose this",
            "configurationReason": "missing-token",
        },
    })

    assert detail["automationPublication"] == {"status": "unconfigured"}
