"""Tests for Assembly Agent and Delivery Agent nodes."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cpg_ingester.nodes.assembly import (
    _check_integrity,
    _resolve_cross_references,
    assembly,
)
from cpg_ingester.nodes.delivery import delivery


# --- Assembly tests ---

class TestCrossReferenceResolution:

    def test_resolves_valid_string_refs(self):
        recs = [
            {"id": "aaa", "cross_references": ["bbb"]},
            {"id": "bbb", "cross_references": ["aaa"]},
        ]
        result = _resolve_cross_references(recs, [])
        assert result[0]["cross_references"][0]["target_id"] == "bbb"
        assert result[1]["cross_references"][0]["target_id"] == "aaa"

    def test_resolves_valid_dict_refs(self):
        recs = [
            {"id": "aaa", "cross_references": [{"target_id": "bbb", "relationship": "related"}]},
            {"id": "bbb", "cross_references": []},
        ]
        result = _resolve_cross_references(recs, [])
        assert result[0]["cross_references"][0]["target_id"] == "bbb"

    def test_removes_missing_refs(self):
        recs = [
            {"id": "aaa", "cross_references": ["nonexistent"]},
        ]
        result = _resolve_cross_references(recs, [])
        assert result[0]["cross_references"] is None

    def test_resolves_refs_to_dmn(self):
        recs = [
            {"id": "aaa", "cross_references": ["dmn-1"]},
        ]
        dmn_results = [{"decision_model_summary": {"id": "dmn-1"}}]
        result = _resolve_cross_references(recs, dmn_results)
        assert result[0]["cross_references"][0]["target_id"] == "dmn-1"

    def test_handles_empty_refs(self):
        recs = [{"id": "aaa", "cross_references": []}]
        result = _resolve_cross_references(recs, [])
        assert result[0]["cross_references"] is None

    def test_handles_null_refs(self):
        recs = [{"id": "aaa", "cross_references": None}]
        result = _resolve_cross_references(recs, [])
        assert result[0]["cross_references"] is None


class TestIntegrityChecks:

    def test_passes_valid_data(self):
        recs = [{"id": "aaa", "source_cpg": "CPG-001"}]
        dmn = [{"decision_model_summary": {"id": "dmn-1"}}]
        errors = _check_integrity(recs, dmn, {"cpg_id": "CPG-001"})
        assert errors == []

    def test_catches_duplicate_rec_ids(self):
        recs = [{"id": "aaa"}, {"id": "aaa"}]
        errors = _check_integrity(recs, [], {"cpg_id": "CPG-001"})
        assert any("Duplicate recommendation" in e for e in errors)

    def test_catches_mismatched_source_cpg(self):
        recs = [{"id": "aaa", "source_cpg": "WRONG"}]
        errors = _check_integrity(recs, [], {"cpg_id": "CPG-001"})
        assert any("source_cpg" in e for e in errors)

    def test_tbd_source_cpg_is_ok(self):
        recs = [{"id": "aaa", "source_cpg": "TBD"}]
        errors = _check_integrity(recs, [], {"cpg_id": "CPG-001"})
        assert errors == []

    def test_catches_empty_output(self):
        errors = _check_integrity([], [], {"cpg_id": "CPG-001"})
        assert any("No recommendations" in e for e in errors)


class TestAssemblyNode:

    def _write_rec_file(self, tmpdir, filename, recs):
        Path(tmpdir, filename).write_text(json.dumps(recs))

    def _write_dmn_file(self, tmpdir, name, xml="<definitions/>"):
        dmn_dir = Path(tmpdir) / "dmn"
        dmn_dir.mkdir(exist_ok=True)
        (dmn_dir / f"{name}.dmn").write_text(xml)

    def test_assembles_recommendations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_rec_file(tmpdir, "recommendations-3.1.json", [
                {"id": "r1", "source_cpg": "TBD", "title": "A", "cross_references": []},
                {"id": "r2", "source_cpg": "TBD", "title": "B", "cross_references": []},
            ])
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001", "contract_version": "1.0"},
                "item_manifest": [],
                "output_dir": tmpdir,
            }
            result = assembly(state)
            assert len(result["recommendation_results"]) == 2
            assert all(r["source_cpg"] == "CPG-001" for r in result["recommendation_results"])

    def test_fills_tbd_source_cpg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_rec_file(tmpdir, "recommendations-3.1.json", [
                {"id": "r1", "source_cpg": "TBD", "cross_references": []},
            ])
            state = {
                "cpg_metadata": {"cpg_id": "MY-CPG"},
                "item_manifest": [],
                "output_dir": tmpdir,
            }
            result = assembly(state)
            assert result["recommendation_results"][0]["source_cpg"] == "MY-CPG"

    def test_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_rec_file(tmpdir, "recommendations-3.1.json", [
                {"id": "r1", "source_cpg": "TBD", "cross_references": []},
            ])
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001"},
                "item_manifest": [],
                "output_dir": tmpdir,
            }
            assembly(state)
            assert (Path(tmpdir) / "recommendation-bundle.json").exists()
            assert (Path(tmpdir) / "assembly-report.json").exists()

    def test_collects_escalated_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001"},
                # Escalations are carried on the generated results, not the manifest.
                "dmn_results": [{
                    "dmn_xml": "<definitions/>",
                    "item": {"name": "Escalated Decision"},
                    "escalated": True,
                    "escalation_reason": "syntax-budget-exhausted",
                    "escalation_errors": ["Missing hitPolicy attribute"],
                }],
                "output_dir": tmpdir,
            }
            result = assembly(state)
            assert len(result["escalated_items"]) >= 1
            escalated = result["escalated_items"][0]
            assert escalated["name"] == "Escalated Decision"
            assert escalated["escalation_reason"] == "syntax-budget-exhausted"
            assert (Path(tmpdir) / "escalated-items.json").exists()

    def test_collects_recommendation_escalation_details(self):
        result = assembly({
            "cpg_metadata": {"cpg_id": "CPG-001"},
            "recommendation_results": [{
                "id": "rec-1", "title": "Use therapy", "escalated": True,
                "escalation_reason": "semantic-budget-exhausted",
                "escalation_errors": ["unsupported claim"],
            }],
            "output_dir": tempfile.mkdtemp(),
        })
        assert result["escalated_items"] == [{
            "type": "recommendation", "id": "rec-1", "name": "Use therapy",
            "escalation_reason": "semantic-budget-exhausted",
            "escalation_errors": ["unsupported claim"],
        }]

    def test_recommendation_escalation_without_errors_keeps_empty_list(self):
        result = assembly({
            "cpg_metadata": {"cpg_id": "CPG-001"},
            "recommendation_results": [{
                "id": "rec-1", "title": "Use therapy", "escalated": True,
            }],
            "output_dir": tempfile.mkdtemp(),
        })
        assert result["escalated_items"][0]["name"] == "Use therapy"
        assert result["escalated_items"][0]["escalation_errors"] == []

    def test_collects_section_escalation_without_polluting_recommendations(self):
        section_escalation = {
            "type": "recommendation", "id": "Section 7", "name": "Section: Section 7",
            "section": "Section 7", "escalation_reason": "generation-exception",
            "escalation_errors": ["boom"],
        }
        result = assembly({
            "cpg_metadata": {"cpg_id": "CPG-001"},
            "recommendation_results": [],
            "recommendation_escalations": [section_escalation],
            "output_dir": tempfile.mkdtemp(),
        })
        assert result["recommendation_results"] == []
        assert result["escalated_items"] == [section_escalation]
        assert result["assembly_report"]["escalated_count"] == 1


# --- Delivery tests ---

def _mock_store():
    store = MagicMock()
    store.bucket = "cpg-artifacts"
    store.put.side_effect = lambda key, data: f"cpg-artifacts:{key}"
    store.put_raw.side_effect = lambda key, data, ct: f"cpg-artifacts:{key}"
    return store


class TestDeliveryNode:

    def test_no_store_returns_unpublished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001"},
                "dmn_results": [],
                "recommendation_results": [],
                "escalated_items": [],
                "assembly_report": {},
                "artifact_store": None,
                "output_dir": tmpdir,
            }
            with patch("cpg_ingester.nodes.delivery.get_artifact_store", return_value=None):
                result = delivery(state)
            assert result["delivery_status"]["published"] is False
            assert (Path(tmpdir) / "delivery-status.json").exists()

    def test_resolves_configured_store_outside_checkpointed_state(self):
        store = _mock_store()
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001"},
                "dmn_results": [],
                "recommendation_results": [],
                "bpmn_results": [],
                "escalated_items": [],
                "assembly_report": {},
                "output_dir": tmpdir,
            }
            with patch("cpg_ingester.nodes.delivery.get_artifact_store", return_value=store):
                result = delivery(state)

        assert result["delivery_status"]["published"] is True
        assert result["delivery_status"]["artifact_location"] == "cpg-artifacts:published/CPG-001"
        store.put.assert_called_once()

    def test_publishes_all_artifacts(self):
        store = _mock_store()
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001", "title": "Test", "contract_version": "1.0"},
                "dmn_results": [{"dmn_xml": "<definitions/>", "item": {"name": "D1"}}],
                "recommendation_results": [{"id": "r1", "title": "R1"}],
                "escalated_items": [],
                "assembly_report": {"cpg_id": "CPG-001"},
                "artifact_store": store,
                "output_dir": tmpdir,
            }
            result = delivery(state)
            ds = result["delivery_status"]

            assert ds["published"] is True
            assert ds["cpg_id"] == "CPG-001"
            assert ds["artifact_location"] == "cpg-artifacts:published/CPG-001"

            types = [a["type"] for a in ds["artifacts"]]
            assert "metadata" in types
            assert "dmn" in types
            assert "recommendations" in types
            assert "assembly_report" in types

            assert store.put.call_count == 3  # metadata, recommendations, assembly report
            assert store.put_raw.call_count == 1  # DMN XML

    def test_handles_store_error(self):
        store = _mock_store()
        store.put.side_effect = Exception("MinIO unavailable")
        store.put_raw.side_effect = Exception("MinIO unavailable")

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001"},
                "dmn_results": [],
                "recommendation_results": [],
                "escalated_items": [],
                "assembly_report": {},
                "artifact_store": store,
                "output_dir": tmpdir,
            }
            result = delivery(state)
            assert result["delivery_status"]["published"] is False

    def test_publishes_escalated_items(self):
        store = _mock_store()
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001", "contract_version": "1.0"},
                "dmn_results": [],
                "recommendation_results": [],
                "escalated_items": [{"name": "Bad item"}, {"name": "Another"}],
                "assembly_report": {},
                "artifact_store": store,
                "output_dir": tmpdir,
            }
            result = delivery(state)
            assert result["delivery_status"]["escalated_items_count"] == 2
            types = [a["type"] for a in result["delivery_status"]["artifacts"]]
            assert "escalated_items" in types

    def test_writes_delivery_status_file(self):
        store = _mock_store()
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "CPG-001", "contract_version": "1.0"},
                "dmn_results": [],
                "recommendation_results": [{"id": "r1"}],
                "escalated_items": [],
                "assembly_report": {},
                "artifact_store": store,
                "output_dir": tmpdir,
            }
            delivery(state)

            status_file = Path(tmpdir) / "delivery-status.json"
            assert status_file.exists()
            status = json.loads(status_file.read_text())
            assert status["published"] is True

    def test_artifact_location_uses_cpg_id(self):
        store = _mock_store()
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "cpg_metadata": {"cpg_id": "MY-CUSTOM-ID"},
                "dmn_results": [],
                "recommendation_results": [],
                "escalated_items": [],
                "assembly_report": {},
                "artifact_store": store,
                "output_dir": tmpdir,
            }
            result = delivery(state)
            assert "MY-CUSTOM-ID" in result["delivery_status"]["artifact_location"]
