"""Tests for the Classification Reviewer node."""

import json
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from cpg_ingester.nodes.classification_reviewer import classification_reviewer


SAMPLE_MANIFEST = [
    {"type": "decision", "name": "Treatment Recommendation", "id": "aaa-111", "category": "treatment", "tier": 1},
    {"type": "recommendation", "title": "DASH Diet", "id": "bbb-222", "recommendation_type": "lifestyle"},
]


class TestClassificationReviewer:

    def test_reviewer_flags_variants_to_merge_from_v2_treatment_and_monitoring(self):
        from cpg_ingester.prompts.classification_reviewer import CLASSIFICATION_REVIEWER_SYSTEM
        from cpg_ingester.prompts.item_identifier import ITEM_IDENTIFIER_SYSTEM

        source = (Path(__file__).resolve().parents[1] / "data" / "synthetic-hypertension-cpg-v2.md").read_text()
        section_start = source.index("### 3.2 Treatment Decision")
        section_end = source.index("### 3.4 Lifestyle Modification Recommendations")
        sections_32_33 = source[section_start:section_end]
        manifest = [
            {"type": "process", "name": "ACE inhibitor follow-up with kidney disease", "section": "3.2"},
            {"type": "process", "name": "ACE inhibitor follow-up without kidney disease", "section": "3.2"},
            {"type": "process", "name": "Lifestyle reassessment at 8 weeks", "section": "3.2"},
            {"type": "process", "name": "Lifestyle reassessment at 12 weeks", "section": "3.2"},
        ]
        mock_response = json.dumps({
            "issues_found": True,
            "feedback": (
                "Merge the kidney-disease and no-kidney-disease ACE inhibitor follow-up "
                "items into one process with a trigger per DMN output. Merge the 8-week "
                "and 12-week lifestyle reassessment items into one process with an "
                "interval parameter and triggers per output value."
            ),
            "issues": [
                {"item_name": "ACE inhibitor follow-up", "issue_type": "process_unsupported",
                 "description": "Merge the kidney/no-kidney variants; they differ only by a DMN row."},
                {"item_name": "Lifestyle reassessment", "issue_type": "process_unsupported",
                 "description": "Merge the 8/12-week variants; they differ only by interval."},
            ],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=mock_response))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item_manifest": manifest,
                "section_map": [],
                "markdown": sections_32_33,
                "output_dir": tmpdir,
                "classification_review_count": 0,
            }
            with patch("cpg_ingester.nodes.classification_reviewer.get_llm", return_value=mock_llm):
                result = classification_reviewer(state)

        reviewer_prompt = mock_llm.invoke.call_args.args[0][1]["content"]
        reviewer_rules = re.sub(r"\s+", " ", CLASSIFICATION_REVIEWER_SYSTEM.lower())
        identifier_rules = re.sub(r"\s+", " ", ITEM_IDENTIFIER_SYSTEM.lower())
        assert "two process items that differ only by a dmn row or a number → merge them" in reviewer_rules
        assert "one template per care process" in identifier_rules
        assert "a trigger for each output value" in identifier_rules
        assert "ACE inhibitor follow-up with kidney disease" in reviewer_prompt
        assert "ACE inhibitor follow-up without kidney disease" in reviewer_prompt
        assert "Lifestyle reassessment at 8 weeks" in reviewer_prompt
        assert "Lifestyle reassessment at 12 weeks" in reviewer_prompt
        assert "Decision Table 1: Treatment Recommendation" in reviewer_prompt
        assert "Decision Table 2: Monitoring Plan" in reviewer_prompt
        assert "Merge the kidney-disease and no-kidney-disease" in result["classification_review_feedback"]
        assert "Merge the 8-week and 12-week" in result["classification_review_feedback"]

    def test_no_issues_returns_empty_feedback(self):
        mock_response = json.dumps({
            "issues_found": False,
            "feedback": "",
            "issues": [],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=mock_response))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item_manifest": SAMPLE_MANIFEST,
                "section_map": [],
                "markdown": "content",
                "output_dir": tmpdir,
                "classification_review_count": 0,
            }
            with patch("cpg_ingester.nodes.classification_reviewer.get_llm", return_value=mock_llm):
                result = classification_reviewer(state)

            assert result["classification_review_feedback"] == ""

    def test_issues_found_returns_feedback(self):
        mock_response = json.dumps({
            "issues_found": True,
            "feedback": "Section 4.3 contains BP thresholds (>=140/90) but is classified as Tier 3 narrative.",
            "issues": [
                {
                    "item_name": "MISSING",
                    "issue_type": "missed_item",
                    "description": "Appendix table on page 8 contains drug dosing criteria",
                },
            ],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=mock_response))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item_manifest": SAMPLE_MANIFEST,
                "section_map": [],
                "markdown": "content",
                "output_dir": tmpdir,
                "classification_review_count": 0,
            }
            with patch("cpg_ingester.nodes.classification_reviewer.get_llm", return_value=mock_llm):
                result = classification_reviewer(state)

            assert "thresholds" in result["classification_review_feedback"]

    def test_writes_review_report(self):
        mock_response = json.dumps({
            "issues_found": True,
            "feedback": "Some issue found",
            "issues": [{"item_name": "X", "issue_type": "tier_misclassification", "description": "wrong tier"}],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=mock_response))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item_manifest": SAMPLE_MANIFEST,
                "section_map": [],
                "markdown": "content",
                "output_dir": tmpdir,
                "classification_review_count": 0,
            }
            with patch("cpg_ingester.nodes.classification_reviewer.get_llm", return_value=mock_llm):
                classification_reviewer(state)

            report_path = Path(tmpdir) / "classification-review-1.json"
            assert report_path.exists()
            report = json.loads(report_path.read_text())
            assert report["issues_found"] is True
            assert report["issue_count"] == 1
            assert report["review_iteration"] == 1

    def test_second_iteration_uses_correct_number(self):
        mock_response = json.dumps({"issues_found": False, "feedback": "", "issues": []})
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=mock_response))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item_manifest": SAMPLE_MANIFEST,
                "section_map": [],
                "markdown": "content",
                "output_dir": tmpdir,
                "classification_review_count": 1,
            }
            with patch("cpg_ingester.nodes.classification_reviewer.get_llm", return_value=mock_llm):
                classification_reviewer(state)

            assert (Path(tmpdir) / "classification-review-2.json").exists()

    def test_empty_manifest_returns_empty_feedback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item_manifest": [],
                "section_map": [],
                "markdown": "content",
                "output_dir": tmpdir,
            }
            result = classification_reviewer(state)
            assert result["classification_review_feedback"] == ""

    def test_handles_parse_failure(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content="not json"))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item_manifest": SAMPLE_MANIFEST,
                "section_map": [],
                "markdown": "content",
                "output_dir": tmpdir,
                "classification_review_count": 0,
            }
            with patch("cpg_ingester.nodes.classification_reviewer.get_llm", return_value=mock_llm):
                result = classification_reviewer(state)

            assert result["classification_review_feedback"] == ""

    def test_reviewer_uses_heterogeneous_persona(self):
        """Verify the reviewer prompt uses a different persona than the identifier."""
        from cpg_ingester.prompts.classification_reviewer import CLASSIFICATION_REVIEWER_SYSTEM
        from cpg_ingester.prompts.item_identifier import ITEM_IDENTIFIER_SYSTEM

        assert "skeptical" in CLASSIFICATION_REVIEWER_SYSTEM.lower()
        assert "methodologist" in CLASSIFICATION_REVIEWER_SYSTEM.lower()
        assert "decision logic engineer" in ITEM_IDENTIFIER_SYSTEM.lower()
