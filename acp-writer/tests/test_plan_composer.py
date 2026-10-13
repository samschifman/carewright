"""Tests for the Plan Composer node."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from acp_writer.nodes.plan_composer import (
    _ensure_clinical_follow_up,
    _format_conditions,
    _format_demographics,
    _format_recommendations,
    _merge_goals_for_merged_regimens,
    _merge_same_cpg_medication_regimens,
    _resolve_missing_medication_codes,
    _neutralize_unprovided_pronouns,
    _normalize_source_bound_safety_text,
    _parse_brief_from_response,
    _sanitize_provenance,
    plan_composer,
)
from acp_writer.planning_brief import PlanningBrief

SAMPLE_BRIEF_JSON = json.dumps({
    "patient_reference": "Patient/patient-1",
    "applicable_cpgs": ["SYN-HTN-2026-001"],
    "dmn_audit_trail": [],
    "goals": [
        {
            "description": "Lower blood pressure to target range",
            "target_measure_code": {"system": "http://loinc.org", "code": "8480-6", "display": "Systolic BP"},
            "target_value": {"high": 140, "unit": "mmHg"},
            "source_recommendation_id": "rec-123",
            "source_cpg": "SYN-HTN-2026-001",
        }
    ],
    "activities": [
        {
            "type": "medication",
            "description": "Start Lisinopril 10mg daily",
            "code": {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "29046", "display": "Lisinopril"},
            "dose": "10 mg",
            "route": "oral",
            "frequency": "daily",
            "source_recommendation_id": "rec-456",
            "source_cpg": "SYN-HTN-2026-001",
            "source_dmn_call": 0,
            "clinical_rationale": "ACE inhibitor for hypertension with diabetes",
            "workflow": {
                "actor": "prescribing_physician",
                "escalation": "If BP not at target after 4 weeks, increase dose",
                "monitoring_trigger": "BMP in 4 weeks",
            },
        },
        {
            "type": "lifestyle",
            "description": "DASH diet",
            "source_recommendation_id": "rec-789",
            "source_cpg": "SYN-HTN-2026-001",
        },
    ],
    "conflicts": [],
    "review_status": "pending",
})


class TestFormatHelpers:
    def test_format_conditions(self):
        codes = [
            {"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"},
            {"system": "http://snomed.info/sct", "code": "44054006", "display": "Type 2 diabetes"},
        ]
        result = _format_conditions(codes)
        assert "Essential hypertension" in result
        assert "Type 2 diabetes" in result

    def test_format_conditions_empty(self):
        assert "No conditions" in _format_conditions([])

    def test_format_demographics(self):
        demo = {"name": "James Reynolds", "gender": "male", "birth_date": "1971-03-15"}
        result = _format_demographics(demo)
        assert "James Reynolds" in result
        assert "male" in result

    def test_format_demographics_empty(self):
        assert _format_demographics({}) == "Unknown"

    def test_format_recommendations(self):
        recs = [{"id": "r1", "title": "Start ACE inhibitor", "content": "...", "recommendation_type": "treatment", "source_cpg": "X"}]
        result = _format_recommendations(recs)
        assert "Start ACE inhibitor" in result

    def test_format_recommendations_empty(self):
        assert "No recommendations" in _format_recommendations([])


class TestParseBrief:
    def test_plain_json(self):
        data = _parse_brief_from_response(SAMPLE_BRIEF_JSON)
        assert data["patient_reference"] == "Patient/patient-1"

    def test_markdown_code_block(self):
        wrapped = f"```json\n{SAMPLE_BRIEF_JSON}\n```"
        data = _parse_brief_from_response(wrapped)
        assert data["patient_reference"] == "Patient/patient-1"

    def test_validates_as_planning_brief(self):
        data = _parse_brief_from_response(SAMPLE_BRIEF_JSON)
        brief = PlanningBrief.model_validate(data)
        assert len(brief.goals) == 1
        assert len(brief.activities) == 2


class TestSanitizeProvenance:
    def test_defaults_null_and_missing_source_cpg(self):
        # The LLM emits source_cpg: null (or omits it) for lifestyle/general
        # items. Without sanitizing, a single null fails PlanningBrief
        # validation and the whole plan is dropped.
        data = {
            "patient_reference": "Patient/patient-1",
            "applicable_cpgs": ["AHA-HTN-2023"],
            "goals": [{"description": "Lower systolic BP", "source_cpg": None}],
            "activities": [
                {"type": "lifestyle", "description": "DASH diet"},  # source_cpg missing
                {"type": "medication", "description": "Optimize amlodipine", "source_cpg": None},
            ],
            "conflicts": [],
            "review_status": "pending",
        }
        _sanitize_provenance(data, "AHA-HTN-2023")
        brief = PlanningBrief.model_validate(data)  # must not raise
        assert len(brief.goals) == 1
        assert len(brief.activities) == 2
        assert brief.goals[0].source_cpg == "AHA-HTN-2023"
        assert all(a.source_cpg == "AHA-HTN-2023" for a in brief.activities)

    def test_preserves_existing_source_cpg(self):
        data = {"goals": [{"description": "g", "source_cpg": "SYN-HTN-2026-001"}], "activities": []}
        _sanitize_provenance(data, "AHA-HTN-2023")
        assert data["goals"][0]["source_cpg"] == "SYN-HTN-2026-001"


class TestDeterministicClinicalNormalization:
    def _duplicate_regimen_brief(self):
        return {
            "goals": [
                {"description": "Initiate lisinopril for confirmed high BP.", "source_recommendation_id": "rec-a", "source_cpg": "SYN-HTN-2026-002"},
                {"description": "Initiate lisinopril for high BP with diabetes.", "source_recommendation_id": "rec-b", "source_cpg": "SYN-HTN-2026-002"},
            ],
            "activities": [
                {"type": "medication", "description": "Start lisinopril 10 mg after confirmed BP.", "dose": "10 mg", "route": "oral", "frequency": "daily", "source_recommendation_id": "rec-a", "source_cpg": "SYN-HTN-2026-002"},
                {"type": "medication", "description": "Start lisinopril 10 mg when BP and diabetes criteria are met.", "dose": "10 mg", "route": "oral", "frequency": "daily", "source_recommendation_id": "rec-b", "source_cpg": "SYN-HTN-2026-002"},
                {"type": "lifestyle", "description": "DASH diet", "source_recommendation_id": "rec-c", "source_cpg": "SYN-HTN-2026-002"},
            ],
        }

    @patch("acp_writer.nodes.plan_composer.resolve")
    def test_same_cpg_rows_use_resolver_and_preserve_sources(self, resolver):
        resolver.return_value = SimpleNamespace(
            action="extract_medication", system="resolver-system", code="resolver-code"
        )
        brief = self._duplicate_regimen_brief()
        _resolve_missing_medication_codes(brief)
        assert resolver.call_count == 2
        groups = _merge_same_cpg_medication_regimens(brief)
        _merge_goals_for_merged_regimens(brief, groups)

        meds = [a for a in brief["activities"] if a["type"] == "medication"]
        assert len(meds) == 1
        assert meds[0]["code"] == {"system": "resolver-system", "code": "resolver-code"}
        assert meds[0]["source_recommendation_ids"] == ["rec-a", "rec-b"]
        assert "either applicable same-CPG decision condition" in meds[0]["description"]
        assert len(brief["goals"]) == 1
        assert brief["goals"][0]["source_recommendation_ids"] == ["rec-a", "rec-b"]

    def test_treatment_dmn_adds_clinical_visit_separate_from_home_review(self):
        brief = self._duplicate_regimen_brief()
        _resolve_missing_medication_codes(brief)
        _merge_same_cpg_medication_regimens(brief)
        _ensure_clinical_follow_up(brief, [{
            "model_id": "treatment-recommendation",
            "outputs": {"Treatment Recommendation": {"Action": "Start medication", "Follow Up Weeks": 2}},
        }])
        followups = [a for a in brief["activities"] if "clinical follow-up visit" in a["description"]]
        assert len(followups) == 1
        assert followups[0]["frequency"] == "2 weeks"
        assert "separate from home-average review" in followups[0]["description"]

    @patch("acp_writer.nodes.plan_composer.resolve", return_value=None)
    def test_unresolved_medication_code_is_left_absent(self, resolver):
        brief = {"activities": [{"type": "medication", "description": "Unrecognized medicine"}]}
        _resolve_missing_medication_codes(brief)
        resolver.assert_called_once_with("Unrecognized medicine")
        assert "code" not in brief["activities"][0]

    def test_sex_specific_alcohol_and_severe_bp_safety_text_are_explicit(self):
        brief = {"activities": [
            {"type": "lifestyle", "description": "Limit alcohol intake to no more than 2 drinks per day.", "source_recommendation_id": "alc", "source_cpg": "SYN"},
            {"type": "monitoring", "description": "Notify on a single reading above 180/120 mmHg.", "source_recommendation_id": "bp", "source_cpg": "SYN"},
        ]}
        recs = [
            {"id": "alc", "content": "Limit alcohol to 2 drinks per day for men and 1 for women."},
            {"id": "bp", "content": "A single reading above 180/120 mmHg requires a repeat."},
        ]
        _normalize_source_bound_safety_text(brief, recs)
        alcohol, severe_bp = brief["activities"]
        assert "2 drinks per day for men and 1 drink per day for women" in alcohol["description"]
        assert "confirm which limit applies" in alcohol["description"]
        assert "emergency services immediately" in severe_bp["description"]
        assert "repeat the reading after five minutes" in severe_bp["description"]
        assert "American Heart Association" in severe_bp["clinical_rationale"]

    def test_unknown_patient_pronouns_are_neutralized(self):
        brief = {
            "goals": [],
            "activities": [{
                "description": "Inform the patient and involve him in shared decisions.",
                "clinical_rationale": "His preferences should guide the discussion.",
                "workflow": {"escalation": "Ask him to contact the clinic."},
            }],
        }
        _neutralize_unprovided_pronouns(brief, {})
        activity = brief["activities"][0]
        assert "involve the patient" in activity["description"]
        assert "the patient's preferences" in activity["clinical_rationale"]
        assert "Ask the patient" in activity["workflow"]["escalation"]

    def test_explicit_patient_pronouns_are_preserved(self):
        brief = {"goals": [], "activities": [{"description": "Involve him in decisions."}]}
        _neutralize_unprovided_pronouns(brief, {"pronouns": "he/him"})
        assert brief["activities"][0]["description"] == "Involve him in decisions."


class TestPlanComposer:
    def _make_state(self) -> dict:
        return {
            "patient_reference": "Patient/patient-1",
            "patient_demographics": {"name": "James Reynolds", "gender": "male", "birth_date": "1971-03-15"},
            "condition_codes": [{"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"}],
            "dmn_results": [{
                "model_id": "treatment-recommendation",
                "model_name": "Treatment Recommendation",
                "inputs": {"Systolic BP": 142},
                "outputs": {"Treatment Recommendation": {"Action": "Start medication", "Medication": "Lisinopril"}},
                "fhir_references": ["Observation/bp-1"],
                "timestamp": "2026-07-21T10:00:00Z",
            }],
            "recommendations": [
                {"id": "rec-123", "title": "BP Target", "content": "Target < 140 mmHg", "recommendation_type": "treatment", "source_cpg": "SYN-HTN-2026-001"},
                {"id": "rec-456", "title": "First-line medication", "content": "Lisinopril 10mg", "recommendation_type": "treatment", "source_cpg": "SYN-HTN-2026-001"},
                {"id": "rec-789", "title": "DASH diet", "content": "Adopt DASH diet", "recommendation_type": "lifestyle", "source_cpg": "SYN-HTN-2026-001"},
            ],
            "applicable_cpgs": [{"cpg_id": "SYN-HTN-2026-001"}],
            "litellm_url": "http://localhost:4000",
            "llm_model": "default",
        }

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_produces_valid_brief(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = plan_composer(self._make_state())

        assert "planning_brief" in result
        brief = PlanningBrief.model_validate(result["planning_brief"])
        assert len(brief.goals) >= 1
        assert len(brief.activities) >= 1
        assert brief.activities[0].source_cpg == "SYN-HTN-2026-001"

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_null_source_cpg_does_not_empty_the_plan(self, mock_get_llm):
        # Regression: on a request_changes regeneration the LLM emitted
        # source_cpg: null for some items, which failed PlanningBrief validation
        # and produced an empty plan (0 goals / 0 activities). Sanitizing must
        # keep the items instead of dropping the whole brief.
        brief_with_nulls = json.dumps({
            "patient_reference": "Patient/patient-1",
            "applicable_cpgs": ["SYN-HTN-2026-001"],
            "goals": [{"description": "Lower systolic BP to target", "source_cpg": None}],
            "activities": [
                {"type": "medication", "description": "Optimize amlodipine", "source_cpg": None},
                {"type": "lifestyle", "description": "DASH diet, sodium <2300mg"},
            ],
            "conflicts": [],
            "review_status": "pending",
        })
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = brief_with_nulls
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = plan_composer(self._make_state())
        brief = PlanningBrief.model_validate(result["planning_brief"])
        assert len(brief.goals) == 1
        assert len(brief.activities) == 2
        assert brief.goals[0].source_cpg == "SYN-HTN-2026-001"

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_clears_review_feedback(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = plan_composer(self._make_state())
        assert result["brief_review_feedback"] == ""

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_handles_parse_error(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "I cannot create a care plan because..."
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = plan_composer(self._make_state())
        assert result["planning_brief"]["review_status"] == "flagged"
        assert "parse error" in result["planning_brief"]["review_feedback"].lower()

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_includes_feedback_in_prompt(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = self._make_state()
        state["brief_review_feedback"] = "Missing monitoring activity for BMP"
        plan_composer(state)

        call_args = mock_llm.invoke.call_args[0][0]
        user_msg = call_args[1]["content"]
        assert "Missing monitoring activity" in user_msg

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_dmn_audit_trail_injected_from_state_not_llm(self, mock_get_llm):
        # F10: the DMN audit trail is authoritative data the executor already
        # built. It is injected into the brief in code, not echoed back through
        # the LLM prompt. Even though SAMPLE_BRIEF_JSON carries an empty
        # dmn_audit_trail, the composed brief must reflect state["dmn_results"].
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON  # has "dmn_audit_trail": []
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = self._make_state()
        result = plan_composer(state)

        trail = result["planning_brief"]["dmn_audit_trail"]
        assert len(trail) == len(state["dmn_results"]) == 1
        assert trail[0]["model_id"] == "treatment-recommendation"

        # And the prompt no longer serializes the audit trail into the LLM input.
        user_prompt = result["plan_composer_prompt"]
        assert "dmn_audit_trail" not in user_prompt

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_authoring_mode_when_no_prior_brief(self, mock_get_llm):
        # F17a: with no prior_planning_brief the composer authors from scratch —
        # authoring system prompt, and no "Prior Care Plan" block in the user
        # prompt. This is the monolith / first-pass path.
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = plan_composer(self._make_state())

        call_args = mock_llm.invoke.call_args[0][0]
        system_msg = call_args[0]["content"]
        user_msg = call_args[1]["content"]
        assert "Preserving conflicts between guidelines" in system_msg
        assert "Revising an existing care plan" not in system_msg
        assert "Care Plan Base" not in user_msg
        # user prompt is captured verbatim
        assert "Care Plan Base" not in result["plan_composer_prompt"]

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_revision_mode_when_prior_brief_present(self, mock_get_llm):
        # F17a: a prior_planning_brief with goals/activities flips the composer to
        # revision mode — revision system prompt, and the prior plan rendered as
        # the authoritative base in the user prompt.
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = self._make_state()
        state["prior_planning_brief"] = {
            "goals": [{"description": "Lower systolic BP to <140", "source_cpg": "SYN-HTN-2026-001"}],
            "activities": [{"type": "medication", "description": "Amlodipine 5mg daily",
                            "source_cpg": "SYN-HTN-2026-001"}],
            "conflicts": [],
        }
        result = plan_composer(state)

        call_args = mock_llm.invoke.call_args[0][0]
        system_msg = call_args[0]["content"]
        user_msg = call_args[1]["content"]
        assert "Revising an existing care plan" in system_msg
        assert "Preserving conflicts between guidelines" not in system_msg
        assert "Care Plan Base" in user_msg
        # prior goals/activities are rendered as the base
        assert "Lower systolic BP to <140" in user_msg
        assert "Amlodipine 5mg daily" in user_msg
        assert "Care Plan Base" in result["plan_composer_prompt"]

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_empty_prior_brief_stays_authoring(self, mock_get_llm):
        # An empty prior brief (no goals, no activities) is NOT a revision — e.g.
        # a prior first pass that produced nothing. Stay in authoring mode.
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = self._make_state()
        state["prior_planning_brief"] = {"goals": [], "activities": [], "conflicts": []}
        plan_composer(state)

        system_msg = mock_llm.invoke.call_args[0][0][0]["content"]
        assert "Preserving conflicts between guidelines" in system_msg
        assert "Revising an existing care plan" not in system_msg

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_feedback_history_threads_into_prompt_and_brief(self, mock_get_llm):
        # F17b: accumulated review history reaches the composer prompt oldest-first
        # and is recorded on the brief's revision_history for the audit trail.
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = self._make_state()
        state["careplan_review_history"] = [
            {"decision": "request_changes", "comment": "never add opioids",
             "clinician": {"display": "Dr. A"}},
            {"decision": "request_changes", "comment": "merge the diet activities",
             "clinician": {"display": "Dr. B"}},
        ]
        result = plan_composer(state)

        user_msg = mock_llm.invoke.call_args[0][0][1]["content"]
        assert "## Feedback history (oldest first)" in user_msg
        # oldest-first and newest marked current
        assert user_msg.index("never add opioids") < user_msg.index("merge the diet activities")
        assert "address THIS round now" in user_msg
        # recorded on the brief
        history = result["planning_brief"]["revision_history"]
        assert [r["comment"] for r in history] == ["never add opioids", "merge the diet activities"]

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_no_feedback_history_when_absent(self, mock_get_llm):
        # First pass / monolith: no history → no history block, empty revision_history.
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = plan_composer(self._make_state())
        user_msg = mock_llm.invoke.call_args[0][0][1]["content"]
        assert "## Feedback history" not in user_msg
        assert result["planning_brief"].get("revision_history", []) == []

    @patch("acp_writer.nodes.plan_composer.get_llm")
    def test_writes_artifact(self, mock_get_llm, tmp_path):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = SAMPLE_BRIEF_JSON
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = self._make_state()
        state["output_dir"] = str(tmp_path)
        plan_composer(state)

        artifact = tmp_path / "planning-brief.json"
        assert artifact.exists()
        data = json.loads(artifact.read_text())
        assert data["patient_reference"] == "Patient/patient-1"
