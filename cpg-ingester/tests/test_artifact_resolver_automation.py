"""Tests for BPMN result hydration into cpg-ingester run details."""

from cpg_ingester.artifact_id import make_artifact_id
from cpg_ingester.services.artifact_resolver import enrich_run_detail


def test_resolves_generated_bpmn_results_as_automations():
    generated = {
        "bpmn_results": [{
            "bpmn_xml": "<definitions/>",
            "process_ir": {"process": {"id": "home-bp-monitoring"}},
            "template_summary": {
                "id": "home-bp-monitoring",
                "name": "Home BP monitoring",
                "section": "3.5",
            },
            "item": {"name": "Home BP monitoring", "section": "3.5"},
        }],
    }
    detail = {
        "metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "workflowData": {"generateResult": generated},
    }

    result = enrich_run_detail(detail, None)

    assert result["automations"] == generated["bpmn_results"]
    assert result["automations"][0]["artifact_id"] == make_artifact_id(
        "SYN-HTN-2026-002", "bpmn", "Home BP monitoring", "3.5"
    )
