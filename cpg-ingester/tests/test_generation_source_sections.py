"""Regressions for section references emitted by the 2026-10-11 slice run."""

import json
from pathlib import Path

from cpg_ingester.generation import _extract_section_text


FIXTURES = Path(__file__).parent / "fixtures"


def test_saved_slice_sections_resolve_composite_manifest_labels():
    markdown = (FIXTURES / "parsed-v2-flattened.md").read_text()
    source_case = json.loads(
        (FIXTURES / "source-sections-from-20261011.json").read_text()
    )
    section_map = source_case["section_map"]

    excerpts = {
        item["name"]: _extract_section_text(markdown, section_map, item["section"])
        for item in source_case["sections"]
    }

    assert "Basic Metabolic Panel" in excerpts["Monitoring Plan"]
    assert "2 weeks" in excerpts["ACE Inhibitor Laboratory Monitoring"]
    assert "lifestyle modification alone" in excerpts[
        "No Routine Laboratory Monitoring for Lifestyle-Only Management"
    ]
    assert all(excerpts.values()), [name for name, text in excerpts.items() if not text]


def test_saved_slice_composite_section_includes_both_referenced_sections():
    markdown = (FIXTURES / "parsed-v2-flattened.md").read_text()
    section_map = json.loads(
        (FIXTURES / "source-sections-from-20261011.json").read_text()
    )["section_map"]

    text = _extract_section_text(markdown, section_map, "2.2; 3.2, Key principles")

    assert "Chronic Kidney Disease (CKD)" in text
    assert "Start medication" in text
