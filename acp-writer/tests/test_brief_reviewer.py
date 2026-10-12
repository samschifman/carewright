"""Tests for the Brief Reviewer node."""

import json
from unittest.mock import MagicMock, patch

import pytest

from acp_writer.nodes.brief_reviewer import (
    _schema_validate,
    brief_reviewer,
)
from acp_writer.planning_brief import ReviewStatus


def _valid_brief() -> dict:
    return {
        "patient_reference": "Patient/patient-1",
        "applicable_cpgs": ["SYN-HTN-2026-001"],
        "dmn_audit_trail": [],
        "goals": [
            {
                "description": "Lower BP",
                "target_measure_code": {"system": "http://loinc.org", "code": "8480-6"},
                "target_value": {"high": 140, "unit": "mmHg"},
                "source_cpg": "SYN-HTN-2026-001",
            }
        ],
        "activities": [
            {
                "type": "medication",
                "description": "Lisinopril 10mg daily",
                "dose": "10 mg",
                "route": "oral",
                "frequency": "daily",
                "source_recommendation_id": "rec-1",
                "source_cpg": "SYN-HTN-2026-001",
            }
        ],
        "conflicts": [],
        "review_status": "pending",
    }


def _make_state(brief: dict | None = None) -> dict:
    return {
        "planning_brief": brief or _valid_brief(),
        "brief_review_count": 0,
        "patient_reference": "Patient/patient-1",
        "condition_codes": [{"display": "Essential hypertension"}],
        "medication_codes": [],
        "allergy_codes": [],
        "recommendations": [
            {"id": "rec-1", "title": "BP Target", "recommendation_type": "treatment", "certainty": {"strength": "strong-for"}},
        ],
        "litellm_url": "http://localhost:4000",
        "llm_model": "default",
    }


class TestSchemaValidation:
    def test_valid_brief_passes(self):
        errors = _schema_validate(_valid_brief())
        assert errors == []

    def test_empty_brief_fails(self):
        errors = _schema_validate({
            "patient_reference": "Patient/1",
            "applicable_cpgs": [],
            "goals": [],
            "activities": [],
        })
        assert any("no goals" in e.lower() for e in errors)

    def test_missing_source_cpg(self):
        brief = _valid_brief()
        brief["activities"][0]["source_cpg"] = ""
        errors = _schema_validate(brief)
        assert any("source_cpg" in e for e in errors)

    def test_medication_without_dose(self):
        brief = _valid_brief()
        del brief["activities"][0]["dose"]
        errors = _schema_validate(brief)
        assert any("dose" in e.lower() for e in errors)


class TestBriefReviewer:
    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_approve(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "verdict": "APPROVE",
            "issues": [],
        })
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = brief_reviewer(_make_state())
        assert result["brief_review_feedback"] == ""
        assert result["brief_review_count"] == 1
        assert result["planning_brief"]["review_status"] == "approved"

    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_revise(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "verdict": "REVISE",
            "issues": [
                {"severity": "error", "description": "Missing monitoring for BMP", "fix": "Add BMP monitoring activity"},
            ],
        })
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = brief_reviewer(_make_state())
        assert result["brief_review_feedback"] != ""
        assert "Missing monitoring" in result["brief_review_feedback"]
        assert result["brief_review_count"] == 1
        assert result["planning_brief"]["review_status"] == "revised"

    def test_schema_validation_gate(self):
        """Schema errors caught without calling LLM."""
        brief = _valid_brief()
        brief["activities"][0]["source_cpg"] = ""
        state = _make_state(brief)

        result = brief_reviewer(state)
        assert "source_cpg" in result["brief_review_feedback"]
        assert result["brief_review_count"] == 1

    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_unparseable_response_treated_as_approve(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "The brief looks good overall."
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = brief_reviewer(_make_state())
        assert result["brief_review_feedback"] == ""
        assert result["brief_review_count"] == 1

    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_increments_review_count(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = _make_state()
        state["brief_review_count"] = 1
        result = brief_reviewer(state)
        assert result["brief_review_count"] == 2

    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_writes_review_artifact(self, mock_get_llm, tmp_path):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = _make_state()
        state["output_dir"] = str(tmp_path)
        brief_reviewer(state)

        artifact = tmp_path / "brief-review-1.json"
        assert artifact.exists()


class TestRevisionAwareness:
    """F17: on a request-changes revision the reviewer prompt carries the
    minimal-revision note so it doesn't demand authoring-style rewrites."""

    def _user_prompt(self, mock_llm) -> str:
        # The user message is the second entry passed to llm.invoke([...]).
        messages = mock_llm.invoke.call_args[0][0]
        return messages[1]["content"]

    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_authoring_has_no_revision_note(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        brief_reviewer(_make_state())
        prompt = self._user_prompt(mock_llm)
        assert "MINIMAL REVISION" not in prompt

    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_revision_includes_note(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = _make_state()
        state["prior_planning_brief"] = _valid_brief()
        brief_reviewer(state)
        prompt = self._user_prompt(mock_llm)
        assert "MINIMAL REVISION" in prompt

    @patch("acp_writer.nodes.brief_reviewer.get_llm")
    def test_empty_prior_brief_stays_authoring(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = _make_state()
        state["prior_planning_brief"] = {"goals": [], "activities": []}
        brief_reviewer(state)
        prompt = self._user_prompt(mock_llm)
        assert "MINIMAL REVISION" not in prompt
