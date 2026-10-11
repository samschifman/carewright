"""Tests for the Guideline Resolver node."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import acp_writer.api as api_module
from acp_writer.api import init_stores
from acp_writer.nodes.guideline_resolver import (
    _build_dependency_graph,
    _condition_matches_scope,
    guideline_resolver,
)
from acp_writer.store.embedding import FakeEmbeddingProvider
from cpg_contracts import CPGMetadata, DecisionModelSummary, DecisionVariable

PROJECT_ROOT = Path(__file__).parent.parent.parent
FIXTURES = PROJECT_ROOT / "shared" / "tests" / "fixtures"
DATA_DIR = PROJECT_ROOT / "mock-EHR" / "data"
DMN_DIR = Path(__file__).parent / "fixtures" / "dmn"


def _load_sample_metadata() -> CPGMetadata:
    data = json.loads((FIXTURES / "sample-recommendations.json").read_text())
    return CPGMetadata.model_validate(data["metadata"])


def _deploy_dmn(name: str):
    from acp_writer.api import _parse_dmn_metadata
    dmn_xml = (DMN_DIR / name).read_text()
    summary = _parse_dmn_metadata(dmn_xml)
    return summary.model_dump(mode="json")


def _resolve(state):
    return guideline_resolver(
        state,
        guidelines_store=api_module._guidelines_store,
        decision_models_provider=lambda: [
            entry["summary"].model_dump(mode="json")
            for entry in api_module._dynamic_models.values()
        ],
    )


def _mock_decision_engine_response(*dmn_names):
    models = [_deploy_dmn(n) for n in dmn_names]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = models
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture(autouse=True)
def reset_stores():
    init_stores(FakeEmbeddingProvider(dimensions=8))
    yield
    init_stores(FakeEmbeddingProvider(dimensions=8))


class TestConditionMatchesScope:
    def test_hypertension_matches(self):
        codes = [{"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"}]
        assert _condition_matches_scope(codes, "Adults (age 18+) with a new diagnosis of essential hypertension")

    def test_no_match(self):
        codes = [{"system": "http://snomed.info/sct", "code": "73211009", "display": "Diabetes mellitus"}]
        assert not _condition_matches_scope(codes, "Adults with essential hypertension")

    def test_none_scope_matches_all(self):
        codes = [{"system": "http://snomed.info/sct", "code": "12345"}]
        assert _condition_matches_scope(codes, None)

    def test_empty_codes(self):
        assert not _condition_matches_scope([], "Adults with hypertension")


class TestBuildDependencyGraph:
    def test_no_dependencies(self):
        models = [
            {"id": "a", "modifies": None},
            {"id": "b", "modifies": None},
        ]
        levels = _build_dependency_graph(models)
        assert len(levels) == 1
        assert set(levels[0]) == {"a", "b"}

    def test_linear_dependency(self):
        models = [
            {"id": "treatment", "modifies": ["monitoring"]},
            {"id": "monitoring", "modifies": None},
        ]
        levels = _build_dependency_graph(models)
        assert len(levels) == 2
        assert levels[0] == ["treatment"]
        assert levels[1] == ["monitoring"]

    def test_external_dependency_ignored(self):
        models = [
            {"id": "a", "modifies": ["external-model"]},
        ]
        levels = _build_dependency_graph(models)
        assert len(levels) == 1
        assert levels[0] == ["a"]

    def test_chained_golden_models_have_two_execution_levels(self):
        models = [
            {"id": "treatment-recommendation", "modifies": ["monitoring-plan"]},
            {"id": "monitoring-plan", "modifies": None},
        ]
        assert _build_dependency_graph(models) == [
            ["treatment-recommendation"], ["monitoring-plan"]
        ]


class TestGuidelineResolver:
    @patch("acp_writer.nodes.guideline_resolver.DECISION_ENGINE_URL", "http://decision-engine:8080")
    @patch("acp_writer.nodes.guideline_resolver.requests.get")
    def test_matches_hypertension_cpg(self, mock_get):
        metadata = _load_sample_metadata()
        api_module._guidelines_store.register(metadata)
        mock_get.return_value = _mock_decision_engine_response(
            "treatment-recommendation.dmn", "monitoring-plan.dmn"
        )

        state = {
            "condition_codes": [
                {"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"},
            ],
        }
        result = _resolve(state)

        assert len(result["applicable_cpgs"]) == 1
        assert result["applicable_cpgs"][0]["cpg_id"] == "SYN-HTN-2026-001"
        assert len(result["applicable_dmn_models"]) == 2
        assert len(result["dmn_dependency_graph"]) >= 1

    def test_no_matching_conditions(self):
        metadata = _load_sample_metadata()
        api_module._guidelines_store.register(metadata)

        state = {
            "condition_codes": [
                {"system": "http://snomed.info/sct", "code": "73211009", "display": "Diabetes mellitus"},
            ],
        }
        result = _resolve(state)
        assert result["applicable_cpgs"] == []
        assert result["applicable_dmn_models"] == []

    @patch("acp_writer.nodes.guideline_resolver.DECISION_ENGINE_URL", "http://decision-engine:8080")
    @patch("acp_writer.nodes.guideline_resolver.requests.get")
    def test_decision_engine_unavailable(self, mock_get):
        metadata = _load_sample_metadata()
        api_module._guidelines_store.register(metadata)
        mock_get.side_effect = Exception("Connection refused")

        state = {
            "condition_codes": [
                {"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"},
            ],
        }
        result = _resolve(state)
        assert len(result["applicable_cpgs"]) == 1
        assert result["applicable_dmn_models"] == []

    def test_no_registered_guidelines(self):
        state = {
            "condition_codes": [
                {"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"},
            ],
        }
        result = _resolve(state)
        assert result["applicable_cpgs"] == []

    def test_no_condition_codes(self):
        result = _resolve({})
        assert result["applicable_cpgs"] == []

    @patch("acp_writer.nodes.guideline_resolver.DECISION_ENGINE_URL", "http://decision-engine:8080")
    @patch("acp_writer.nodes.guideline_resolver.requests.get")
    def test_pipeline_integration(self, mock_get):
        """Full pipeline with Condition Scanner + Guideline Resolver."""
        metadata = _load_sample_metadata()
        api_module._guidelines_store.register(metadata)
        mock_get.return_value = _mock_decision_engine_response(
            "treatment-recommendation.dmn", "monitoring-plan.dmn"
        )

        bundle = json.loads((DATA_DIR / "patient-bundle-medication.json").read_text())

        with patch("acp_writer.nodes.plan_composer.get_llm") as mock_compose, \
             patch("acp_writer.nodes.brief_reviewer.get_llm") as mock_brief, \
             patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm") as mock_fhir:
            for mock_llm in [mock_compose, mock_brief, mock_fhir]:
                resp = MagicMock()
                resp.content = '{"patient_reference":"Patient/patient-1","applicable_cpgs":[],"goals":[],"activities":[],"conflicts":[],"review_status":"pending"}'
                m = MagicMock()
                m.invoke.return_value = resp
                mock_llm.return_value = m

            from .pipeline_helpers import build_pipeline
            graph = build_pipeline()
            compiled = graph.compile()
            result = compiled.invoke({"ips_bundle": bundle})

            assert len(result["applicable_cpgs"]) == 1
            assert len(result["applicable_dmn_models"]) == 2
