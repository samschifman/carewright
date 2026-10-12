"""Live LLM test for rec_semantic_reviewer prompt tuning.

Loads real artifacts from a pipeline run and calls the reviewer
against a running LiteLLM proxy (localhost:4000) to reproduce
the over-escalation problem.

Run explicitly (excluded from default test suite):
    cd cpg-ingester
    .venv/bin/python -m pytest tests/test_rec_reviewer_live.py -v -s -m live

Requires: LiteLLM running in podman on port 4000.
"""

import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from cpg_ingester.nodes.rec_semantic_reviewer import rec_semantic_reviewer

ARTIFACTS_DIR = Path(__file__).parent.parent / "output" / "a76a8506"
PARSED_MD = ARTIFACTS_DIR / "parsed.md"

LITELLM_URL = "http://localhost:4000"
LLM_MODEL = "default"
LLM_API_KEY = os.environ.get("LITELLM_API_KEY", "sk-change-me")

SECTIONS_WITH_RECS = ["3.1", "3.2", "3.4", "5.1", "5.2", "5.3"]

SECTION_HEADINGS = {
    "3.1": "3.1 Patient Assessment",
    "3.2": "3.2 Treatment Decision",
    "3.4": "3.4 Lifestyle Modification Recommendations",
    "5.1": "5.1 Clinical Workflow Integration",
    "5.2": "5.2 Patient Engagement",
    "5.3": "5.3 Quality Measures",
}


def _extract_section_text(markdown: str, heading: str) -> str:
    lines = markdown.split("\n")
    start_idx = None
    heading_level = 0
    for i, line in enumerate(lines):
        if heading in line and line.strip().startswith("#"):
            start_idx = i
            heading_level = len(line) - len(line.lstrip("#"))
            break
    if start_idx is None:
        return ""
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        line = lines[i].strip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= heading_level:
                text_after_hash = line.lstrip("#").strip()
                if re.match(r"\d", text_after_hash):
                    end_idx = i
                    break
    return "\n".join(lines[start_idx:end_idx]).strip()


def _load_recs(section: str) -> list:
    rec_file = ARTIFACTS_DIR / f"recommendations-{section}.json"
    if not rec_file.exists():
        return []
    return json.loads(rec_file.read_text())


def _skip_if_no_artifacts():
    if not ARTIFACTS_DIR.exists() or not PARSED_MD.exists():
        pytest.skip("No pipeline artifacts found — run pipeline first")


def _skip_if_no_litellm():
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{LITELLM_URL}/health",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pytest.skip("LiteLLM not running on localhost:4000")


@pytest.fixture(autouse=True)
def check_prereqs():
    _skip_if_no_artifacts()
    _skip_if_no_litellm()


@pytest.mark.live
class TestRecReviewerLive:
    """Call rec_semantic_reviewer with real data against a live LLM."""

    @pytest.mark.parametrize("section", SECTIONS_WITH_RECS)
    def test_section_review(self, section):
        recs = _load_recs(section)
        if not recs:
            pytest.skip(f"No recommendations for section {section}")

        markdown = PARSED_MD.read_text()
        heading = SECTION_HEADINGS.get(section, section)
        source_text = _extract_section_text(markdown, heading)
        assert source_text, f"Could not extract source text for {heading}"

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": recs,
                "source_pages": source_text,
                "items": [{"section": section}],
                "output_dir": tmpdir,
                "review_count": 0,
                "litellm_url": LITELLM_URL,
                "llm_model": LLM_MODEL,
            }

            result = rec_semantic_reviewer(state)

            discrepancies = result.get("semantic_discrepancies", [])

            review_files = list(Path(tmpdir).glob("rec-review-*.json"))
            if review_files:
                review = json.loads(review_files[0].read_text())
                print(f"\n{'='*60}")
                print(f"Section {section}: {heading}")
                print(f"Recs checked: {review.get('recommendations_checked', 0)}")
                print(f"Passed: {review.get('passed', 0)}")
                print(f"With issues: {review.get('with_issues', 0)}")
                print(f"Missing: {len(review.get('missing_recommendations', []))}")
                for m in review.get("missing_recommendations", []):
                    print(f"  MISSING: {m}")
                for check in review.get("checks", []):
                    status = "PASS" if not check.get("issues") else "FAIL"
                    print(f"  [{status}] {check.get('recommendation_title', '?')}")
                    for issue in check.get("issues", []):
                        print(f"         {issue}")
                print(f"Summary: {review.get('summary', '')}")
                print(f"Discrepancies returned: {len(discrepancies)}")
                print(f"{'='*60}")

            if discrepancies:
                print(f"\nReviewer escalated section {section}:")
                for d in discrepancies:
                    print(f"  - {d}")
