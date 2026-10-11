"""Tests for the DMN Executor node."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import acp_writer.api as api_module
from acp_writer.api import _dynamic_models, init_stores
from acp_writer.nodes.dmn_executor import (
    _extract_input_value,
    dmn_executor,
)
from acp_writer.tools.bundle_inventory import build_bundle_inventory
from acp_writer.tools.dmn_evaluation import DmnEngineError
from acp_writer.store.embedding import FakeEmbeddingProvider

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "mock-EHR" / "data"
DMN_DIR = Path(__file__).parent / "fixtures" / "dmn"
FIXTURES = PROJECT_ROOT / "shared" / "tests" / "fixtures"

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"


def _load_bundle(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text())


def _deploy_dmn(name: str):
    from acp_writer.api import _parse_dmn_metadata
    dmn_xml = (DMN_DIR / name).read_text()
    summary = _parse_dmn_metadata(dmn_xml)
    _dynamic_models[summary.id] = {"summary": summary, "dmn_xml": dmn_xml}
    return summary


@pytest.fixture(autouse=True)
def reset_stores():
    _dynamic_models.clear()
    init_stores(FakeEmbeddingProvider(dimensions=8))
    yield
    _dynamic_models.clear()


class TestExtractInputValue:
    def test_systolic_bp_from_ips(self):
        bundle = _load_bundle("patient-bundle-medication.json")
        inventory = build_bundle_inventory(bundle)
        value, ref, _ = _extract_input_value(bundle, "Systolic BP", "number", {}, inventory=inventory)
        assert value == 142
        assert ref is not None

    def test_has_diabetes_from_ips(self):
        bundle = _load_bundle("patient-bundle-medication.json")
        inventory = build_bundle_inventory(bundle)
        value, ref, _ = _extract_input_value(bundle, "Has Diabetes", "boolean", {}, inventory=inventory)
        assert value is True

    def test_has_diabetes_absent_degraded(self):
        """Without LLM, absent concept is unresolved (missing input), not False."""
        bundle = _load_bundle("patient-bundle-lifestyle.json")
        inventory = build_bundle_inventory(bundle)
        value, ref, audit = _extract_input_value(bundle, "Has Diabetes", "boolean", {}, inventory=inventory)
        assert value is None
        assert audit.get("degraded") is True

    def test_from_prior_results(self):
        prior = {
            "treatment-recommendation": {
                "Treatment Recommendation": {
                    "Action": "Start medication",
                    "Treatment Action": "Start medication",
                }
            }
        }
        value, ref, audit = _extract_input_value({}, "Treatment Action", "string", prior)
        assert value == "Start medication"
        assert ref is None
        assert audit.get("match_basis") == "prior_dmn"

    def test_unknown_variable(self):
        bundle = {"entry": []}
        inventory = build_bundle_inventory(bundle)
        value, ref, _ = _extract_input_value(bundle, "Unknown Variable", "string", {}, inventory=inventory)
        assert value is None

    def test_extract_via_codes_observation(self):
        bundle = _load_bundle("patient-bundle-medication.json")
        codes = [f"{LOINC}|8480-6"]
        value, ref, audit = _extract_input_value(
            bundle, "SBP Reading", "number", {}, codes=codes,
        )
        assert value == 142
        assert audit.get("match_basis") == "decision_variable_codes"

    def test_extract_via_codes_condition(self):
        bundle = _load_bundle("patient-bundle-medication.json")
        codes = [f"{SNOMED}|44054006"]
        value, ref, audit = _extract_input_value(
            bundle, "Diabetes Present", "boolean", {}, codes=codes,
        )
        assert value is True

    def test_codes_take_priority_over_pipeline(self):
        """When codes are provided, they are used even if the name resolves differently."""
        bundle = _load_bundle("patient-bundle-medication.json")
        codes = [f"{LOINC}|8462-4"]
        value, ref, audit = _extract_input_value(
            bundle, "Systolic BP", "number", {}, codes=codes,
        )
        assert value == 92
        assert audit.get("match_basis") == "decision_variable_codes"

    def test_codes_none_falls_back_to_pipeline(self):
        bundle = _load_bundle("patient-bundle-medication.json")
        inventory = build_bundle_inventory(bundle)
        value, ref, _ = _extract_input_value(
            bundle, "Systolic BP", "number", {}, codes=None, inventory=inventory,
        )
        assert value == 142

    def test_concept_resolver_hba1c(self):
        """Concept resolver resolves 'HbA1c Value' without codes."""
        bundle = _load_bundle("patient-bundle-medication.json")
        inventory = build_bundle_inventory(bundle)
        value, ref, _ = _extract_input_value(bundle, "HbA1c Value", "number", {}, inventory=inventory)
        assert value is None

    def test_concept_resolver_condition(self):
        """Concept resolver resolves 'hypertension' to a condition check."""
        bundle = _load_bundle("patient-bundle-medication.json")
        inventory = build_bundle_inventory(bundle)
        value, ref, _ = _extract_input_value(bundle, "Has Hypertension", "boolean", {}, inventory=inventory)
        assert value is True

    def test_concept_resolver_drug_class(self):
        """Concept resolver handles drug class queries when coded with RxNorm."""
        RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "MedicationRequest",
                    "id": "mr1",
                    "status": "active",
                    "medicationCodeableConcept": {
                        "coding": [{"system": RXNORM, "code": "314076", "display": "Lisinopril"}],
                    },
                },
            }],
        }
        inventory = build_bundle_inventory(bundle)
        value, ref, audit = _extract_input_value(bundle, "Current ACE Inhibitor", "boolean", {}, inventory=inventory)
        assert value is True
        assert audit.get("match_basis") == "cache"


class TestDMNExecutor:
    def test_no_models(self):
        result = dmn_executor({"applicable_dmn_models": [], "ips_bundle": {}})
        assert result["dmn_results"] == []

    def test_model_not_deployed(self):
        state = {
            "ips_bundle": _load_bundle("patient-bundle-medication.json"),
            "applicable_dmn_models": [
                {"id": "nonexistent", "name": "Nonexistent Model", "inputs": []},
            ],
            "dmn_dependency_graph": [["nonexistent"]],
        }
        result = dmn_executor(state)
        assert len(result["dmn_results"]) == 1
        assert result["dmn_results"][0]["error"] == "Model not deployed"

    @patch("acp_writer.nodes.dmn_executor.get_evaluation_client")
    def test_engine_messages_are_recorded_in_audit(self, mock_client_factory):
        summary = _deploy_dmn("treatment-recommendation.dmn")
        mock_client = mock_client_factory.return_value
        mock_client.evaluate.side_effect = DmnEngineError(
            422,
            [{"severity": "ERROR", "text": "DMN compilation failed"}],
            "DMN compilation errors",
        )
        state = {
            "ips_bundle": _load_bundle("patient-bundle-medication.json"),
            "applicable_dmn_models": [summary.model_dump(mode="json")],
            "dmn_dependency_graph": [[summary.id]],
        }

        result = dmn_executor(state)

        audit = result["dmn_results"][0]
        assert audit["error_status"] == 422
        assert audit["error_messages"] == [
            {"severity": "ERROR", "text": "DMN compilation failed"},
        ]

    @patch("acp_writer.api._evaluate_jit")
    def test_successful_evaluation(self, mock_jit):
        mock_jit.return_value = {
            "Treatment Recommendation": {
                "Action": "Start medication",
                "Medication": "Lisinopril",
                "Dose": "10 mg daily",
                "Follow Up Weeks": 4,
            }
        }

        summary = _deploy_dmn("treatment-recommendation.dmn")
        state = {
            "ips_bundle": _load_bundle("patient-bundle-medication.json"),
            "applicable_dmn_models": [summary.model_dump(mode="json")],
            "dmn_dependency_graph": [[summary.id]],
        }

        result = dmn_executor(state)
        assert len(result["dmn_results"]) == 1

        audit = result["dmn_results"][0]
        assert audit["model_id"] == "treatment-recommendation"
        assert "Systolic BP" in audit["inputs"]
        assert audit["inputs"]["Systolic BP"] == 142
        assert audit["outputs"]["Treatment Recommendation"]["Action"] == "Start medication"
        assert len(audit["fhir_references"]) > 0
        assert "timestamp" in audit
        assert "error" not in audit
        assert "input_resolution" in audit

    @patch("acp_writer.api._evaluate_jit")
    def test_chained_evaluation(self, mock_jit):
        """Treatment → Monitoring dependency chain."""
        mock_jit.side_effect = [
            {
                "Treatment Recommendation": {
                    "Action": "Start medication",
                    "Medication": "Lisinopril",
                    "Dose": "10 mg daily",
                    "Follow Up Weeks": 4,
                }
            },
            {
                "Monitoring Plan": {
                    "Lab Order": "Basic Metabolic Panel",
                    "Lab Timing Weeks": 4,
                }
            },
        ]

        treat = _deploy_dmn("treatment-recommendation.dmn")
        monitor = _deploy_dmn("monitoring-plan.dmn")

        state = {
            "ips_bundle": _load_bundle("patient-bundle-medication.json"),
            "applicable_dmn_models": [
                treat.model_dump(mode="json"),
                monitor.model_dump(mode="json"),
            ],
            "dmn_dependency_graph": [
                ["treatment-recommendation"],
                ["monitoring-plan"],
            ],
        }

        result = dmn_executor(state)
        assert len(result["dmn_results"]) == 2

        treat_audit = result["dmn_results"][0]
        monitor_audit = result["dmn_results"][1]
        assert treat_audit["model_id"] == "treatment-recommendation"
        assert monitor_audit["model_id"] == "monitoring-plan"

        assert mock_jit.call_count == 2
        monitor_call_inputs = mock_jit.call_args_list[1][0][1]
        assert "Treatment Action" in monitor_call_inputs

    @patch("acp_writer.api._evaluate_jit")
    def test_evaluation_failure_recorded(self, mock_jit):
        mock_jit.side_effect = Exception("Connection refused")

        summary = _deploy_dmn("treatment-recommendation.dmn")
        state = {
            "ips_bundle": _load_bundle("patient-bundle-medication.json"),
            "applicable_dmn_models": [summary.model_dump(mode="json")],
            "dmn_dependency_graph": [[summary.id]],
        }

        result = dmn_executor(state)
        assert len(result["dmn_results"]) == 1
        assert "error" in result["dmn_results"][0]
        assert "Connection refused" in result["dmn_results"][0]["error"]
