"""Tests for cpg_contracts types — serialization roundtrips and validation."""

import json
from pathlib import Path

from cpg_contracts import (
    Extraction,
    CPGMetadata,
    CONTRACT_VERSION,
    DecisionModelSummary,
    Recommendation,
    RecommendationBundle,
    SourceLocation,
    decision_model_id,
    DecisionVariable,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_source_location_full():
    loc = SourceLocation(
        page_start=12,
        page_end=13,
        bbox=[108.0, 273.0, 504.0, 176.8],
        source_text="All patients presenting with elevated blood pressure...",
    )
    assert loc.page_start == 12
    assert loc.page_end == 13
    assert len(loc.bbox) == 4
    data = loc.model_dump()
    assert SourceLocation(**data) == loc


def test_source_location_minimal():
    loc = SourceLocation(page_start=5)
    assert loc.page_end is None
    assert loc.bbox is None
    assert loc.source_text is None


def test_recommendation_with_source_location():
    rec = Recommendation(
        id="test-id",
        source_cpg="CPG-001",
        title="Test",
        content="Test content",
        recommendation_type="treatment",
        source_location=SourceLocation(page_start=10, page_end=11),
    )
    data = rec.model_dump()
    assert data["source_location"]["page_start"] == 10
    roundtrip = Recommendation(**data)
    assert roundtrip.source_location.page_start == 10


def test_recommendation_without_source_location():
    rec = Recommendation(
        id="test-id",
        source_cpg="CPG-001",
        title="Test",
        content="Test content",
        recommendation_type="treatment",
    )
    assert rec.source_location is None


def test_recommendation_can_link_automation_templates_additively():
    rec = Recommendation(
        id="rec-automation",
        source_cpg="CPG-001",
        title="Monitor readings",
        content="Submit readings weekly.",
        recommendation_type="monitoring",
        automation_template_ids=["home-bp-monitoring"],
    )

    assert Recommendation.model_validate(rec.model_dump()).automation_template_ids == [
        "home-bp-monitoring"
    ]


def test_contract_version_bumps_without_rejecting_legacy_payloads():
    assert CONTRACT_VERSION == "1.1"
    assert CPGMetadata(cpg_id="CPG-002", title="Test guideline").contract_version == "1.1"
    assert RecommendationBundle(source_cpg="CPG-002", recommendations=[]).contract_version == "1.1"

    raw = json.loads((FIXTURES / "sample-recommendations.json").read_text())
    assert CPGMetadata.model_validate(raw["metadata"]).contract_version == "1.0"
    assert RecommendationBundle.model_validate(raw["recommendation_bundle"]).contract_version == "1.0"


def test_decision_model_summary_with_source_location():
    dm = DecisionModelSummary(
        id="dm-1",
        name="BP Treatment",
        inputs=[],
        outputs=[],
        source_cpg="CPG-001",
        source_location=SourceLocation(
            page_start=47,
            page_end=48,
            source_text="Table 3. Blood pressure treatment thresholds",
        ),
    )
    data = dm.model_dump()
    assert data["source_location"]["page_start"] == 47
    assert data["source_location"]["source_text"].startswith("Table 3")


def test_decision_summary_namespace_is_optional_and_roundtrips():
    summary = DecisionModelSummary(
        id="dm-namespace",
        name="Monitoring Plan",
        inputs=[],
        outputs=[],
        namespace="https://example.org/dmn/monitoring-plan",
    )

    assert DecisionModelSummary.model_validate(summary.model_dump()).namespace == summary.namespace
    assert DecisionModelSummary(id="dm-legacy", name="Legacy", inputs=[], outputs=[]).namespace is None


def test_decision_model_id_is_stable():
    assert decision_model_id("Treatment Recommendation") == "treatment-recommendation"
    assert decision_model_id("BP / CKD: follow-up") == "bp-ckd-follow-up"


def test_decision_variable_extraction_roundtrip():
    variable = DecisionVariable(
        name="Systolic BP",
        type="number",
        extraction={"function": "observation_count", "params": {
            "code": "http://loinc.org|8480-6", "duration": "P3M",
        }},
    )
    assert DecisionVariable.model_validate(variable.model_dump()).extraction.params["duration"] == "P3M"


def test_extraction_contract_rejects_invalid_parameters():
    import pytest

    with pytest.raises(ValueError, match="system\\|code"):
        Extraction(function="observation_count", params={"code": "8480-6", "duration": "P3M"})
    with pytest.raises(ValueError, match="comparator"):
        Extraction(function="observation_count", params={
            "code": "http://loinc.org|8480-6", "duration": "P3M",
            "threshold": 9, "comparator": "gte",
        })
        with pytest.raises(ValueError, match="not boolean"):
            Extraction(function="consecutive_above", params={
                "code": "http://loinc.org|8480-6", "threshold": True,
            })


def test_consecutive_above_roundtrips_without_comparator():
    extraction = Extraction(function="consecutive_above", params={
        "code": "http://loinc.org|8480-6", "threshold": 140,
    })
    assert Extraction.model_validate(extraction.model_dump()) == extraction


def test_consecutive_above_rejects_comparator():
    import pytest

    with pytest.raises(ValueError, match="comparator is not supported"):
        Extraction(function="consecutive_above", params={
            "code": "http://loinc.org|8480-6", "threshold": 140, "comparator": "ge",
        })


def test_extraction_contract_rejects_unknown_function_and_missing_parameter():
    import pytest

    with pytest.raises(ValueError):
        Extraction(function="unknown", params={})
    with pytest.raises(ValueError, match="duration"):
        Extraction(function="rate_of_change", params={
            "code": "http://loinc.org|8480-6",
        })


def test_sample_fixture_roundtrip():
    raw = json.loads((FIXTURES / "sample-recommendations.json").read_text())
    metadata = CPGMetadata(**raw["metadata"])
    bundle = RecommendationBundle(**raw["recommendation_bundle"])
    assert metadata.cpg_id == bundle.source_cpg

    with_location = [r for r in bundle.recommendations if r.source_location]
    assert len(with_location) >= 2
    for rec in with_location:
        assert rec.source_location.page_start > 0
        assert rec.source_location.source_text is not None

    without_location = [r for r in bundle.recommendations if not r.source_location]
    assert len(without_location) > 0
