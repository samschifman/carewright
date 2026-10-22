import logging

import requests

from cpg_contracts import cloud_events
from cpg_contracts.cloud_events import post_callback


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
