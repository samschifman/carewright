"""A persisted care plan shows the brief's automations, not only the FHIR ones.

The FHIR bundle carries the activity-automation extension only for enabled,
valid instances. Incomplete and escalated automations exist in the planning
brief, which is what the review gate shows. The persisted view must show the
same set, so a clinician opening a saved plan sees what the gate saw.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from acp_writer.services import bff

CAREPLAN_ID = "cp-1"
ACTIVITY_ID = "act-1"

BRIEF = {
    "goals": [],
    "conflicts": [],
    "activities": [
        {
            "id": ACTIVITY_ID,
            "type": "monitoring",
            "description": "Every 12 months, query the record for a screening result.",
            "source_cpg": "DEMO",
            "source_recommendation_id": "rec-1",
            "clinical_rationale": "The guideline requires annual screening.",
            "automations": [
                {
                    "id": "auto-1",
                    "enabled": False,
                    "template_refs": [
                        {"template_id": "annual-screening", "version": "v1", "source_cpg": "DEMO"}
                    ],
                    "bindings": [{"name": "max_duration", "value": "P90D", "source": "plan-derived"}],
                    "validation": {"status": "incomplete", "rungs_passed": ["L0", "L1"], "warnings": [], "errors": []},
                    "bpmn_xml": "<definitions/>",
                    "capabilities_used": ["ehr.query_results"],
                }
            ],
        }
    ],
}

COMPLETED = {
    "id": "sf-1",
    "status": "COMPLETED",
    "startDate": "2026-01-01T00:00:00Z",
    "workflowdata": {
        "composerData": {"planning_brief": BRIEF},
        "fhirGenData": {"fhir_bundle": {"resourceType": "Bundle", "entry": []}},
        "writeResult": {"careplan_id": CAREPLAN_ID},
    },
}

PERSISTED = {
    "id": CAREPLAN_ID,
    "status": "active",
    "bundle": {
        "resourceType": "Bundle",
        "entry": [
            {
                "fullUrl": "urn:uuid:cp",
                "resource": {
                    "resourceType": "CarePlan",
                    "id": CAREPLAN_ID,
                    "status": "active",
                    "subject": {"reference": "Patient/p1"},
                    "activity": [
                        {
                            "id": ACTIVITY_ID,
                            "detail": {"description": "Every 12 months, query the record for a screening result."},
                        }
                    ],
                },
            }
        ],
    },
}


class FakeSonataFlow:
    async def list_instances(self):
        return [COMPLETED]

    async def get_instance(self, sf_id):
        assert sf_id == COMPLETED["id"]
        return COMPLETED


class StoreNeverCalled:
    def get(self, ref):  # pragma: no cover - the brief is inline, no ref to resolve
        raise AssertionError(f"unexpected store read {ref!r}")


@pytest.fixture
def client(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/api/v1/careplans/{CAREPLAN_ID}")
        return httpx.Response(200, json=PERSISTED)

    monkeypatch.setattr(bff, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(bff, "_sonataflow", FakeSonataFlow())
    monkeypatch.setattr(bff, "_phi_store", StoreNeverCalled())
    monkeypatch.setattr(bff, "_artifacts_store", None)
    return TestClient(bff.app)


def test_persisted_careplan_carries_brief_automations(client):
    detail = client.get(f"/api/v1/careplans/{CAREPLAN_ID}").json()

    assert detail["runId"] == "sf-1"
    activity = next(a for a in detail["view"]["activities"] if a["id"] == ACTIVITY_ID)
    automations = activity["automations"]
    assert [a["id"] for a in automations] == ["auto-1"]
    assert automations[0]["validation"]["status"] == "incomplete"
    assert automations[0]["enabled"] is False
    assert automations[0]["templateRefs"][0]["templateId"] == "annual-screening"
    # Provenance comes across from the brief as well; the card shows it.
    assert activity["sourceCpg"] == "DEMO"
    assert activity["sourceRecommendationId"] == "rec-1"
    assert activity["clinicalRationale"] == "The guideline requires annual screening."


def test_persisted_careplan_without_a_run_is_unchanged(client, monkeypatch):
    class NoRuns:
        async def list_instances(self):
            return []

    monkeypatch.setattr(bff, "_sonataflow", NoRuns())
    detail = client.get(f"/api/v1/careplans/{CAREPLAN_ID}").json()
    activity = next(a for a in detail["view"]["activities"] if a["id"] == ACTIVITY_ID)
    assert "automations" not in activity
