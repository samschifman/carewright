import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "cpg-ingester" / "data"
V2_PATH = DATA_DIR / "synthetic-hypertension-cpg-v2.md"
V1_PATH = DATA_DIR / "synthetic-hypertension-cpg.md"
HOME_BP_IR_PATH = REPO_ROOT / "shared" / "tests" / "fixtures" / "automation" / "home-bp-monitoring.ir.json"


def _source_texts(value):
    if isinstance(value, dict):
        if isinstance(value.get("source_text"), str):
            yield value["source_text"]
        for child in value.values():
            yield from _source_texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _source_texts(child)


def _citation_fragments(source_text: str) -> list[str]:
    # The fixture uses ellipses to abbreviate source excerpts. Check each
    # quoted fragment while preserving ordinary punctuation and wording.
    return [fragment.strip() for fragment in re.split(r"\s+(?:\.{3}|…)\s+", source_text) if fragment.strip()]


def test_v2_metadata_sections_quality_measure_and_disclaimer():
    text = V2_PATH.read_text(encoding="utf-8")
    assert "**Guideline ID:** SYN-HTN-2026-002" in text
    assert "**Version:** 2.0" in text
    assert "**Effective Date:** October 2026" in text
    assert "### 3.5 Home Blood Pressure Monitoring" in text
    assert "### 3.6 Follow-up Contact and Outreach" in text
    assert (
        "Percentage of patients performing home monitoring whose weekly readings "
        "were reviewed by the care team within seven days of submission"
    ) in text
    assert "This is a synthetic clinical practice guideline" in text


def test_v2_contains_every_home_bp_fixture_citation():
    document = " ".join(V2_PATH.read_text(encoding="utf-8").split()).casefold()
    fixture = json.loads(HOME_BP_IR_PATH.read_text(encoding="utf-8"))
    citations = sorted(set(_source_texts(fixture)))
    missing = [
        fragment
        for citation in citations
        for fragment in _citation_fragments(citation)
        if " ".join(fragment.split()).casefold() not in document
    ]
    assert citations, "the home blood pressure fixture should contain source citations"
    assert not missing, f"v2 CPG is missing fixture source text: {missing}"


def test_v1_remains_the_original_document():
    text = V1_PATH.read_text(encoding="utf-8")
    assert "**Guideline ID:** SYN-HTN-2026-001" in text
    assert "**Version:** 1.0" in text
    assert "**Effective Date:** July 2026" in text
    assert "### 3.5 Home Blood Pressure Monitoring" not in text
