import logging

import pytest
import requests
from cpg_contracts import cloud_events
from cpg_contracts.cloud_events import (
    callback_origin_allowed,
    post_callback,
    require_callback_url,
)


@pytest.fixture(autouse=True)
def allowed_origin(monkeypatch):
    monkeypatch.setenv("WORKFLOW_CALLBACK_ORIGIN", "http://sonataflow")
    monkeypatch.setattr(cloud_events, "_warned_missing_origin", False)


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _call(session, **kwargs):
    return post_callback(
        "http://sonataflow/callback",
        "instance-1",
        "task-done",
        {"result": "ok"},
        session=session,
        **kwargs,
    )


def test_post_callback_retries_transport_failure_and_reuses_event_id(monkeypatch):
    session = FakeSession([requests.ConnectionError(), FakeResponse()])
    sleeps = []
    monkeypatch.setattr(cloud_events.time, "sleep", sleeps.append)

    assert _call(session)

    assert len(session.calls) == 2
    events = [call[1]["json"] for call in session.calls]
    assert events[0]["id"] == events[1]["id"]
    assert all(call[1]["timeout"] == (30, 60) for call in session.calls)
    assert sleeps == [2]


def test_post_callback_gives_up_after_three_transport_failures(monkeypatch, caplog):
    session = FakeSession([
        requests.ConnectionError(),
        requests.Timeout(),
        requests.ConnectionError(),
    ])
    sleeps = []
    monkeypatch.setattr(cloud_events.time, "sleep", sleeps.append)

    with caplog.at_level(logging.WARNING, logger=cloud_events.logger.name):
        assert not _call(session)

    assert len(session.calls) == 3
    assert len({call[1]["json"]["id"] for call in session.calls}) == 1
    assert sleeps == [2, 10]
    assert [record.getMessage() for record in caplog.records] == [
        "Callback attempt failed: type=task-done instance=instance-1",
        "Callback attempt failed: type=task-done instance=instance-1",
        "Callback attempt failed: type=task-done instance=instance-1",
    ]


def test_post_callback_does_not_retry_client_error(monkeypatch, caplog):
    session = FakeSession([FakeResponse(422)])
    sleeps = []
    monkeypatch.setattr(cloud_events.time, "sleep", sleeps.append)

    with caplog.at_level(logging.ERROR, logger=cloud_events.logger.name):
        assert not _call(session)

    assert len(session.calls) == 1
    assert sleeps == []
    assert [record.getMessage() for record in caplog.records] == [
        "Callback failed: type=task-done instance=instance-1",
    ]


@pytest.mark.parametrize(("origins", "url", "expected"), [
    ("http://sonataflow", "http://sonataflow/wait-done", True),
    ("http://sonataflow", "http://sonataflow:80/wait-done", True),
    ("http://sonataflow:80", "http://sonataflow/wait-done", True),
    ("https://sonataflow", "https://SONATAFLOW:443/wait-done?q=ok", True),
    ("http://sonataflow", "https://sonataflow/wait-done", False),
    ("http://sonataflow", "http://other/wait-done", False),
    ("http://sonataflow", "http://sonataflow:8080/wait-done", False),
    ("http://sonataflow, https://second:8443", "https://second:8443/wait-done", True),
    ("http://sonataflow", "http://sonataflow.attacker.test/wait-done", False),
    ("http://sonataflow", "http://sonataflow@attacker.test/wait-done", False),
    ("http://sonataflow", "http://attacker.test@sonataflow/wait-done", False),
    ("http://sonataflow", "http://attacker.test\\@sonataflow/wait-done", False),
    ("http://sonataflow", " http://sonataflow/wait-done", False),
    ("http://sonataflow", "http://sona\ttaflow/wait-done", False),
    ("http://sonataflow", "http://sonataflow:bad/wait-done", False),
    ("http://sonataflow", "http://sonataflow:65536/wait-done", False),
    ("http://sonataflow", "http://sonataflow:0/wait-done", False),
    ("http://sonataflow", "http://[broken/wait-done", False),
    ("http://sonataflow", "//sonataflow/wait-done", False),
    ("http://sonataflow", "file:///etc/passwd", False),
    ("http://sonataflow", "", False),
    ("http://sonataflow", None, False),
    ("http://sonataflow", {"host": "sonataflow"}, False),
    ("http://sonataflow/prefix", "http://sonataflow/wait-done", False),
    ("http://sonataflow?q=bad", "http://sonataflow/wait-done", False),
    ("ftp://sonataflow", "ftp://sonataflow/wait-done", False),
    ("invalid, http://sonataflow/", "http://sonataflow/wait-done", True),
    ("http://[::1]:8080", "http://[::1]:8080/wait-done", True),
    ("http://[::1]:8080", "http://[::1]:8081/wait-done", False),
])
def test_callback_origin_matches_only_configured_origins(monkeypatch, origins, url, expected):
    monkeypatch.setenv("WORKFLOW_CALLBACK_ORIGIN", origins)
    assert callback_origin_allowed(url) is expected


def test_unconfigured_callbacks_fail_closed_and_warn_once(monkeypatch, caplog):
    monkeypatch.delenv("WORKFLOW_CALLBACK_ORIGIN", raising=False)
    with caplog.at_level(logging.WARNING, logger=cloud_events.logger.name):
        assert not callback_origin_allowed("http://sonataflow/wait-done")
        assert not callback_origin_allowed("http://sonataflow/wait-done")
    assert [r.getMessage() for r in caplog.records] == [
        "WORKFLOW_CALLBACK_ORIGIN not set; callbacks disabled",
    ]


def test_require_callback_consumes_valid_url_only():
    data = {"callback_url": "http://sonataflow/wait-done", "input": "unchanged"}
    assert require_callback_url(data) == "http://sonataflow/wait-done"
    assert data == {"input": "unchanged"}


@pytest.mark.parametrize("data", [{}, {"callback_url": "http://other"}])
def test_require_callback_refuses_missing_or_wrong_origin(data):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        require_callback_url(data)
    assert exc.value.status_code == 400
    assert exc.value.detail == {"error": "callback-origin"}


def test_disallowed_callback_makes_no_request_and_does_not_mutate_data(caplog):
    session = FakeSession([])
    data = {"result": "ok"}
    with caplog.at_level(logging.WARNING, logger=cloud_events.logger.name):
        assert not post_callback("http://other/wait-done", "instance-1", "done", data, session=session)
    assert session.calls == []
    assert data == {"result": "ok"}
    assert [r.getMessage() for r in caplog.records] == ["callback refused: origin not allowed"]


def test_post_callback_disables_redirects_and_reports_redirect_as_failure():
    session = FakeSession([FakeResponse(302)])
    assert not _call(session)
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
