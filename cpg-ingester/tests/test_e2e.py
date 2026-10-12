"""End-to-end integration test — runs the full pipeline with a live LLM.

Requires:
  - LITELLM_URL env var or LiteLLM running on localhost:4000
  - Synthetic CPG PDF in data/

Run with:
  LITELLM_URL=http://localhost:4000 pytest tests/test_e2e.py -v -s
"""

import json
import os
import tempfile
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from cpg_ingester.pipeline import build_pipeline
from cpg_ingester.validators.dmn_syntax import validate_dmn_xml

SYNTHETIC_CPG = Path(__file__).parent.parent / "data" / "synthetic-hypertension-cpg.pdf"

LITELLM_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
LLM_MODEL = os.environ.get("LLM_MODEL", "default")

pytestmark = [
    pytest.mark.skipif(not SYNTHETIC_CPG.exists(), reason="Synthetic CPG PDF not found"),
    pytest.mark.skipif(not os.environ.get("LITELLM_URL"), reason="Set LITELLM_URL to run e2e tests"),
]


@pytest.fixture(scope="module")
def pipeline_result(tmp_path_factory):
    """Run the full pipeline once and share the result across all tests in this module."""
    output_dir = tmp_path_factory.mktemp("e2e")

    graph = build_pipeline()
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)

    state = {
        "run_id": "e2e-test",
        "output_dir": str(output_dir),
        "pdf_path": str(SYNTHETIC_CPG),
        "litellm_url": LITELLM_URL,
        "llm_model": LLM_MODEL,
    }
    config = {"configurable": {"thread_id": "e2e-test"}}

    result = compiled.invoke(state, config=config)
    result["_output_dir"] = str(output_dir)
    return result


# --- Phase 1 checks ---

class TestPhase1Analysis:

    def test_produced_markdown(self, pipeline_result):
        md = pipeline_result.get("markdown", "")
        assert len(md) > 1000, f"Markdown too short: {len(md)} chars"
        assert "Hypertension" in md or "hypertension" in md

    def test_detected_archetype(self, pipeline_result):
        archetype = pipeline_result.get("archetype", "")
        assert archetype in ("institutional", "journal-article", "multi-module", "focused-policy"), \
            f"Unexpected archetype: {archetype}"

    def test_produced_section_map(self, pipeline_result):
        section_map = pipeline_result.get("section_map", [])
        assert len(section_map) >= 5, f"Only {len(section_map)} sections"
        classifications = {s["classification"] for s in section_map}
        assert len(classifications) >= 2, f"Only {classifications} classifications"

    def test_extracted_abbreviations(self, pipeline_result):
        abbrs = pipeline_result.get("abbreviations", {})
        assert len(abbrs) > 0, "No abbreviations extracted"

    def test_produced_cpg_metadata(self, pipeline_result):
        meta = pipeline_result.get("cpg_metadata", {})
        assert meta.get("cpg_id"), "Missing cpg_id"
        assert meta.get("title"), "Missing title"
        assert "1.0" == meta.get("contract_version"), f"Wrong contract version: {meta.get('contract_version')}"

    def test_produced_item_manifest(self, pipeline_result):
        manifest = pipeline_result.get("item_manifest", [])
        assert len(manifest) > 0, "Empty manifest"

    def test_manifest_has_decisions(self, pipeline_result):
        manifest = pipeline_result.get("item_manifest", [])
        decisions = [i for i in manifest if i.get("type") == "decision"]
        assert len(decisions) >= 1, f"Only {len(decisions)} decisions (expected >= 1)"

    def test_manifest_has_recommendations(self, pipeline_result):
        manifest = pipeline_result.get("item_manifest", [])
        recs = [i for i in manifest if i.get("type") == "recommendation"]
        assert len(recs) >= 3, f"Only {len(recs)} recommendations (expected >= 3)"

    def test_all_manifest_items_have_guids(self, pipeline_result):
        manifest = pipeline_result.get("item_manifest", [])
        for item in manifest:
            assert item.get("id"), f"Item missing id: {item.get('name') or item.get('title')}"
            assert len(item["id"]) == 36, f"ID not a GUID: {item['id']}"

    def test_classification_review_ran(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        reviews = list(out.glob("classification-review-*.json"))
        assert len(reviews) >= 1, "No classification review files"


# --- Phase 2 checks ---

class TestPhase2Generation:

    def test_generated_dmn_files(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        dmn_files = list((out / "dmn").glob("*.dmn"))
        assert len(dmn_files) >= 1, f"Only {len(dmn_files)} DMN files"

    def test_dmn_files_are_valid_xml(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        for dmn_file in (out / "dmn").glob("*.dmn"):
            errors = validate_dmn_xml(dmn_file.read_text())
            assert errors == [], f"{dmn_file.name} has syntax errors: {errors}"

    def test_dmn_semantic_reviews_ran(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        reviews = list(out.glob("dmn-review-*.json"))
        assert len(reviews) >= 1, "No DMN semantic review files"

    def test_generated_recommendation_files(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        rec_files = list(out.glob("recommendations-*.json"))
        assert len(rec_files) >= 1, f"Only {len(rec_files)} recommendation files"

    def test_recommendations_have_required_fields(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        for rec_file in out.glob("recommendations-*.json"):
            recs = json.loads(rec_file.read_text())
            if isinstance(recs, dict) and "recommendations" in recs:
                recs = recs["recommendations"]
            for rec in recs:
                assert rec.get("id"), f"Rec missing id in {rec_file.name}"
                assert rec.get("title"), f"Rec missing title in {rec_file.name}"
                assert rec.get("content"), f"Rec missing content in {rec_file.name}"
                assert rec.get("recommendation_type"), f"Rec missing type in {rec_file.name}"


# --- Assembly + Delivery checks ---

class TestAssemblyAndDelivery:

    def test_assembly_report_exists(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        report = out / "assembly-report.json"
        assert report.exists(), "No assembly-report.json"
        data = json.loads(report.read_text())
        assert "recommendations_count" in data
        assert "dmn_models_count" in data

    def test_recommendation_bundle_exists(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        bundle = out / "recommendation-bundle.json"
        assert bundle.exists(), "No recommendation-bundle.json"
        data = json.loads(bundle.read_text())
        assert data.get("contract_version") == "1.0"
        assert data.get("source_cpg"), "Bundle missing source_cpg"
        assert len(data.get("recommendations", [])) > 0, "Bundle has no recommendations"

    def test_delivery_status_exists(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        status = out / "delivery-status.json"
        assert status.exists(), "No delivery-status.json"

    def test_pipeline_produced_results(self, pipeline_result):
        assert pipeline_result.get("run_id") == "e2e-test"
        assert len(pipeline_result.get("item_manifest", [])) > 0


# --- Output artifact inventory ---

class TestOutputArtifacts:

    def test_all_expected_artifacts_present(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        expected = [
            "parsed.md",
            "heading-page-map.json",
            "section-map.json",
            "abbreviations.json",
            "filter-report.json",
            "filtered.md",
            "manifest.json",
            "metadata.json",
            "recommendation-bundle.json",
            "assembly-report.json",
            "delivery-status.json",
        ]
        for name in expected:
            assert (out / name).exists(), f"Missing artifact: {name}"

    def test_dmn_directory_exists(self, pipeline_result):
        out = Path(pipeline_result["_output_dir"])
        assert (out / "dmn").is_dir(), "No dmn/ directory"
