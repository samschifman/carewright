"""End-to-end integration tests for the acp-writer pipeline.

These tests require:
  - LITELLM_URL env var pointing to a running LiteLLM proxy
  - Kogito decision service is NOT required (DMN uses JIT via api._evaluate_jit)

Run with: LITELLM_URL=http://localhost:4000 pytest tests/test_e2e.py -v
"""

import json
import os
from pathlib import Path

import pytest

import acp_writer.api as api_module
from acp_writer.api import _dynamic_models, init_stores
from acp_writer.nodes.fhir_server_writer import _care_plans
from .pipeline_helpers import build_pipeline
from acp_writer.store.embedding import FakeEmbeddingProvider

# Prefer LLM_BASE_URL per the no-tech-specific-names convention; fall back to
# the legacy LITELLM_URL. The state key remains ``litellm_url`` (get_llm reads it).
LITELLM_URL = os.environ.get("LLM_BASE_URL") or os.environ.get("LITELLM_URL")
pytestmark = pytest.mark.skipif(
    not LITELLM_URL, reason="LLM_BASE_URL (or legacy LITELLM_URL) not set"
)

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "mock-EHR" / "data"
DMN_DIR = Path(__file__).parent / "fixtures" / "dmn"
FIXTURES = PROJECT_ROOT / "shared" / "tests" / "fixtures"
INT_FIXTURES = PROJECT_ROOT / "tests" / "integration" / "fixtures"


def _load_bundle(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text())


def _load_sample_data() -> dict:
    return json.loads((FIXTURES / "sample-recommendations.json").read_text())


def _deploy_dmn(name: str):
    from acp_writer.api import _parse_dmn_metadata
    dmn_xml = (DMN_DIR / name).read_text()
    summary = _parse_dmn_metadata(dmn_xml)
    _dynamic_models[summary.id] = {"summary": summary, "dmn_xml": dmn_xml}


def _setup_hypertension_scenario():
    """Register CPG, deploy DMN models, ingest recommendations."""
    data = _load_sample_data()
    from cpg_contracts import CPGMetadata, RecommendationBundle

    metadata = CPGMetadata.model_validate(data["metadata"])
    api_module._guidelines_store.register(metadata)

    _deploy_dmn("treatment-recommendation.dmn")
    _deploy_dmn("monitoring-plan.dmn")

    bundle = RecommendationBundle.model_validate(data["recommendation_bundle"])
    api_module._vector_store.add_batch(bundle.recommendations)


def _ingest_cpg_fixture(name: str):
    """Register CPG metadata + ingest its recommendations from the repo-root
    integration fixtures (tests/integration/fixtures)."""
    from cpg_contracts import CPGMetadata, RecommendationBundle

    data = json.loads((INT_FIXTURES / name).read_text())
    metadata = CPGMetadata.model_validate(data["metadata"])
    api_module._guidelines_store.register(metadata)
    bundle = RecommendationBundle.model_validate(data["recommendation_bundle"])
    api_module._vector_store.add_batch(bundle.recommendations)


def _setup_conflicting_scenario():
    """Register + ingest BOTH the hypertension and diabetes fixture CPGs, which
    are deliberately engineered to conflict (overlapping diet advice, divergent
    BP targets, and opposite lisinopril titration directives)."""
    _ingest_cpg_fixture("htn-cpg.json")
    _ingest_cpg_fixture("diabetes-cpg.json")


@pytest.fixture(autouse=True)
def reset():
    _dynamic_models.clear()
    _care_plans.clear()
    init_stores(FakeEmbeddingProvider(dimensions=8))
    yield
    _dynamic_models.clear()
    _care_plans.clear()


def _find_resources(bundle: dict, resource_type: str) -> list[dict]:
    return [
        e["resource"]
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == resource_type
    ]


class TestEndToEnd:
    def test_full_pipeline_medication_patient(self, tmp_path):
        """Full pipeline: IPS → condition scan → guideline resolve → DMN →
        recs → plan compose → review → FHIR generate → validate → write."""
        _setup_hypertension_scenario()
        bundle = _load_bundle("patient-bundle-medication.json")

        graph = build_pipeline()
        compiled = graph.compile()

        result = compiled.invoke({
            "ips_bundle": bundle,
            "run_id": "e2e-med-001",
            "output_dir": str(tmp_path),
            "litellm_url": LITELLM_URL,
            "llm_model": os.environ.get("LLM_MODEL", "default"),
            "llm_api_key": os.environ.get("LLM_API_KEY", "sk-change-me"),
        })

        assert result["patient_reference"] == "Patient/patient-1"

        assert len(result["condition_codes"]) >= 2
        snomed_codes = {c["code"] for c in result["condition_codes"]
                        if c["system"] == "http://snomed.info/sct"}
        assert "59621000" in snomed_codes

        assert len(result["applicable_cpgs"]) >= 1

        assert len(result["recommendations"]) > 0

        brief = result.get("planning_brief", {})
        assert len(brief.get("goals", [])) >= 1
        assert len(brief.get("activities", [])) >= 1

        fhir_bundle = result.get("fhir_bundle", {})
        assert fhir_bundle["resourceType"] == "Bundle"
        assert fhir_bundle["type"] == "transaction"
        assert len(fhir_bundle["entry"]) > 0

        careplans = _find_resources(fhir_bundle, "CarePlan")
        assert len(careplans) == 1
        assert careplans[0]["status"] == "draft"

        goals = _find_resources(fhir_bundle, "Goal")
        assert len(goals) >= 1

        devices = _find_resources(fhir_bundle, "Device")
        assert len(devices) == 1

        provs = _find_resources(fhir_bundle, "Provenance")
        assert len(provs) >= 1

        for entry in fhir_bundle["entry"]:
            resource = entry["resource"]
            security = resource.get("meta", {}).get("security", [])
            aiast = [s for s in security if s.get("code") == "AIAST"]
            assert len(aiast) >= 1, f"Missing AIAST on {resource['resourceType']}"

        assert result.get("delivery_status") in ("delivered", "stored_locally", "skipped")

        assert (tmp_path / "planning-brief.json").exists()
        assert (tmp_path / "fhir-bundle.json").exists()

    def test_full_pipeline_lifestyle_patient(self, tmp_path):
        """Lifestyle patient — fewer activities, no medications expected."""
        _setup_hypertension_scenario()
        bundle = _load_bundle("patient-bundle-lifestyle.json")

        graph = build_pipeline()
        compiled = graph.compile()

        result = compiled.invoke({
            "ips_bundle": bundle,
            "run_id": "e2e-life-001",
            "output_dir": str(tmp_path),
            "litellm_url": LITELLM_URL,
            "llm_model": os.environ.get("LLM_MODEL", "default"),
            "llm_api_key": os.environ.get("LLM_API_KEY", "sk-change-me"),
        })

        assert result["patient_reference"] == "Patient/patient-2"
        assert len(result["condition_codes"]) >= 1

        fhir_bundle = result.get("fhir_bundle", {})
        assert fhir_bundle["resourceType"] == "Bundle"
        assert len(fhir_bundle["entry"]) > 0

    def test_comprehensive_patient_surfaces_conflicts(self, tmp_path):
        """Full-pipeline smoke for issue #169: the comprehensive multimorbidity
        patient, run against two deliberately-conflicting CPGs, must yield a
        bundle carrying at least one conflict Provenance and the CarePlan
        conflict marker extension."""
        from acp_writer.services.ai_transparency import is_conflict_provenance

        _setup_conflicting_scenario()
        bundle = _load_bundle("patient-bundle-comprehensive.json")

        graph = build_pipeline()
        compiled = graph.compile()

        result = compiled.invoke({
            "ips_bundle": bundle,
            "run_id": "e2e-conflict-001",
            "output_dir": str(tmp_path),
            "litellm_url": LITELLM_URL,
            "llm_model": os.environ.get("LLM_MODEL", "default"),
            "llm_api_key": os.environ.get("LLM_API_KEY", "sk-change-me"),
        })

        # Both CPGs should match the patient's conditions and be retrieved.
        cpg_ids = {c.get("cpg_id") for c in result.get("applicable_cpgs", [])}
        assert {"SYN-HTN-2026-001", "SYN-DM2-2026-001"} <= cpg_ids, (
            f"expected both fixture CPGs to match, got {cpg_ids}"
        )

        conflicts = result.get("planning_brief", {}).get("conflicts", [])
        assert conflicts, "conflict_analyst flagged no conflicts on a conflicting plan"

        fhir_bundle = result.get("fhir_bundle", {})

        # (a) at least one conflict Provenance (identified by the conflict-id ext)
        conflict_provs = [
            p for p in _find_resources(fhir_bundle, "Provenance")
            if is_conflict_provenance(p)
        ]
        assert conflict_provs, "no conflict Provenance in the generated bundle"

        # (b) exactly one CarePlan marker extension
        careplans = _find_resources(fhir_bundle, "CarePlan")
        assert len(careplans) == 1
        markers = [
            e for e in careplans[0].get("extension", [])
            if e.get("url", "").endswith("/careplan-conflict-detected")
        ]
        assert len(markers) == 1, f"expected exactly one conflict marker, got {len(markers)}"
        assert markers[0].get("valueBoolean") is True

    def test_pipeline_without_registered_guidelines(self, tmp_path):
        """Pipeline should complete even with no registered guidelines."""
        bundle = _load_bundle("patient-bundle-medication.json")

        graph = build_pipeline()
        compiled = graph.compile()

        result = compiled.invoke({
            "ips_bundle": bundle,
            "run_id": "e2e-no-cpg-001",
            "output_dir": str(tmp_path),
            "litellm_url": LITELLM_URL,
            "llm_model": os.environ.get("LLM_MODEL", "default"),
            "llm_api_key": os.environ.get("LLM_API_KEY", "sk-change-me"),
        })

        assert result["patient_reference"] == "Patient/patient-1"
        assert result.get("applicable_cpgs") == []
        assert "delivery_status" in result
