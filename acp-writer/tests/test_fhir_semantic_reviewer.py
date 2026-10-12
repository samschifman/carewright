"""Tests for the FHIR Semantic Reviewer node."""

import json
from unittest.mock import MagicMock, patch

from acp_writer.nodes.fhir_semantic_reviewer import (
    _bundle_for_review,
    fhir_semantic_reviewer,
)


def _make_state(bundle: dict | None = None) -> dict:
    return {
        "fhir_bundle": bundle or {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [{"resource": {"resourceType": "CarePlan", "id": "cp-1"}}],
        },
        "fhir_review_count": 0,
        "syntax_errors": [],
        "terminology_issues": [],
        "litellm_url": "http://localhost:4000",
        "llm_model": "default",
    }


class TestFHIRSemanticReviewer:
    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_approve(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = fhir_semantic_reviewer(_make_state())
        assert result["fhir_review_feedback"] == ""
        assert result["fhir_review_count"] == 1

    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_revise(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "verdict": "REVISE",
            "issues": [{
                "severity": "error",
                "resource": "CarePlan/cp-1",
                "description": "Goal has no matching activity",
                "fix": "Add activity for BP goal",
            }],
        })
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = fhir_semantic_reviewer(_make_state())
        assert result["fhir_review_feedback"] != ""
        assert "Goal has no matching activity" in result["fhir_review_feedback"]
        assert result["fhir_review_count"] == 1

    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_unparseable_treated_as_approve(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "The bundle looks clinically sound."
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        result = fhir_semantic_reviewer(_make_state())
        assert result["fhir_review_feedback"] == ""

    def test_empty_bundle_auto_approves(self):
        state = _make_state({"resourceType": "Bundle", "type": "transaction", "entry": []})
        result = fhir_semantic_reviewer(state)
        assert result["fhir_review_feedback"] == ""
        assert result["fhir_review_count"] == 1

    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_increments_count(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = _make_state()
        state["fhir_review_count"] = 1
        result = fhir_semantic_reviewer(state)
        assert result["fhir_review_count"] == 2

    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_passes_validation_context(self, mock_get_llm):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = _make_state()
        state["syntax_errors"] = ["Missing AIAST tag"]
        state["terminology_issues"] = [{"code": "INVALID", "status": "invalid"}]
        fhir_semantic_reviewer(state)

        call_args = mock_llm.invoke.call_args[0][0]
        user_msg = call_args[1]["content"]
        assert "Missing AIAST" in user_msg
        assert "INVALID" in user_msg

    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_strips_attachment_data_from_review_prompt(self, mock_get_llm):
        # F10: DocumentReference attachment payloads (captured prompts, model
        # cards) are huge base64 blobs with no clinical signal. They must not
        # reach the reviewer LLM prompt — and the original bundle in state must
        # be left untouched.
        big_data = "QQ==" * 5000
        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {"resource": {"resourceType": "CarePlan", "id": "cp-1"}},
                {"resource": {
                    "resourceType": "DocumentReference",
                    "id": "prompt-1",
                    "content": [{"attachment": {"contentType": "text/plain", "data": big_data}}],
                }},
            ],
        }
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        fhir_semantic_reviewer(_make_state(bundle))

        user_msg = mock_llm.invoke.call_args[0][0][1]["content"]
        assert big_data not in user_msg
        assert "omitted for review" in user_msg
        # Original bundle in state is not mutated.
        assert bundle["entry"][1]["resource"]["content"][0]["attachment"]["data"] == big_data

    def test_bundle_for_review_leaves_non_docref_untouched(self):
        bundle = {
            "entry": [
                {"resource": {"resourceType": "CarePlan", "id": "cp-1"}},
                {"resource": {
                    "resourceType": "DocumentReference",
                    "content": [{"attachment": {"data": "abc", "contentType": "text/plain"}}],
                }},
            ],
        }
        sanitized = _bundle_for_review(bundle)
        assert sanitized["entry"][0]["resource"] == {"resourceType": "CarePlan", "id": "cp-1"}
        att = sanitized["entry"][1]["resource"]["content"][0]["attachment"]
        assert "data" not in att
        assert att["contentType"] == "text/plain"

    @patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm")
    def test_writes_artifact(self, mock_get_llm, tmp_path):
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps({"verdict": "APPROVE", "issues": []})
        mock_llm.invoke.return_value = mock_response
        mock_get_llm.return_value = mock_llm

        state = _make_state()
        state["output_dir"] = str(tmp_path)
        fhir_semantic_reviewer(state)

        assert (tmp_path / "fhir-review-1.json").exists()
