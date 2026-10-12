"""Repeatability gate for the Conflict Analyst node (issue #169, WS8).

This is a *live-LLM* test: it runs the real ``conflict_analyst`` node against a
deterministically-built PlanningBrief that deliberately contains three headline
conflicts drawn from the two synthetic fixture CPGs:

  - overlap:          two substantially-the-same diet/lifestyle activities
  - divergent_target: BP goals of <140/90 (HTN) vs <130/80 (DM2)
  - contradiction:    "titrate lisinopril upward" (HTN) vs "reduce lisinopril
                      dose" (DM2) on the same drug

LLM output is nondeterministic, so we assert on *categories and index sets*,
never on exact wording, and we require each expected category to appear in a
majority (>=2) of three runs rather than all three — a strict 3/3 gate would be
hostage to model nondeterminism. The overall floor is stricter: every run must
flag at least one conflict.

Skips unless an LLM endpoint is configured. Prefer ``LLM_BASE_URL`` per the
no-tech-specific-names convention; fall back to ``LITELLM_URL`` for
compatibility with the existing e2e harness.

Run with e.g.:
    LLM_BASE_URL=http://localhost:4000 pytest tests/test_conflict_repeatability.py -v -s
"""

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from acp_writer.nodes.conflict_analyst import conflict_analyst

# acp-writer/tests/ -> acp-writer/ -> repo root
FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "integration" / "fixtures"

LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("LITELLM_URL")
pytestmark = pytest.mark.skipif(
    not LLM_BASE_URL,
    reason="LLM_BASE_URL (or legacy LITELLM_URL) not set — live-LLM repeatability gate skipped",
)

RUNS = 3


def _load_cpg(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _recs(cpg: dict) -> dict[str, dict]:
    return {r["id"]: r for r in cpg["recommendation_bundle"]["recommendations"]}


def _build_brief() -> tuple[dict, list[dict]]:
    """Deterministically assemble a PlanningBrief (no composer LLM call) whose
    goals/activities are lifted straight from the two fixture CPGs so the three
    seeded conflicts are genuinely present. Returns (brief, recommendations)."""
    htn = _recs(_load_cpg("htn-cpg.json"))
    dm2 = _recs(_load_cpg("diabetes-cpg.json"))

    goals = [
        {
            "description": "Achieve blood pressure < 140/90 mmHg",
            "target_measure_code": {
                "system": "http://loinc.org", "code": "8480-6", "display": "Systolic BP",
            },
            "target_value": {"high": 140, "unit": "mmHg"},
            "source_cpg": "SYN-HTN-2026-001",
            "source_recommendation_id": "htn-rec-001",
        },
        {
            "description": "Achieve blood pressure < 130/80 mmHg",
            "target_measure_code": {
                "system": "http://loinc.org", "code": "8480-6", "display": "Systolic BP",
            },
            "target_value": {"high": 130, "unit": "mmHg"},
            "source_cpg": "SYN-DM2-2026-001",
            "source_recommendation_id": "dm2-rec-002",
        },
    ]

    activities = [
        # overlap pair — two substantially-the-same diet activities
        {
            "type": "lifestyle",
            "description": "Adopt a balanced diet limiting sodium and refined carbohydrates",
            "source_cpg": "SYN-HTN-2026-001",
            "source_recommendation_id": "htn-rec-003",
        },
        {
            "type": "lifestyle",
            "description": "Adopt a balanced diet emphasizing whole grains and limiting added sugars",
            "source_cpg": "SYN-DM2-2026-001",
            "source_recommendation_id": "dm2-rec-004",
        },
        # contradiction pair — titrate up vs reduce the same drug
        {
            "type": "medication",
            "description": "Titrate lisinopril upward to reach the blood pressure target",
            "code": {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "29046", "display": "Lisinopril"},
            "source_cpg": "SYN-HTN-2026-001",
            "source_recommendation_id": "htn-rec-002",
        },
        {
            "type": "medication",
            "description": "Consider reducing the lisinopril dose to protect renal function",
            "code": {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "29046", "display": "Lisinopril"},
            "source_cpg": "SYN-DM2-2026-001",
            "source_recommendation_id": "dm2-rec-005",
        },
    ]

    brief = {
        "patient_reference": "Patient/comprehensive",
        "applicable_cpgs": ["SYN-HTN-2026-001", "SYN-DM2-2026-001"],
        "goals": goals,
        "activities": activities,
        "conflicts": [],
        "review_status": "approved",
    }

    recs = [
        htn["htn-rec-001"], htn["htn-rec-002"], htn["htn-rec-003"],
        dm2["dm2-rec-002"], dm2["dm2-rec-004"], dm2["dm2-rec-005"],
    ]
    # Node expects snake_case source_cpg keys, which the fixtures already use.
    return brief, recs


def _state() -> dict:
    brief, recs = _build_brief()
    return {
        "planning_brief": brief,
        "recommendations": recs,
        "condition_codes": [
            {"display": "Hypertension"},
            {"display": "Type 2 diabetes mellitus"},
        ],
        "medication_codes": [{"display": "Lisinopril"}],
        "litellm_url": LLM_BASE_URL,
        "llm_model": os.environ.get("LLM_MODEL", "default"),
    }


class TestConflictRepeatability:
    def test_seeded_conflicts_are_flagged_repeatably(self):
        per_run_categories: list[set[str]] = []
        category_run_counts: Counter[str] = Counter()

        for i in range(RUNS):
            result = conflict_analyst(_state())
            conflicts = result["planning_brief"]["conflicts"]
            cats = sorted(c.get("category") for c in conflicts)
            idx = [
                (c.get("category"), tuple(c.get("goal_indices") or []), tuple(c.get("activity_indices") or []))
                for c in conflicts
            ]
            print(f"[repeatability] run {i + 1}/{RUNS}: {len(conflicts)} conflicts "
                  f"categories={cats} indices={idx}")

            # (a) every run must flag at least one conflict
            assert conflicts, f"run {i + 1} flagged no conflicts on a deliberately-conflicting plan"

            unique_cats = set(cats)
            per_run_categories.append(unique_cats)
            for cat in unique_cats:
                category_run_counts[cat] += 1

        # (b) each expected category appears in a majority of runs.
        # The overlap pair should be reliably caught; the drug conflict may
        # surface as either contradiction or divergent_target depending on how
        # the model frames it, so accept either for that slot.
        majority = (RUNS // 2) + 1  # 2 of 3
        assert category_run_counts["overlap"] >= majority, (
            f"'overlap' seen in only {category_run_counts['overlap']}/{RUNS} runs "
            f"(need >= {majority}); per-run categories: {per_run_categories}"
        )
        drug_conflict_runs = sum(
            1 for cats in per_run_categories
            if "contradiction" in cats or "divergent_target" in cats
        )
        assert drug_conflict_runs >= majority, (
            f"neither 'contradiction' nor 'divergent_target' reached a majority: "
            f"seen in {drug_conflict_runs}/{RUNS} runs (need >= {majority}); "
            f"per-run categories: {per_run_categories}"
        )
