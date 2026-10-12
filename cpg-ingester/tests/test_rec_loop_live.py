"""Live LLM test for the full rec extraction loop (extract → validate → review).

Runs the rec subgraph for one section against a live LiteLLM proxy
to verify that recommendations pass schema validation and semantic
review with the tuned prompts.

Run explicitly (excluded from default test suite):
    cd cpg-ingester
    .venv/bin/python -m pytest tests/test_rec_loop_live.py -v -s -m live

Requires: LiteLLM running in podman on port 4000.
"""

import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from cpg_ingester.generation import _build_rec_subgraph, _extract_section_text

ARTIFACTS_DIR = Path(__file__).parent.parent / "output" / "a76a8506"
PARSED_MD = ARTIFACTS_DIR / "parsed.md"
MANIFEST = ARTIFACTS_DIR / "manifest.json"

LITELLM_URL = "http://localhost:4000"
LLM_MODEL = "default"
LLM_API_KEY = os.environ.get("LITELLM_API_KEY", "sk-change-me")

SECTION_HEADINGS = {
    "3.4": "3.4 Lifestyle Modification Recommendations",
    "5.1": "5.1 Clinical Workflow Integration",
    "5.3": "5.3 Quality Measures",
}


def _extract_text(markdown, heading):
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


def _load_manifest_items(section):
    if not MANIFEST.exists():
        return []
    m = json.loads(MANIFEST.read_text())
    items = m if isinstance(m, list) else m.get("items", [])
    return [i for i in items if i.get("section") == section and i.get("type") == "recommendation"]


def _skip_if_missing():
    if not ARTIFACTS_DIR.exists() or not PARSED_MD.exists():
        pytest.skip("No pipeline artifacts")


def _skip_if_no_litellm():
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{LITELLM_URL}/health",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pytest.skip("LiteLLM not running")


@pytest.fixture(autouse=True)
def prereqs():
    _skip_if_missing()
    _skip_if_no_litellm()


@pytest.mark.live
class TestRecLoopLive:
    """Run the full rec subgraph (extract → schema validate → semantic review)."""

    @pytest.mark.parametrize("section", ["3.4", "5.1", "5.3"])
    def test_full_loop(self, section):
        items = _load_manifest_items(section)
        if not items:
            pytest.skip(f"No manifest items for section {section}")

        markdown = PARSED_MD.read_text()
        heading = SECTION_HEADINGS[section]
        source_text = _extract_text(markdown, heading)
        assert source_text, f"No source text for {heading}"

        rec_graph = _build_rec_subgraph().compile()

        with tempfile.TemporaryDirectory() as tmpdir:
            result = rec_graph.invoke({
                "items": items,
                "source_pages": source_text,
                "grading_definitions": "",
                "abbreviations": {},
                "litellm_url": LITELLM_URL,
                "llm_model": LLM_MODEL,
                "output_dir": tmpdir,
            })

            recs = result.get("recommendations", [])
            escalated = result.get("escalated", False)
            schema_errors = result.get("schema_errors", [])
            semantic_disc = result.get("semantic_discrepancies", [])

            print(f"\n{'='*60}")
            print(f"Section {section}: {heading}")
            print(f"Recommendations: {len(recs)}")
            print(f"Escalated: {escalated}")
            print(f"Schema errors: {schema_errors}")
            print(f"Semantic discrepancies: {semantic_disc}")
            if recs:
                for r in recs:
                    xrefs = r.get("cross_references", [])
                    cert = r.get("certainty", {})
                    print(f"  [{cert.get('strength','?')}] {r.get('title','?')} (xrefs={len(xrefs)})")
            print(f"{'='*60}")

            assert not escalated, (
                f"Section {section} escalated. "
                f"Schema: {schema_errors}, Semantic: {semantic_disc}"
            )
            assert len(recs) > 0, f"No recommendations produced for {section}"
