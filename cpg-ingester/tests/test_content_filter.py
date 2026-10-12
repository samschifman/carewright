"""Tests for the Content Filter node."""

import json
import tempfile
from pathlib import Path

from cpg_ingester.nodes.content_filter import (
    _is_abbreviation_section,
    _section_contains_high_value_keywords,
    _extract_section_text,
    _remove_section_from_markdown,
    content_filter,
)


SAMPLE_MARKDOWN = """\
# 1. Scope and Purpose

This guideline addresses hypertension.

# 2. Methodology

We searched PubMed and Cochrane databases.

# 3. Recommendations

## 3.1 Treatment Decision

Patients with SBP >= 140 should begin pharmacological therapy.

## 3.2 Monitoring

Monitor blood pressure at follow-up visits.

# 4. Evidence Summary

The SPRINT trial demonstrated benefits of intensive treatment.

# 5. Appendix: Pharmacotherapy Reference Table

Drug dosing and contraindication information.

# 6. Abbreviations

BP - Blood Pressure
SBP - Systolic Blood Pressure

# 7. Author Disclosures

Dr. Smith reports no conflicts of interest.

# 8. References

1. Whelton PK, et al. 2017 ACC/AHA Guideline.
"""


class TestKeywordSafetyCheck:

    def test_detects_drug_keyword(self):
        found = _section_contains_high_value_keywords("Appendix", "Drug dosing tables")
        assert "drug" in found
        assert "dosing" in found

    def test_detects_threshold(self):
        found = _section_contains_high_value_keywords("Criteria", "BP threshold of 140")
        assert "threshold" in found

    def test_no_keywords_in_methodology(self):
        found = _section_contains_high_value_keywords("Methodology", "We searched PubMed databases using standard search terms")
        assert len(found) == 0

    def test_no_keywords_in_author_disclosures(self):
        found = _section_contains_high_value_keywords("Author Disclosures", "Dr. Smith reports no conflicts")
        assert len(found) == 0

    def test_detects_recommendation_keyword(self):
        found = _section_contains_high_value_keywords("Summary", "Key recommendation for treatment")
        assert "recommendation" in found
        assert "treatment" in found


class TestAbbreviationDetection:

    def test_detects_abbreviation_heading(self):
        assert _is_abbreviation_section("Abbreviations")
        assert _is_abbreviation_section("List of Abbreviations")
        assert _is_abbreviation_section("Glossary of Terms")

    def test_rejects_non_abbreviation(self):
        assert not _is_abbreviation_section("Recommendations")
        assert not _is_abbreviation_section("Evidence Summary")


class TestSectionExtraction:

    def test_extracts_section_between_headings(self):
        text = _extract_section_text(SAMPLE_MARKDOWN, "2. Methodology", "3. Recommendations")
        assert "PubMed" in text
        assert "Treatment Decision" not in text

    def test_extracts_last_section(self):
        text = _extract_section_text(SAMPLE_MARKDOWN, "8. References", None)
        assert "Whelton" in text


class TestSectionRemoval:

    def test_removes_section(self):
        result = _remove_section_from_markdown(SAMPLE_MARKDOWN, "2. Methodology", "3. Recommendations")
        assert "Methodology" not in result
        assert "PubMed" not in result
        assert "Recommendations" in result

    def test_preserves_other_sections(self):
        result = _remove_section_from_markdown(SAMPLE_MARKDOWN, "2. Methodology", "3. Recommendations")
        assert "Scope and Purpose" in result
        assert "Treatment Decision" in result


class TestContentFilter:

    def _make_section_map(self):
        return [
            {"heading": "1. Scope and Purpose", "classification": "skip", "page_start": 1, "page_end": 2},
            {"heading": "2. Methodology", "classification": "skip", "page_start": 2, "page_end": 3},
            {"heading": "3. Recommendations", "classification": "both", "page_start": 3, "page_end": 4},
            {"heading": "3.1 Treatment Decision", "classification": "decision", "page_start": 3, "page_end": 3},
            {"heading": "3.2 Monitoring", "classification": "recommendation", "page_start": 3, "page_end": 4},
            {"heading": "4. Evidence Summary", "classification": "skip", "page_start": 4, "page_end": 5},
            {"heading": "5. Appendix: Pharmacotherapy Reference Table", "classification": "skip", "page_start": 5, "page_end": 6},
            {"heading": "6. Abbreviations", "classification": "skip", "page_start": 6, "page_end": 7},
            {"heading": "7. Author Disclosures", "classification": "skip", "page_start": 7, "page_end": 8},
            {"heading": "8. References", "classification": "skip", "page_start": 8, "page_end": 8},
        ]

    def test_removes_methodology(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            result = content_filter(state)
            assert "PubMed" not in result["markdown"]

    def test_preserves_recommendations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            result = content_filter(state)
            assert "Treatment Decision" in result["markdown"]

    def test_restores_pharmacotherapy_appendix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            result = content_filter(state)
            restored = [s for s in result["section_map"] if s["heading"] == "5. Appendix: Pharmacotherapy Reference Table"]
            assert restored[0]["classification"] != "skip"
            assert "Drug dosing" in result["markdown"]

    def test_restores_abbreviation_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            result = content_filter(state)
            abbr_section = [s for s in result["section_map"] if s["heading"] == "6. Abbreviations"]
            assert abbr_section[0]["classification"] == "reference"

    def test_removes_author_disclosures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            result = content_filter(state)
            assert "Dr. Smith reports" not in result["markdown"]

    def test_writes_filter_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            content_filter(state)
            report = json.loads((Path(tmpdir) / "filter-report.json").read_text())
            assert report["total_sections"] == 10
            assert len(report["removed"]) > 0
            assert len(report["restored"]) > 0

    def test_writes_filtered_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            content_filter(state)
            assert (Path(tmpdir) / "filtered.md").exists()

    def test_filtered_markdown_is_shorter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            result = content_filter(state)
            assert len(result["markdown"]) < len(SAMPLE_MARKDOWN)

    def test_preserves_unfiltered_source_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "section_map": self._make_section_map(),
                "markdown": SAMPLE_MARKDOWN,
                "output_dir": tmpdir,
            }
            result = content_filter(state)
            assert result["source_markdown"] == SAMPLE_MARKDOWN
            assert result["markdown"] != result["source_markdown"]

    def test_passthrough_when_no_section_map(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": SAMPLE_MARKDOWN, "output_dir": tmpdir}
            result = content_filter(state)
            assert "markdown" not in result
