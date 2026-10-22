"""Endpoint tests for POST /api/v1/runs/{id}/review/{gate} and run lookup.

Covers the review-gate v2 behavior (#169):

  - engine send_review raises -> 503 with a friendly message (kept from bcf487f)
  - the submission's `reviewRound` reaches the CloudEvent data verbatim, under
    that exact camelCase key (the round guard fails OPEN, so this passthrough is
    load-bearing — a rename would silently disable engine-side validation)
  - submit at a gate that isn't armed -> 409 (fast-feedback courtesy)
  - runs resolve by business key when the in-memory map is empty (BFF restart),
    for both get_run and submit_review; an unknown run -> 404

The duplicate-tracker (_pending_reviews) from bcf487f is gone: duplicates are
now handled semantically by the engine's round guard, so there are no
409-duplicate tests here.
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from acp_writer.services import bff


def _instance(workflowdata: dict) -> dict:
    return {
        "id": "sf-1",
        "status": "ACTIVE",
        "startDate": "2026-01-01T00:00:00Z",
        "workflowdata": workflowdata,
    }


# careplanReviewCount is injected immediately before the ReviewCarePlan gate.
AT_GATE = _instance({
    "fhirReviewData": {"completed_at": "2026-01-01T01:00:00Z"},
    "careplanReviewCount": 0,
})
# approve consumed -> WriteFHIR, run no longer awaiting the careplan gate.
PAST_GATE = _instance(
    {
        "fhirReviewData": {"completed_at": "2026-01-01T01:00:00Z"},
        "careplanReview": {"decision": "approve", "completed_at": "2026-01-01T02:00:00Z"},
    }
)


class FakeSonataFlow:
    """Stand-in for SonataFlowClient.

    Models the real id/business-key split: `run_id` is a business key, never a
    valid instance id, so get_instance(run_id) raises and only get_instance for
    the actual instance id (`instance["id"]`) or a business-key lookup succeeds.
    """

    def __init__(self, instance: dict):
        self.instance = instance
        self.send_review = AsyncMock()

    async def get_instance(self, sf_id: str) -> dict:
        if sf_id == self.instance["id"]:
            return self.instance
        raise httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("POST", "http://sf/graphql"),
            response=httpx.Response(404),
        )

    async def get_instance_by_business_key(self, key: str) -> dict | None:
        return self.instance


@pytest.fixture
def client(monkeypatch):
    fake = FakeSonataFlow(AT_GATE)
    monkeypatch.setattr(bff, "_sonataflow", fake)
    monkeypatch.setattr(bff, "_pending_runs", {})
    tc = TestClient(bff.app)
    tc.fake = fake  # let tests reach the mock
    return tc


def test_engine_unavailable_returns_503(client):
    client.fake.send_review.side_effect = RuntimeError("Error Occurred After Shutdown")
    res = client.post("/api/v1/runs/run-1/review/careplan", json={"decision": "approve", "reviewRound": 0})
    assert res.status_code == 503
    assert "temporarily unavailable" in res.json()["message"]


def test_review_round_reaches_cloudevent_verbatim(client):
    res = client.post(
        "/api/v1/runs/run-1/review/careplan",
        json={"decision": "approve", "clinician": "Dr. Smith", "reviewRound": 2},
    )
    assert res.status_code == 202
    # send_review(sf_id, gate, review_dict) — the review dict is what becomes the
    # CloudEvent `data`. Assert the EXACT camelCase key survives.
    _sf_id, _gate, review = client.fake.send_review.await_args.args
    assert "reviewRound" in review
    assert review["reviewRound"] == 2


def test_submit_when_not_awaiting_gate_returns_409(client):
    client.fake.instance = PAST_GATE
    res = client.post("/api/v1/runs/run-1/review/careplan", json={"decision": "approve", "reviewRound": 0})
    assert res.status_code == 409
    assert "not awaiting" in res.json()["message"]
    client.fake.send_review.assert_not_awaited()


def test_submit_resolves_run_by_business_key(client):
    # Empty _pending_runs (as after a BFF restart): the run must still be found
    # via the business-key fallback and reach the gate check + send_review.
    res = client.post("/api/v1/runs/run-1/review/careplan", json={"decision": "approve", "reviewRound": 0})
    assert res.status_code == 202
    assert client.fake.send_review.await_count == 1


def test_get_run_resolves_by_business_key(client):
    # get_instance(run_id) raises (business key is not an instance id); the
    # business-key fallback must recover the live run instead of 404ing.
    res = client.get("/api/v1/runs/run-1")
    assert res.status_code == 200
    assert res.json()["awaitingReview"] == "careplan"


def test_get_run_unknown_returns_404(client):
    async def none_by_key(key):
        return None

    client.fake.get_instance_by_business_key = none_by_key
    res = client.get("/api/v1/runs/does-not-exist")
    assert res.status_code == 404
