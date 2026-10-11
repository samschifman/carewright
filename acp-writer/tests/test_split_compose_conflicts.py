"""Regression test for conflict surfacing in the split / SonataFlow path.

The cluster runs the pod-split services, not the monolith ``pipeline.py``. The
``ComposePlan`` SonataFlow state calls the llm-reasoning pod's ``/api/v1/compose``
(and ``/api/v1/compose-async``) endpoint. Conflict detection must run there —
otherwise conflicts never surface in a deployed run (see issue #169).

These tests exercise the compose endpoints with a mocked LLM and assert that the
returned planning brief carries analyst-detected conflicts.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from acp_writer.services import fhir_generation, llm_reasoning

from ._conflict_fixtures import OVERLAP_JSON as _OVERLAP_JSON
from ._conflict_fixtures import mock_llm as _mock_llm


client = TestClient(llm_reasoning.app)


class _EmptyTemplateClient:
    def list(self, **_kwargs):
        return []

    def validate(self, _payload):
        return {"valid": True}


@pytest.fixture(autouse=True)
def _disable_registry_network(monkeypatch):
    monkeypatch.setattr(llm_reasoning, "TemplateClient", _EmptyTemplateClient)


def _ai_input_prompts(bundle: dict) -> list[dict]:
    return [
        e["resource"]
        for e in bundle.get("entry", [])
        if e["resource"]["resourceType"] == "DocumentReference"
        and any(
            "AI-InputPrompt" in p
            for p in e["resource"].get("meta", {}).get("profile", [])
        )
    ]


def _composed_brief() -> dict:
    """A converged brief with two same-diet activities — no conflicts annotated
    yet (the composer never flags them; the analyst does)."""
    return {
        "patient_reference": "Patient/1",
        "applicable_cpgs": ["SYN-HTN-2026-001", "SYN-DM2-2026-001"],
        "goals": [],
        "activities": [
            {"type": "lifestyle", "description": "Adopt a healthy diet",
             "source_cpg": "SYN-HTN-2026-001", "source_recommendation_id": "htn-rec-004"},
            {"type": "lifestyle", "description": "Follow a heart-healthy diet",
             "source_cpg": "SYN-DM2-2026-001", "source_recommendation_id": "dm2-rec-004"},
        ],
        "conflicts": [],
        "review_status": "approved",
    }


_COMPOSE_PAYLOAD = {
    "patient_reference": "Patient/1",
    "recommendations": [
        {"id": "htn-rec-004", "title": "Diet", "content": "Adopt a healthy diet", "source_cpg": "SYN-HTN-2026-001"},
        {"id": "dm2-rec-004", "title": "Diet", "content": "Follow a heart-healthy diet", "source_cpg": "SYN-DM2-2026-001"},
    ],
    "applicable_cpgs": ["SYN-HTN-2026-001", "SYN-DM2-2026-001"],
    "condition_codes": [{"display": "Hypertension"}, {"display": "Type 2 diabetes"}],
}


def test_compose_runs_conflict_analyst():
    """The sync /compose endpoint must run the conflict analyst so the returned
    brief carries conflicts (regression: it previously stopped after the
    brief-review loop and never invoked the analyst)."""
    with patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(llm_reasoning, "plan_composer",
                      return_value={"planning_brief": _composed_brief()}), \
         patch.object(llm_reasoning, "brief_reviewer",
                      return_value={"brief_review_feedback": "", "brief_review_count": 1}), \
         patch("acp_writer.nodes.conflict_analyst.get_llm",
               return_value=_mock_llm(_OVERLAP_JSON)):
        resp = client.post("/api/v1/compose", json=_COMPOSE_PAYLOAD)

    assert resp.status_code == 200
    brief = resp.json()["planning_brief"]
    conflicts = brief["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["category"] == "overlap"
    assert conflicts[0]["activity_indices"] == [0, 1]


def test_compose_offloads_blocking_pipeline_to_thread():
    """C9: /compose is async but the reasoning pipeline (composer → reviewer
    loop → conflict analyst) makes blocking LLM calls. It must run in a worker
    thread via asyncio.to_thread so it never stalls the event loop."""
    import asyncio

    real_to_thread = asyncio.to_thread
    offloaded = []

    async def _recording_to_thread(fn, *args, **kwargs):
        offloaded.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    with patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(llm_reasoning, "plan_composer",
                      return_value={"planning_brief": _composed_brief()}), \
         patch.object(llm_reasoning, "brief_reviewer",
                      return_value={"brief_review_feedback": "", "brief_review_count": 1}), \
         patch("acp_writer.nodes.conflict_analyst.get_llm",
               return_value=_mock_llm(_OVERLAP_JSON)), \
         patch.object(llm_reasoning.asyncio, "to_thread", _recording_to_thread):
        resp = client.post("/api/v1/compose", json=_COMPOSE_PAYLOAD)

    assert resp.status_code == 200
    assert llm_reasoning._compose_pipeline in offloaded


def test_compose_async_runs_conflict_analyst():
    """The async /compose-async background task must also run the analyst; the
    conflicts ride the brief in the callback payload."""
    captured = {}

    def _capture(callback_url, process_instance_id, event, result):
        captured["result"] = result

    with patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(llm_reasoning, "plan_composer",
                      return_value={"planning_brief": _composed_brief()}), \
         patch.object(llm_reasoning, "brief_reviewer",
                      return_value={"brief_review_feedback": "", "brief_review_count": 1}), \
         patch("acp_writer.nodes.conflict_analyst.get_llm",
               return_value=_mock_llm(_OVERLAP_JSON)), \
         patch.object(llm_reasoning, "post_callback", _capture):
        payload = dict(_COMPOSE_PAYLOAD, callback_url="http://cb", process_instance_id="pi-1")
        resp = client.post("/api/v1/compose-async", json=payload)

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    brief = captured["result"]["planning_brief"]
    conflicts = brief["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["category"] == "overlap"


def test_prompts_ferry_compose_to_generate_bundle():
    """F2: rendered prompts captured in the split compose service must reach the
    fhir-generation service so the DEPLOYED bundle carries AI-InputPrompt
    DocRefs, exactly as the monolith does. Before F2 the compose endpoint
    returned only the brief and dropped the prompts, so the split-path bundle
    silently lost prompt traceability (issue #169)."""
    composed = {
        "planning_brief": _composed_brief(),
        "plan_composer_prompt": "COMPOSE PROMPT — patient X",
    }
    with patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(llm_reasoning, "plan_composer", return_value=composed), \
         patch.object(llm_reasoning, "brief_reviewer",
                      return_value={"brief_review_feedback": "", "brief_review_count": 1}), \
         patch("acp_writer.nodes.conflict_analyst.get_llm",
               return_value=_mock_llm(_OVERLAP_JSON)):
        compose_resp = client.post("/api/v1/compose", json=_COMPOSE_PAYLOAD)

    assert compose_resp.status_code == 200
    compose_out = compose_resp.json()
    # Prompts must be ferried out of compose (inline when no PHI store).
    assert "prompts" in compose_out
    assert compose_out["prompts"].get("plan_composer_prompt") == "COMPOSE PROMPT — patient X"
    assert compose_out["prompts"].get("conflict_prompt")

    gen_client = TestClient(fhir_generation.app)
    with patch.object(fhir_generation, "_phi_store", None):
        gen_resp = gen_client.post("/api/v1/generate-bundle", json={
            "planning_brief": compose_out["planning_brief"],
            "prompts": compose_out["prompts"],
        })

    assert gen_resp.status_code == 200
    bundle = gen_resp.json()["fhir_bundle"]
    input_prompts = _ai_input_prompts(bundle)
    assert input_prompts, "deployed bundle lost AI-InputPrompt DocRefs (F2)"


def test_prior_conflicts_reach_the_composer_state():
    """F18a through /compose: a request-changes call threads the prior brief
    (with its conflicts) and the clinician comment into composer STATE — the
    durable channel plan_composer renders every iteration. brief_review_feedback
    starts EMPTY: seeding clinician directives there was the F18 bug (the
    internal reviewer overwrites that channel each iteration)."""
    captured = {}

    def _capture_compose(state):
        captured["feedback"] = state.get("brief_review_feedback", "")
        captured["careplan_feedback"] = state.get("careplan_feedback", "")
        captured["prior"] = state.get("prior_planning_brief") or {}
        return {"planning_brief": _composed_brief()}

    prior_brief = {
        "conflicts": [{
            "id": "conf-xyz", "category": "overlap", "severity": "info",
            "description": "Both guidelines recommend a healthy diet",
            "suggested_resolution": "Combine the two diet activities into one",
            "sources": [{"cpg_id": "SYN-HTN-2026-001", "recommendation_id": "htn-rec-004"}],
        }],
    }
    with patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(llm_reasoning, "plan_composer", side_effect=_capture_compose), \
         patch.object(llm_reasoning, "brief_reviewer",
                      return_value={"brief_review_feedback": "", "brief_review_count": 1}), \
         patch("acp_writer.nodes.conflict_analyst.get_llm",
               return_value=_mock_llm(_OVERLAP_JSON)):
        # prior_brief_ref (truthy) passes the guard; with _phi_store None the
        # resolver falls back to the inline prior_brief payload.
        payload = dict(
            _COMPOSE_PAYLOAD,
            careplan_feedback="resolve all identified conflicts as you suggested",
            prior_brief_ref="phi:prior-brief",
            prior_brief=prior_brief,
        )
        resp = client.post("/api/v1/compose", json=payload)

    assert resp.status_code == 200
    # The reviewer channel starts empty (F18a): clinician content must NOT be
    # seeded there, or the internal review loop wipes it after iteration 1.
    assert captured["feedback"] == ""
    assert captured["careplan_feedback"] == "resolve all identified conflicts as you suggested"
    prior_conflicts = captured["prior"].get("conflicts") or []
    assert prior_conflicts and prior_conflicts[0]["id"] == "conf-xyz"
    assert prior_conflicts[0]["suggested_resolution"] == "Combine the two diet activities into one"


def test_first_pass_has_no_prior_brief_or_clinician_feedback():
    """Control for F18a: first pass (no prior_brief_ref) → authoring state."""
    captured = {}

    def _capture_compose(state):
        captured["feedback"] = state.get("brief_review_feedback", "")
        captured["careplan_feedback"] = state.get("careplan_feedback", "")
        captured["prior"] = state.get("prior_planning_brief") or {}
        return {"planning_brief": _composed_brief()}

    with patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(llm_reasoning, "plan_composer", side_effect=_capture_compose), \
         patch.object(llm_reasoning, "brief_reviewer",
                      return_value={"brief_review_feedback": "", "brief_review_count": 1}), \
         patch("acp_writer.nodes.conflict_analyst.get_llm",
               return_value=_mock_llm(_OVERLAP_JSON)):
        resp = client.post("/api/v1/compose", json=_COMPOSE_PAYLOAD)

    assert resp.status_code == 200
    assert captured["feedback"] == ""
    assert captured["careplan_feedback"] == ""
    assert captured["prior"] == {}


def test_generate_bundle_without_prompts_has_no_input_prompt_docrefs():
    """Control for F2: no ferried prompts → no AI-InputPrompt DocRefs. Proves the
    ferry is what puts them in the deployed bundle, not the brief alone."""
    gen_client = TestClient(fhir_generation.app)
    with patch.object(fhir_generation, "_phi_store", None):
        gen_resp = gen_client.post("/api/v1/generate-bundle", json={
            "planning_brief": _composed_brief(),
        })

    assert gen_resp.status_code == 200
    assert _ai_input_prompts(gen_resp.json()["fhir_bundle"]) == []
