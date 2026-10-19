"""Tests for versioned BPMN template publication."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from cpg_ingester.nodes.delivery import delivery

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden" / "automation"


def _store():
    store = MagicMock()
    store.bucket = "cpg-artifacts"
    store.put.side_effect = lambda key, value: f"cpg-artifacts:{key}"
    store.put_raw.side_effect = lambda key, value, content_type: f"cpg-artifacts:{key}"
    return store


def _template():
    return json.loads(
        (GOLDEN_DIR / "home-bp-monitoring.template.json").read_text()
    )


def test_publishes_template_bpmn_and_latest_pointer(tmp_path):
    store = _store()
    template = _template()
    result = delivery({
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "bpmn_results": [{
            "automation_template": template,
            "template_summary": template["summary"],
            "item": {"name": template["summary"]["name"]},
        }],
        "artifact_store": store,
        "output_dir": str(tmp_path),
    })

    template_key = (
        "published/SYN-HTN-2026-002/bpmn/home-bp-monitoring/"
        f"{template['summary']['version']}.template.json"
    )
    bpmn_key = (
        "published/SYN-HTN-2026-002/bpmn/home-bp-monitoring/"
        f"{template['summary']['version']}.bpmn"
    )
    latest_key = "published/SYN-HTN-2026-002/bpmn/home-bp-monitoring/latest.json"

    assert [call.args[0] for call in store.put.call_args_list] == [
        "published/SYN-HTN-2026-002/metadata.json",
        template_key,
        latest_key,
    ]
    assert store.put_raw.call_args.args[0] == bpmn_key
    assert store.put_raw.call_args.args[2] == "application/xml"
    bpmn_artifact = next(
        item for item in result["delivery_status"]["artifacts"]
        if item["type"] == "bpmn"
    )
    assert bpmn_artifact == {
        "type": "bpmn",
        "ref": f"cpg-artifacts:{template_key}",
        "name": template["summary"]["name"],
        "artifact_id": template["summary"]["artifact_id"],
        "template_id": "home-bp-monitoring",
        "version": template["summary"]["version"],
    }
    latest = store.put.call_args_list[2].args[1]
    assert latest["version"] == template["summary"]["version"]
    assert latest["template_ref"] == f"cpg-artifacts:{template_key}"
    assert latest["bpmn_ref"] == f"cpg-artifacts:{bpmn_key}"


def test_escalated_template_is_not_published(tmp_path):
    store = _store()
    result = delivery({
        "cpg_metadata": {},
        "bpmn_results": [{
            "escalated": True,
            "automation_template": _template(),
        }],
        "artifact_store": store,
        "output_dir": str(tmp_path),
    })

    assert not any(
        item["type"] == "bpmn" for item in result["delivery_status"]["artifacts"]
    )
    assert store.put.call_count == 0
    assert store.put_raw.call_count == 0


def test_acknowledged_l7_template_is_published_with_validation_note(tmp_path):
    store = _store()
    template = _template()
    template["summary"]["validation"].update({
        "status": "escalated",
        "errors": ["Review was inconclusive."],
        "acknowledgement": "The care team accepted the remaining review note.",
    })
    result = delivery({
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "bpmn_results": [{
            "escalated": False,
            "automation_template": template,
            "template_summary": template["summary"],
        }],
        "artifact_store": store,
        "output_dir": str(tmp_path),
    })

    assert any(item["type"] == "bpmn" for item in result["delivery_status"]["artifacts"])
    published = next(
        call.args[1] for call in store.put.call_args_list
        if call.args[0].endswith(".template.json")
    )
    assert published["summary"]["validation"]["acknowledgement"] == (
        "The care team accepted the remaining review note."
    )
