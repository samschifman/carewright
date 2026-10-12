"""Tests for the Metadata Extractor node."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from cpg_contracts import CONTRACT_VERSION
from cpg_ingester.nodes.metadata_extractor import (
    _cross_check_grading_system,
    _extract_front_matter,
    metadata_extractor,
)

FIXTURES = Path(__file__).parent / "fixtures"


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

    def test_front_matter_reads_flattened_v2_and_line_oriented_v1(self):
        flattened_v2 = (FIXTURES / "parsed-v2-flattened.md").read_text()
        line_oriented_v1 = (
            Path(__file__).parents[1] / "data" / "synthetic-hypertension-cpg.md"
        ).read_text()

        v2 = _extract_front_matter(flattened_v2)
        v1 = _extract_front_matter(line_oriented_v1)

        assert v2["cpg_id"] == "SYN-HTN-2026-002"
        assert v2["version"] == "2.0"
        assert v2["effective_date"] == "October 2026"
        assert v1["cpg_id"] == "SYN-HTN-2026-001"
        assert v1["version"] == "1.0"
        assert v1["effective_date"] == "July 2026"

    def test_front_matter_overrides_missed_llm_identity_fields(self):
        missed_fields = json.dumps({
            **json.loads(MOCK_LLM_RESPONSE),
            "cpg_id": "UNK-HTN-UNDATED",
            "version": None,
            "effective_date": None,
            "publication_date": None,
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=missed_fields))
        markdown = (FIXTURES / "parsed-v2-flattened.md").read_text()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                result = metadata_extractor({"markdown": markdown, "output_dir": tmpdir})

        meta = result["cpg_metadata"]
        assert meta["cpg_id"] == "SYN-HTN-2026-002"
        assert meta["version"] == "2.0"
        assert meta["effective_date"] == "October 2026"
        assert meta["publication_date"] is None

    def test_extracts_identity_when_filter_removed_front_matter(self):
        from cpg_ingester.nodes.content_filter import content_filter

        source_markdown = (FIXTURES / "parsed-v2-flattened.md").read_text()
        filtered_markdown = "## 1. Scope and Purpose\n\nFiltered guideline body."
        section_map = [{
            "heading": "Clinical Practice Guideline: Initial Management of Hypertension in Adults",
            "classification": "skip",
        }]
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=json.dumps({
            **json.loads(MOCK_LLM_RESPONSE),
            "cpg_id": "UNK-HTN-UNK",
            "version": None,
            "effective_date": None,
        })))

        with tempfile.TemporaryDirectory() as tmpdir:
            filtered = content_filter({
                "markdown": source_markdown,
                "section_map": section_map,
                "output_dir": tmpdir,
            })
            # Model the observed state: filtering removed the front matter and
            # left the full parsed source only in source_markdown.
            with patch("cpg_ingester.nodes.metadata_extractor.get_llm", return_value=mock_llm):
                result = metadata_extractor({
                    **filtered,
                    "markdown": filtered_markdown,
                    "source_markdown": source_markdown,
                    "output_dir": tmpdir,
                })

        assert result["cpg_metadata"]["cpg_id"] == "SYN-HTN-2026-002"
        assert result["cpg_metadata"]["version"] == "2.0"
        assert result["cpg_metadata"]["effective_date"] == "October 2026"
        assert mock_llm.invoke.call_args.args[0][1]["content"].find("SYN-HTN-2026-002") >= 0

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
