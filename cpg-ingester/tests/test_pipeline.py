"""Integration tests for the cpg-ingester pipeline."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from langgraph.checkpoint.memory import MemorySaver

from cpg_ingester import cli
from cpg_ingester.output import write_artifact
from cpg_ingester.pipeline import build_pipeline

requires_llm = pytest.mark.skipif(
    not os.environ.get("LITELLM_URL"),
    reason="Requires LITELLM_URL env var (running LiteLLM instance)",
)

SYNTHETIC_CPG = Path(__file__).parent.parent / "data" / "synthetic-hypertension-cpg.pdf"
GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden"


# --- Structural tests (no LLM needed) ---

def test_pipeline_builds():
    graph = build_pipeline()
    assert graph is not None


def test_pipeline_compiles():
    graph = build_pipeline()
    compiled = graph.compile()
    assert compiled is not None


def test_pipeline_compiles_with_checkpointer():
    graph = build_pipeline()
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)
    assert compiled is not None


def test_pipeline_has_expected_nodes():
    graph = build_pipeline()
    compiled = graph.compile()
    node_names = set(compiled.get_graph().nodes.keys())
    expected = {
        "docling_agent", "structure_analyzer", "content_filter",
        "item_identifier", "classification_reviewer", "metadata_extractor",
        "generate", "assembly", "delivery",
    }
    assert expected.issubset(node_names), f"Missing nodes: {expected - node_names}"


def test_pipeline_graph_has_edges():
    graph = build_pipeline()
    compiled = graph.compile()
    mermaid = compiled.get_graph().draw_mermaid()
    assert "docling_agent" in mermaid
    assert "structure_analyzer" in mermaid
    assert "assembly" in mermaid
    assert "delivery" in mermaid


# --- Output helper tests ---

def test_write_artifact_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        data = {"key": "value", "count": 42}
        path = write_artifact(tmpdir, "test.json", data)
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["key"] == "value"


def test_write_artifact_string():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_artifact(tmpdir, "test.xml", "<root/>")
        assert path.exists()
        assert path.read_text() == "<root/>"


def test_write_artifact_nested_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = write_artifact(tmpdir, "dmn/table-1.dmn", "<definitions/>")
        assert path.exists()
        assert path.parent.name == "dmn"


def test_cli_does_not_put_artifact_store_client_in_checkpointed_state(tmp_path, monkeypatch):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"test")
    output_dir = tmp_path / "run"

    class FakeGraph:
        def compile(self, checkpointer=None):
            return self

        def invoke(self, state, config=None):
            assert "artifact_store" not in state
            return {}

    monkeypatch.setattr(cli, "build_pipeline", FakeGraph)

    result = CliRunner().invoke(
        cli.main, [str(pdf), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 0, result.output
    assert json.loads((output_dir / "run-summary.json").read_text())["delivery_status"] is None


# --- Full pipeline integration tests (require LLM) ---

@pytest.mark.skipif(not SYNTHETIC_CPG.exists(), reason="Synthetic CPG PDF not found")
@requires_llm
class TestFullPipelineIntegration:

    @pytest.fixture(autouse=True)
    def run_pipeline(self, tmp_path):
        """Run the full pipeline once and share the result across tests."""
        graph = build_pipeline()
        checkpointer = MemorySaver()
        compiled = graph.compile(checkpointer=checkpointer)

        self.output_dir = str(tmp_path)
        state = {
            "run_id": "integration-test",
            "output_dir": self.output_dir,
            "pdf_path": str(SYNTHETIC_CPG),
            "litellm_url": os.environ.get("LITELLM_URL", "http://localhost:4000"),
            "llm_model": os.environ.get("LLM_MODEL", "default"),
        }
        config = {"configurable": {"thread_id": "integration-test"}}
        self.result = compiled.invoke(state, config=config)

    def test_produces_markdown(self):
        assert len(self.result.get("markdown", "")) > 1000

    def test_produces_section_map(self):
        section_map = self.result.get("section_map", [])
        assert len(section_map) > 5
        classifications = {s["classification"] for s in section_map}
        assert "skip" in classifications or "recommendation" in classifications

    def test_produces_item_manifest(self):
        manifest = self.result.get("item_manifest", [])
        assert len(manifest) > 0
        decisions = [i for i in manifest if i.get("type") == "decision"]
        recs = [i for i in manifest if i.get("type") == "recommendation"]
        assert len(decisions) >= 1
        assert len(recs) >= 1

    def test_produces_cpg_metadata(self):
        meta = self.result.get("cpg_metadata", {})
        assert meta.get("cpg_id")
        assert meta.get("title")
        assert "hypertension" in meta.get("title", "").lower() or "HTN" in meta.get("cpg_id", "")

    def test_produces_abbreviations(self):
        abbrs = self.result.get("abbreviations", {})
        assert len(abbrs) > 0

    def test_writes_output_artifacts(self):
        p = Path(self.output_dir)
        assert (p / "parsed.md").exists()
        assert (p / "section-map.json").exists()
        assert (p / "manifest.json").exists()
        assert (p / "metadata.json").exists()

    def test_all_manifest_items_have_guids(self):
        manifest = self.result.get("item_manifest", [])
        for item in manifest:
            assert "id" in item
            assert len(item["id"]) == 36

    def test_recommendations_match_expected_count(self):
        """Synthetic CPG should produce roughly 10-15 recommendations."""
        manifest = self.result.get("item_manifest", [])
        recs = [i for i in manifest if i.get("type") == "recommendation"]
        assert 5 <= len(recs) <= 20, f"Got {len(recs)} recs, expected 5-20"

    def test_decisions_match_expected_count(self):
        """Synthetic CPG has 2 decision tables."""
        manifest = self.result.get("item_manifest", [])
        decisions = [i for i in manifest if i.get("type") == "decision"]
        assert 1 <= len(decisions) <= 5, f"Got {len(decisions)} decisions, expected 1-5"


# --- Golden file validation (no LLM needed, validates existing golden files) ---

class TestGoldenFiles:

    @pytest.mark.skipif(not GOLDEN_DIR.exists(), reason="Golden DMN dir not found")
    def test_golden_dmn_files_exist(self):
        dmn_files = list(GOLDEN_DIR.glob("*.dmn"))
        assert len(dmn_files) >= 2

    @pytest.mark.skipif(not GOLDEN_DIR.exists(), reason="Golden DMN dir not found")
    def test_golden_dmn_files_are_valid_xml(self):
        from cpg_ingester.validators.dmn_syntax import validate_dmn_xml

        for dmn_file in GOLDEN_DIR.glob("*.dmn"):
            errors = validate_dmn_xml(dmn_file.read_text())
            assert errors == [], f"Golden file {dmn_file.name} has errors: {errors}"

    def test_sample_recommendations_fixture_exists(self):
        fixture = Path(__file__).parents[2] / "shared" / "tests" / "fixtures" / "sample-recommendations.json"
        if fixture.exists():
            data = json.loads(fixture.read_text())
            bundle = data.get("recommendation_bundle", {})
            recs = bundle.get("recommendations", [])
            assert len(recs) == 12

    def test_sample_fixture_recs_have_valid_types(self):
        fixture = Path(__file__).parents[2] / "shared" / "tests" / "fixtures" / "sample-recommendations.json"
        if not fixture.exists():
            pytest.skip("Fixture not found")
        data = json.loads(fixture.read_text())
        valid_types = {"treatment", "diagnostic", "monitoring", "lifestyle", "educational", "referral", "screening", "contraindication", "process"}
        for rec in data["recommendation_bundle"]["recommendations"]:
            assert rec["recommendation_type"] in valid_types, f"Invalid type: {rec['recommendation_type']}"
