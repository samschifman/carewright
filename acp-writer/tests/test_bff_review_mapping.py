"""Regression tests for care-plan review routing/derivation.

The UI + contract (bff-openapi.yaml) submit ReviewAction with a `decision`
field. The workflow switch and this mapper must key on `decision` — an earlier
`action` mismatch made every review fall through to the default (WriteFHIR),
so rejecting a plan silently wrote it. See CheckCarePlanReview in
acp-writer-workflow.yaml.

request_changes loops back to ComposePlan (not GenerateBundle): goals and
activities are decided in the planning brief, so the plan is only actually
revised by re-composing with the clinician's feedback. These tests pin the
mapper's view of where the loop is.
"""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from acp_writer.services import bff
from acp_writer.services.sonataflow_client import (
    infer_current_state,
    map_to_run_detail,
)


def test_approve_routes_to_write_fhir():
    data = {"careplanReview": {"decision": "approve", "completed_at": "2026-01-01T00:00:00Z"}}
    assert infer_current_state(data, "ACTIVE") == "WriteFHIR"


def test_request_changes_loops_back_to_compose_plan():
    # Feedback just submitted; nothing regenerated yet. request_changes must loop
    # back to ComposePlan (goals/activities live in the planning brief) so the
    # clinician's feedback can actually revise the plan -- not straight to
    # GenerateBundle, which only re-serializes an unchanged brief.
    data = {
        "careplanReview": {"decision": "request_changes", "completed_at": "2026-01-01T02:00:00Z"},
        "composerData": {"completed_at": "2026-01-01T00:30:00Z"},
        "fhirGenData": {"completed_at": "2026-01-01T01:00:00Z"},
    }
    assert infer_current_state(data, "ACTIVE") == "ComposePlan"


def test_request_changes_after_recompose_is_generating_bundle():
    # Plan re-composed (composer newer than review) but bundle not yet regenerated.
    data = {
        "careplanReview": {"decision": "request_changes", "completed_at": "2026-01-01T02:00:00Z"},
        "composerData": {"completed_at": "2026-01-01T02:30:00Z"},
        "fhirGenData": {"completed_at": "2026-01-01T01:00:00Z"},
    }
    assert infer_current_state(data, "ACTIVE") == "GenerateBundle"


def test_second_round_gate_surfaces_previous_feedback():
    # request_changes, then bundle regenerated (gen newer than review) -> back at gate.
    instance = {
        "id": "run-1",
        "status": "ACTIVE",
        "startDate": "2026-01-01T00:00:00Z",
        "workflowdata": {
            "careplanReviewCount": 1,
            "careplanReview": {
                "decision": "request_changes",
                "clinician": "Dr. Smith",
                "comment": "Tighten the HbA1c target",
                "feedback": [{"itemId": "g1", "comment": "too loose"}],
                "completed_at": "2026-01-01T01:00:00Z",
            },
            "fhirGenData": {"completed_at": "2026-01-01T02:00:00Z"},
        },
    }
    detail = map_to_run_detail(instance)
    assert detail["awaitingReview"] == "careplan"
    assert detail["reviewIteration"] == 1
    assert detail["previousFeedback"] == {
        "decision": "request_changes",
        "clinician": "Dr. Smith",
        "comment": "Tighten the HbA1c target",
        "feedback": [{"itemId": "g1", "comment": "too loose"}],
    }


def test_fhir_review_iteration_is_running_without_awaiting_review():
    instance = {
        "id": "run-1",
        "status": "ACTIVE",
        "workflowdata": {
            "fhirGenData": {"completed_at": "2026-01-01T00:30:00Z"},
            "fhirReviewData": {"completed_at": "2026-01-01T01:00:00Z"},
        },
    }

    detail = map_to_run_detail(instance)

    assert detail["status"] == "running"
    assert detail["awaitingReview"] is None


def test_careplan_review_count_marks_the_review_gate():
    instance = {
        "id": "run-1",
        "status": "ACTIVE",
        "workflowdata": {
            "fhirReviewData": {"completed_at": "2026-01-01T01:00:00Z"},
            "careplanReviewCount": 0,
        },
    }

    detail = map_to_run_detail(instance)

    assert detail["status"] == "awaiting_careplan_review"
    assert detail["awaitingReview"] == "careplan"


def test_submit_review_during_fhir_review_returns_409(monkeypatch):
    instance = {
        "id": "sf-1",
        "status": "ACTIVE",
        "workflowdata": {
            "fhirGenData": {"completed_at": "2026-01-01T00:30:00Z"},
            "fhirReviewData": {"completed_at": "2026-01-01T01:00:00Z"},
        },
    }

    class FakeSonataFlow:
        def __init__(self):
            self.send_review = AsyncMock()

        async def get_instance_by_business_key(self, _business_key):
            return instance

        async def get_instance(self, _instance_id):
            return instance

    fake = FakeSonataFlow()
    monkeypatch.setattr(bff, "_sonataflow", fake)
    monkeypatch.setattr(bff, "_pending_runs", {})

    response = TestClient(bff.app).post(
        "/api/v1/runs/run-1/review/careplan",
        json={"decision": "approve", "reviewRound": 0},
    )

    assert response.status_code == 409
    fake.send_review.assert_not_awaited()
