"""Tests for the Metadata Extractor node."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from cpg_contracts import CONTRACT_VERSION
from cpg_ingester.nodes.metadata_extractor import (
    _cross_check_grading_system,
    metadata_extractor,
)


MOCK_LLM_RESPONSE = json.dumps({
    "cpg_id": "SYN-HTN-2026-001",
    "title": "Clinical Practice Guideline: Initial Management of Hypertension in Adults",
    "version": "1.0",
    "publication_date": "2026-07-01",
    "evidence_review_date": "2026-06-01",
    "issuing_body": "Synthetic Guidelines Collaborative",
    "grading_system": "GRADE",
    "scope": "Adults (age 18+) with a new diagnosis of essential hypertension",
    "supersedes": None,
})


class TestGradingCrossCheck:

    def test_no_warning_when_matching(self):
        text = "This is a strong recommendation with high certainty evidence."
        result = _cross_check_grading_system("GRADE", text)
        assert result is None

    def test_warning_when_no_vocabulary(self):
        text = "The committee reviewed the evidence and made suggestions."
        result = _cross_check_grading_system("GRADE", text)
        assert result is not None
        assert "no matching vocabulary" in result

    def test_warning_when_wrong_system(self):
        text = "Class I recommendation, Level A evidence. Class IIa, Level B-R."
        result = _cross_check_grading_system("GRADE", text)
        assert result is not None
        assert "GRADE" in result

    def test_no_warning_for_cor_loe(self):
        text = "This is a Class I recommendation with Level A evidence. Class IIa, Level B."
        result = _cross_check_grading_system("COR-LOE", text)
        assert result is None

    def test_none_declared_returns_none(self):
        result = _cross_check_grading_system(None, "any text")
        assert result is None


class TestMetadataExtractor:

    def test_extracts_metadata(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "# Guideline\nSome content", "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                result = metadata_extractor(state)

            meta = result["cpg_metadata"]
            assert meta["cpg_id"] == "SYN-HTN-2026-001"
            assert meta["title"] == "Clinical Practice Guideline: Initial Management of Hypertension in Adults"
            assert meta["version"] == "1.0"
            assert meta["grading_system"] == "GRADE"
            assert meta["scope"] is not None

    def test_writes_metadata_artifact(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                metadata_extractor(state)

            metadata_file = Path(tmpdir) / "metadata.json"
            assert metadata_file.exists()
            data = json.loads(metadata_file.read_text())
            assert data["cpg_id"] == "SYN-HTN-2026-001"

    def test_validates_with_pydantic(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                result = metadata_extractor(state)

            from cpg_contracts import CPGMetadata
            CPGMetadata(**result["cpg_metadata"])

    def test_handles_invalid_grading_system(self):
        bad_response = json.dumps({
            "cpg_id": "TEST-001",
            "title": "Test Guideline",
            "grading_system": "NONEXISTENT",
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=bad_response))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                result = metadata_extractor(state)

            assert result["cpg_metadata"]["cpg_id"] == "TEST-001"
            assert result["cpg_metadata"]["grading_system"] is None

    def test_handles_parse_failure(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content="not json"))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                result = metadata_extractor(state)

            assert result["cpg_metadata"] == {}

    def test_includes_contract_version(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                result = metadata_extractor(state)

            assert result["cpg_metadata"]["contract_version"] == CONTRACT_VERSION
