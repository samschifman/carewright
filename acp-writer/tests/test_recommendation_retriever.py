"""Tests for the Recommendation Retriever node."""

import json
from pathlib import Path

import pytest

import acp_writer.api as api_module
from acp_writer.api import _dynamic_models, init_stores
from acp_writer.nodes.recommendation_retriever import (
    _build_search_query,
    recommendation_retriever,
)
from acp_writer.store.embedding import FakeEmbeddingProvider

PROJECT_ROOT = Path(__file__).parent.parent.parent
FIXTURES = PROJECT_ROOT / "shared" / "tests" / "fixtures"


def _load_sample_data() -> dict:
    return json.loads((FIXTURES / "sample-recommendations.json").read_text())


def _retrieve(state):
    return recommendation_retriever(state, vector_store=api_module._vector_store)


def _ingest_recommendations():
    from cpg_contracts import RecommendationBundle
    data = _load_sample_data()
    bundle = RecommendationBundle.model_validate(data["recommendation_bundle"])
    api_module._vector_store.add_batch(bundle.recommendations)


@pytest.fixture(autouse=True)
def reset_stores():
    _dynamic_models.clear()
    init_stores(FakeEmbeddingProvider(dimensions=8))
    yield
    _dynamic_models.clear()


class TestBuildSearchQuery:
    def test_from_conditions(self):
        codes = [{"display": "Essential hypertension"}, {"display": "Type 2 diabetes"}]
        query = _build_search_query(codes, [])
        assert "Essential hypertension" in query
        assert "Type 2 diabetes" in query

    def test_from_dmn_results(self):
        results = [{
            "outputs": {
                "Treatment Recommendation": {
                    "Action": "Start medication",
                    "Medication": "Lisinopril",
                }
            }
        }]
        query = _build_search_query([], results)
        assert "Start medication" in query
        assert "Lisinopril" in query

    def test_empty_inputs(self):
        query = _build_search_query([], [])
        assert query == "clinical recommendation"


class TestRecommendationRetriever:
    def test_empty_store(self):
        result = _retrieve({
            "condition_codes": [{"display": "hypertension"}],
            "dmn_results": [],
            "applicable_cpgs": [],
        })
        assert result["recommendations"] == []

    def test_retrieves_from_store(self):
        _ingest_recommendations()
        result = _retrieve({
            "condition_codes": [{"display": "Essential hypertension"}],
            "dmn_results": [],
            "applicable_cpgs": [{"cpg_id": "SYN-HTN-2026-001"}],
        })
        assert len(result["recommendations"]) > 0
        for rec in result["recommendations"]:
            assert rec["source_cpg"] == "SYN-HTN-2026-001"
            assert "id" in rec
            assert "title" in rec
            assert "content" in rec

    def test_no_duplicates(self):
        _ingest_recommendations()
        result = _retrieve({
            "condition_codes": [{"display": "Essential hypertension"}],
            "dmn_results": [],
            "applicable_cpgs": [{"cpg_id": "SYN-HTN-2026-001"}],
        })
        ids = [r["id"] for r in result["recommendations"]]
        assert len(ids) == len(set(ids))

    def test_with_dmn_results(self):
        _ingest_recommendations()
        result = _retrieve({
            "condition_codes": [{"display": "Essential hypertension"}],
            "dmn_results": [{
                "outputs": {
                    "Treatment Recommendation": {
                        "Action": "Start medication",
                        "Medication": "Lisinopril",
                    }
                }
            }],
            "applicable_cpgs": [{"cpg_id": "SYN-HTN-2026-001"}],
        })
        assert len(result["recommendations"]) > 0

    def test_no_cpg_filter_returns_all(self):
        _ingest_recommendations()
        result = _retrieve({
            "condition_codes": [{"display": "hypertension treatment"}],
            "dmn_results": [],
            "applicable_cpgs": [],
        })
        assert len(result["recommendations"]) > 0

    def test_full_recommendation_objects(self):
        _ingest_recommendations()
        result = _retrieve({
            "condition_codes": [{"display": "Essential hypertension"}],
            "dmn_results": [],
            "applicable_cpgs": [{"cpg_id": "SYN-HTN-2026-001"}],
        })
        rec = result["recommendations"][0]
        assert "recommendation_type" in rec
        assert "source_cpg" in rec
