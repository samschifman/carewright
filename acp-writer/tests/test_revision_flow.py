"""Multi-round revision-flow harness (issue #169 F18).

Drives the REAL split-path compose pipeline (`llm_reasoning._compose_pipeline`:
composer → internal brief-review loop → conflict analyst → directive
enforcement) through multiple clinician review rounds — locally, with no
SonataFlow, no MinIO, and no cluster deploy.

Two layers:

1. **Structural tests (always run).** The three LLM roles are scripted fakes,
   so the tests assert the *process*: the clinician-directives section survives
   internal review iterations (THE F18 regression), the revision base evolves
   to the latest draft, enforcement retries and then flags, and multi-round
   continuity holds.

2. **Live test / driver (LLM-gated).** With ``LLM_BASE_URL`` (or legacy
   ``LITELLM_URL``) set, `test_live_two_rounds` runs the pipeline against a
   real model over the HTN+DM2 fixture guidelines: author → "resolve all
   conflicts according to suggestions" → assert every directed conflict is
   resolved or the brief is honestly flagged. Run interactively with:

       cd acp-writer && LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... \
           python -m tests.test_revision_flow
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from acp_writer.nodes import brief_reviewer as brief_reviewer_mod
from acp_writer.nodes import conflict_analyst as conflict_analyst_mod
from acp_writer.nodes import plan_composer as plan_composer_mod
from acp_writer.services import llm_reasoning

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "integration" / "fixtures"


# ---------------------------------------------------------------------------
# Scripted role LLMs
# ---------------------------------------------------------------------------


class ScriptedLLM:
    """Returns queued responses; records every invoke's messages.

    The last response repeats once the queue is exhausted, so a scripted role
    keeps answering if the pipeline calls it more often than the test expected
    (the call-count assertions still catch the difference).
    """

    def __init__(self, *responses: str):
        self.queue = list(responses)
        self.calls: list[list[dict]] = []

    def invoke(self, messages):
        self.calls.append([dict(m) for m in messages])
        text = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return SimpleNamespace(content=text)

    # -- helpers for assertions ------------------------------------------------
    def user_prompt(self, call: int) -> str:
        return next(m["content"] for m in self.calls[call] if m["role"] == "user")

    def system_prompt(self, call: int) -> str:
        return next(m["content"] for m in self.calls[call] if m["role"] == "system")


class EmptyTemplateClient:
    """Keep revision-flow tests independent of the decision-engine service."""

    def list(self, *, recommendation_id=None, dmn_model_id=None):
        return []

    def validate(self, payload):
        raise AssertionError("no automation template was selected")


def _brief_json(goals: list[dict], activities: list[dict]) -> str:
    return json.dumps({
        "patient_reference": "Patient/robert",
        "applicable_cpgs": ["SYN-HTN-2026-001", "SYN-DM2-2026-001"],
        "goals": goals,
        "activities": activities,
        "review_status": "pending",
    })


APPROVE = json.dumps({"verdict": "APPROVE", "issues": []})
REVISE_ONCE = json.dumps({
    "verdict": "REVISE",
    "issues": [{"severity": "warning", "description": "tighten monitoring interval", "fix": "specify 3 months"}],
})


# The round-1 ("prior") plan: divergent BP-target goals + duplicate diet
# activities — the standard conflict bait.
GOAL_BP_HTN = {"description": "BP below 140/90", "source_cpg": "SYN-HTN-2026-001",
               "source_recommendation_id": "htn-rec-001"}
GOAL_BP_DM2 = {"description": "BP below 130/80", "source_cpg": "SYN-DM2-2026-001",
               "source_recommendation_id": "dm2-rec-002"}
ACT_DIET_HTN = {"type": "lifestyle", "description": "Reduced-sodium balanced diet",
                "source_cpg": "SYN-HTN-2026-001", "source_recommendation_id": "htn-rec-003"}
ACT_DIET_DM2 = {"type": "lifestyle", "description": "Diabetes meal planning, limit refined carbs",
                "source_cpg": "SYN-DM2-2026-001", "source_recommendation_id": "dm2-rec-004"}
ACT_HBA1C = {"type": "monitoring", "description": "HbA1c monitoring", "frequency": "3 months",
             "source_cpg": "SYN-DM2-2026-001", "source_recommendation_id": "dm2-rec-003"}

PRIOR_CONFLICTS = [
    {
        "id": "conf-bp000001", "category": "divergent_target", "severity": "warning",
        "status": "detected",
        "description": "Goal [0] targets BP <140/90 while goal [1] targets BP <130/80",
        "suggested_resolution": "Prefer the diabetes guideline's <130/80 target and drop the 140/90 goal",
        "sources": [{"cpg_id": "SYN-HTN-2026-001", "recommendation_id": "htn-rec-001"},
                    {"cpg_id": "SYN-DM2-2026-001", "recommendation_id": "dm2-rec-002"}],
    },
    {
        "id": "conf-diet0001", "category": "overlap", "severity": "info",
        "status": "detected",
        "description": "Activities [0] and [1] both prescribe a healthy diet",
        "suggested_resolution": "Combine into one cardiometabolic diet activity",
        "sources": [{"cpg_id": "SYN-HTN-2026-001", "recommendation_id": "htn-rec-003"},
                    {"cpg_id": "SYN-DM2-2026-001", "recommendation_id": "dm2-rec-004"}],
    },
]

PRIOR_BRIEF = {
    "patient_reference": "Patient/robert",
    "applicable_cpgs": ["SYN-HTN-2026-001", "SYN-DM2-2026-001"],
    "goals": [GOAL_BP_HTN, GOAL_BP_DM2],
    "activities": [ACT_DIET_HTN, ACT_DIET_DM2, ACT_HBA1C],
    "conflicts": PRIOR_CONFLICTS,
    "review_status": "pending",
}

# The properly revised plan: one BP goal, one merged diet activity.
GOAL_BP_MERGED = {"description": "BP below 130/80", "source_cpg": "SYN-DM2-2026-001",
                  "source_recommendation_id": "dm2-rec-002"}
ACT_DIET_MERGED = {"type": "lifestyle",
                   "description": "Combined cardiometabolic diet: reduced sodium, limit refined carbs",
                   "source_cpg": "SYN-DM2-2026-001", "source_recommendation_id": "dm2-rec-004"}
REVISED_BRIEF_JSON = _brief_json([GOAL_BP_MERGED], [ACT_DIET_MERGED, ACT_HBA1C])
UNREVISED_BRIEF_JSON = _brief_json([GOAL_BP_HTN, GOAL_BP_DM2],
                                   [ACT_DIET_HTN, ACT_DIET_DM2, ACT_HBA1C])


def _analyst_json(conflicts: list[dict]) -> str:
    return json.dumps({"conflicts": conflicts})


ANALYST_RESOLVED = _analyst_json([
    {**PRIOR_CONFLICTS[0], "status": "resolved", "clinician_directed": True,
     "resolution": "Resolved as suggested — kept the 130/80 goal", "goal_indices": [], "activity_indices": []},
    {**PRIOR_CONFLICTS[1], "status": "resolved", "clinician_directed": True,
     "resolution": "Resolved as suggested — merged the diet activities", "goal_indices": [], "activity_indices": []},
])
ANALYST_STILL_DETECTED = _analyst_json([
    {**PRIOR_CONFLICTS[0], "status": "detected", "clinician_directed": True,
     "goal_indices": [0, 1], "activity_indices": []},
    {**PRIOR_CONFLICTS[1], "status": "detected", "clinician_directed": True,
     "goal_indices": [], "activity_indices": [0, 1]},
])

REVISION_PAYLOAD = {
    "patient_reference": "Patient/robert",
    "patient_demographics": {"name": "Robert Thompson", "gender": "male", "birth_date": "1958-11-30"},
    "condition_codes": [{"system": "s", "code": "c", "display": "Hypertension"}],
    "medication_codes": [],
    "allergy_codes": [],
    "dmn_results": [],
    "recommendations": [],
    "applicable_cpgs": [{"cpg_id": "SYN-HTN-2026-001"}, {"cpg_id": "SYN-DM2-2026-001"}],
    "careplan_feedback": "resolve all conflicts according to suggestions",
    "careplan_review_history": [
        {"decision": "request_changes", "comment": "resolve all conflicts according to suggestions",
         "clinician": "Demo Clinician", "completed_at": "2026-08-27T20:00:00Z"},
    ],
    "prior_brief": PRIOR_BRIEF,
    "prior_brief_ref": "",  # inline prior_brief (no artifact store locally)
}


def _run_pipeline(payload, composer: ScriptedLLM, reviewer: ScriptedLLM, analyst: ScriptedLLM) -> dict:
    """Run the real _compose_pipeline with scripted role LLMs and no stores."""
    with patch.object(llm_reasoning, "_store", None), \
         patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(plan_composer_mod, "get_llm", return_value=composer), \
         patch.object(brief_reviewer_mod, "get_llm", return_value=reviewer), \
         patch.object(conflict_analyst_mod, "get_llm", return_value=analyst), \
         patch.object(llm_reasoning, "TemplateClient", EmptyTemplateClient):
        return llm_reasoning._compose_pipeline(dict(payload))


# ---------------------------------------------------------------------------
# Structural tests (scripted LLMs — always run)
# ---------------------------------------------------------------------------


def test_directives_survive_internal_review_loop():
    """THE F18 regression test: after the internal reviewer REVISEs once, the
    second composer iteration must still see the clinician-directed changes
    (instruction + conflicts + suggestions). The original bug piggybacked them
    on brief_review_feedback, which the reviewer overwrote after iteration 1."""
    composer = ScriptedLLM(REVISED_BRIEF_JSON, REVISED_BRIEF_JSON)
    reviewer = ScriptedLLM(REVISE_ONCE, APPROVE)
    analyst = ScriptedLLM(ANALYST_RESOLVED)

    result = _run_pipeline(REVISION_PAYLOAD, composer, reviewer, analyst)

    assert len(composer.calls) == 2, "internal REVISE should trigger a second composer iteration"
    for call in range(2):
        prompt = composer.user_prompt(call)
        assert "## Clinician-directed changes (MANDATORY" in prompt, f"iteration {call + 1} lost the directives"
        assert "resolve all conflicts according to suggestions" in prompt
        assert "conf-bp000001" in prompt and "conf-diet0001" in prompt
        assert "Prefer the diabetes guideline's <130/80 target" in prompt
        assert "Combine into one cardiometabolic diet activity" in prompt
    # Iteration 2 also carries the reviewer's CURRENT issue — both channels present.
    assert "tighten monitoring interval" in composer.user_prompt(1)
    # Revision system prompt selected (not authoring).
    assert "Revising an existing care plan" in composer.system_prompt(0)
    assert "Preserving conflicts between guidelines" not in composer.system_prompt(0)

    brief = result["planning_brief"]
    assert brief.get("review_status") != "flagged"
    assert {c["id"]: c["status"] for c in brief["conflicts"]} == {
        "conf-bp000001": "resolved", "conf-diet0001": "resolved",
    }
    # F17b: the clinician round is recorded on the brief.
    assert brief["revision_history"][0]["comment"] == "resolve all conflicts according to suggestions"


def test_base_evolves_to_latest_draft():
    """F18b: iteration 2's Care Plan Base is iteration 1's OUTPUT (merged diet,
    single BP goal), not the frozen prior brief — reviewer-driven iterations
    must not revert clinician-applied changes."""
    composer = ScriptedLLM(REVISED_BRIEF_JSON, REVISED_BRIEF_JSON)
    reviewer = ScriptedLLM(REVISE_ONCE, APPROVE)
    analyst = ScriptedLLM(ANALYST_RESOLVED)

    _run_pipeline(REVISION_PAYLOAD, composer, reviewer, analyst)

    first = composer.user_prompt(0)
    second = composer.user_prompt(1)
    # Iteration 1 bases on the prior brief (both diet activities present).
    assert "Reduced-sodium balanced diet" in first
    assert "Diabetes meal planning, limit refined carbs" in first
    # Iteration 2 bases on the draft: merged activity present, originals gone
    # from the base section, and only one BP goal remains.
    base2 = second.split("## Care Plan Base")[1].split("## Clinician-directed changes")[0]
    assert "Combined cardiometabolic diet" in base2
    assert "Reduced-sodium balanced diet" not in base2
    assert "BP below 140/90" not in base2


def test_enforcement_retries_then_flags():
    """F18c: analyst reports directed-but-unapplied → one composer retry with an
    explicit enforcement note; still unapplied → brief flagged naming the ids."""
    composer = ScriptedLLM(UNREVISED_BRIEF_JSON)          # never applies the directives
    reviewer = ScriptedLLM(APPROVE)
    analyst = ScriptedLLM(ANALYST_STILL_DETECTED, ANALYST_STILL_DETECTED)

    result = _run_pipeline(REVISION_PAYLOAD, composer, reviewer, analyst)

    # Loop iteration + one enforcement retry.
    assert len(composer.calls) == 2
    assert len(analyst.calls) == 2
    retry_prompt = composer.user_prompt(1)
    assert "### NOT APPLIED in your previous attempt" in retry_prompt
    assert "conf-bp000001" in retry_prompt and "conf-diet0001" in retry_prompt

    brief = result["planning_brief"]
    assert brief["review_status"] == "flagged"
    assert "conf-bp000001" in brief["review_feedback"]
    assert "conf-diet0001" in brief["review_feedback"]


def test_enforcement_retry_succeeds_without_flagging():
    """F18c happy path: the retry applies the directives → resolved, no flag."""
    composer = ScriptedLLM(UNREVISED_BRIEF_JSON, REVISED_BRIEF_JSON)
    reviewer = ScriptedLLM(APPROVE)
    analyst = ScriptedLLM(ANALYST_STILL_DETECTED, ANALYST_RESOLVED)

    result = _run_pipeline(REVISION_PAYLOAD, composer, reviewer, analyst)

    assert len(composer.calls) == 2
    brief = result["planning_brief"]
    assert brief.get("review_status") != "flagged"
    assert all(c["status"] == "resolved" for c in brief["conflicts"])


def test_multi_round_resolved_stay_resolved():
    """Round 3: the prior brief carries one RESOLVED and one still-detected
    conflict. The directives section must list the resolved one under
    'keep resolved' (not as a referent), and the feedback history must carry
    both clinician rounds."""
    prior_round2 = dict(PRIOR_BRIEF)
    prior_round2["goals"] = [GOAL_BP_MERGED, ]
    prior_round2["activities"] = [ACT_DIET_HTN, ACT_DIET_DM2, ACT_HBA1C]
    prior_round2["conflicts"] = [
        {**PRIOR_CONFLICTS[0], "status": "resolved",
         "resolution": "Resolved as suggested — kept the 130/80 goal"},
        PRIOR_CONFLICTS[1],  # diet overlap still detected
    ]
    payload = dict(REVISION_PAYLOAD)
    payload["prior_brief"] = prior_round2
    payload["careplan_feedback"] = "now merge the diet activities as suggested"
    payload["careplan_review_history"] = REVISION_PAYLOAD["careplan_review_history"] + [
        {"decision": "request_changes", "comment": "now merge the diet activities as suggested",
         "clinician": "Demo Clinician", "completed_at": "2026-08-27T21:00:00Z"},
    ]

    composer = ScriptedLLM(REVISED_BRIEF_JSON)
    reviewer = ScriptedLLM(APPROVE)
    analyst = ScriptedLLM(_analyst_json([
        {**PRIOR_CONFLICTS[0], "status": "resolved", "clinician_directed": False,
         "resolution": "Resolved as suggested — kept the 130/80 goal",
         "goal_indices": [], "activity_indices": []},
        {**PRIOR_CONFLICTS[1], "status": "resolved", "clinician_directed": True,
         "resolution": "Merged as directed", "goal_indices": [], "activity_indices": []},
    ]))

    result = _run_pipeline(payload, composer, reviewer, analyst)

    prompt = composer.user_prompt(0)
    directives = prompt.split("## Clinician-directed changes")[1].split("## Feedback history")[0]
    assert "### Resolved in earlier rounds" in directives
    assert "kept the 130/80 goal" in directives
    referents = directives.split("### Unresolved conflicts")[1].split("### Resolved in earlier rounds")[0]
    assert "conf-diet0001" in referents
    assert "conf-bp000001" not in referents
    # Both rounds in the history, newest marked current.
    assert "resolve all conflicts according to suggestions" in prompt
    assert "now merge the diet activities as suggested" in prompt
    assert "(address THIS round now)" in prompt
    # Both rounds recorded on the brief.
    assert len(result["planning_brief"]["revision_history"]) == 2


def test_authoring_pass_untouched():
    """Control: no prior brief → authoring prompt, no directives, no enforcement."""
    payload = {k: v for k, v in REVISION_PAYLOAD.items()
               if k not in ("prior_brief", "prior_brief_ref", "careplan_feedback",
                            "careplan_review_history")}
    composer = ScriptedLLM(UNREVISED_BRIEF_JSON)
    reviewer = ScriptedLLM(APPROVE)
    analyst = ScriptedLLM(_analyst_json([
        {**PRIOR_CONFLICTS[0], "goal_indices": [0, 1], "activity_indices": []},
    ]))

    result = _run_pipeline(payload, composer, reviewer, analyst)

    assert len(composer.calls) == 1
    prompt = composer.user_prompt(0)
    assert "Clinician-directed changes" not in prompt
    assert "Care Plan Base" not in prompt
    assert "Preserving conflicts between guidelines" in composer.system_prompt(0)
    assert len(analyst.calls) == 1  # no enforcement pass
    assert result["planning_brief"].get("review_status") != "flagged"


# ---------------------------------------------------------------------------
# Live LLM layer (gated; also the interactive local driver)
# ---------------------------------------------------------------------------

_LLM_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("LITELLM_URL")


def _fixture_payload() -> dict:
    """Round-1 authoring payload built from the two fixture guidelines."""
    recs: list[dict] = []
    cpgs: list[dict] = []
    for name in ("htn-cpg.json", "diabetes-cpg.json"):
        doc = json.loads((FIXTURES / name).read_text())
        cpgs.append(doc["metadata"])
        recs.extend(doc["recommendation_bundle"]["recommendations"])
    return {
        "patient_reference": "Patient/robert",
        "patient_demographics": {"name": "Robert Thompson", "gender": "male", "birth_date": "1958-11-30"},
        "condition_codes": [
            {"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"},
            {"system": "http://snomed.info/sct", "code": "44054006", "display": "Type 2 diabetes mellitus"},
        ],
        "medication_codes": [
            {"system": "rxnorm", "code": "314076", "display": "Lisinopril 10 MG Oral Tablet"},
        ],
        "allergy_codes": [],
        "dmn_results": [],
        "recommendations": recs,
        "applicable_cpgs": cpgs,
    }


def _run_live_round(payload: dict) -> dict:
    with patch.object(llm_reasoning, "_store", None), \
         patch.object(llm_reasoning, "_phi_store", None), \
         patch.object(llm_reasoning, "LITELLM_URL", _LLM_URL), \
         patch.object(llm_reasoning, "LLM_MODEL", os.environ.get("LLM_MODEL", "")):
        return llm_reasoning._compose_pipeline(dict(payload))


def _conflict_table(brief: dict) -> str:
    rows = []
    for c in brief.get("conflicts", []):
        rows.append(f"  [{c['id']}] {c['category']:<17} {c['status']:<9} {c['description'][:70]}")
        if c.get("suggested_resolution"):
            rows.append(f"      suggested: {c['suggested_resolution'][:80]}")
        if c.get("resolution"):
            rows.append(f"      resolution: {c['resolution'][:80]}")
    return "\n".join(rows) or "  (none)"


@pytest.mark.skipif(not _LLM_URL, reason="requires LLM_BASE_URL (or LITELLM_URL)")
def test_live_two_rounds():
    """Full live flow: author → clinician 'resolve as suggested' → revision.
    Asserts the pipeline's contract, not exact wording: round 2 must leave no
    clinician-directed conflict silently detected — each prior conflict is
    resolved, or the brief is honestly flagged."""
    r1 = _run_live_round(_fixture_payload())
    brief1 = r1["planning_brief"]
    print("\n== Round 1 (authoring):", len(brief1["goals"]), "goals,",
          len(brief1["activities"]), "activities")
    print(_conflict_table(brief1))
    assert brief1["conflicts"], "expected the fixture guidelines to produce conflicts"

    comment = "resolve all conflicts according to suggestions"
    payload2 = _fixture_payload()
    payload2.update({
        "careplan_feedback": comment,
        "careplan_review_history": [
            {"decision": "request_changes", "comment": comment, "clinician": "Demo Clinician"},
        ],
        "prior_brief": brief1,
        "prior_brief_ref": "",
    })
    r2 = _run_live_round(payload2)
    brief2 = r2["planning_brief"]
    print("== Round 2 (revision):", len(brief2["goals"]), "goals,",
          len(brief2["activities"]), "activities; review_status:",
          brief2.get("review_status"))
    print(_conflict_table(brief2))

    prior_ids = {c["id"] for c in brief1["conflicts"]}
    flagged = brief2.get("review_status") == "flagged"
    for c in brief2["conflicts"]:
        if c["id"] in prior_ids and c["status"] == "detected":
            assert flagged, (
                f"prior conflict {c['id']} still detected but brief not flagged — "
                "the directed resolution was silently dropped (F18 regression)"
            )
    resolved = [c for c in brief2["conflicts"] if c["status"] == "resolved"]
    assert resolved or flagged, "round 2 neither resolved anything nor flagged the brief"


def main() -> int:
    if not _LLM_URL:
        print("Set LLM_BASE_URL (or LITELLM_URL), LLM_MODEL, LLM_API_KEY first.")
        return 1
    test_live_two_rounds()
    print("\nLive two-round revision flow: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
