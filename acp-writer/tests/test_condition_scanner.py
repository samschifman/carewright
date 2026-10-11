"""Tests for the Condition Scanner node."""

import json
from pathlib import Path

from acp_writer.nodes.condition_scanner import condition_scanner

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "mock-EHR" / "data"


def _load_bundle(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text())


class TestConditionScanner:
    def test_medication_patient(self):
        bundle = _load_bundle("patient-bundle-medication.json")
        result = condition_scanner({"ips_bundle": bundle})

        assert result["patient_reference"] not in ("Patient/", "Patient/unknown")
        assert result["patient_demographics"]["gender"] == "male"
        assert result["patient_demographics"]["birth_date"] == "1971-03-15"
        assert result["patient_demographics"]["name"] == "James Reynolds"

        codes = result["condition_codes"]
        systems = {c["system"] for c in codes}
        assert "http://snomed.info/sct" in systems
        assert "http://hl7.org/fhir/sid/icd-10-cm" in systems

        snomed_codes = {c["code"] for c in codes if c["system"] == "http://snomed.info/sct"}
        assert "59621000" in snomed_codes
        assert "44054006" in snomed_codes

        med_codes = result["medication_codes"]
        assert len(med_codes) >= 1
        rxnorm_codes = {c["code"] for c in med_codes if "rxnorm" in c["system"]}
        assert "860975" in rxnorm_codes

        assert result["allergy_codes"] == []

    def test_lifestyle_patient(self):
        bundle = _load_bundle("patient-bundle-lifestyle.json")
        result = condition_scanner({"ips_bundle": bundle})

        assert result["patient_reference"] not in ("Patient/", "Patient/unknown")
        assert result["patient_demographics"]["gender"] == "female"
        assert result["patient_demographics"]["name"] == "Maria Chen"

        snomed_codes = {c["code"] for c in result["condition_codes"]
                        if c["system"] == "http://snomed.info/sct"}
        assert "59621000" in snomed_codes
        assert "44054006" not in snomed_codes

        assert result["medication_codes"] == []
        assert result["allergy_codes"] == []

    def test_empty_bundle(self):
        result = condition_scanner({"ips_bundle": {"resourceType": "Bundle", "entry": []}})
        assert result == {}

    def test_no_bundle(self):
        result = condition_scanner({})
        assert result == {}

    def test_inactive_condition_excluded(self):
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "test-1"}},
                {
                    "resource": {
                        "resourceType": "Condition",
                        "clinicalStatus": {
                            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                        "code": "resolved"}],
                        },
                        "code": {
                            "coding": [{"system": "http://snomed.info/sct", "code": "12345",
                                        "display": "Resolved condition"}],
                        },
                    },
                },
                {
                    "resource": {
                        "resourceType": "Condition",
                        "clinicalStatus": {
                            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                        "code": "active"}],
                        },
                        "code": {
                            "coding": [{"system": "http://snomed.info/sct", "code": "67890",
                                        "display": "Active condition"}],
                        },
                    },
                },
            ],
        }
        result = condition_scanner({"ips_bundle": bundle})
        codes = {c["code"] for c in result["condition_codes"]}
        assert "67890" in codes
        assert "12345" not in codes

    def test_medication_request_extracted(self):
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "test-1"}},
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "status": "active",
                        "medicationCodeableConcept": {
                            "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                        "code": "29046", "display": "Lisinopril"}],
                        },
                    },
                },
            ],
        }
        result = condition_scanner({"ips_bundle": bundle})
        med_codes = {c["code"] for c in result["medication_codes"]}
        assert "29046" in med_codes

    def test_cancelled_medication_excluded(self):
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "test-1"}},
                {
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "status": "cancelled",
                        "medicationCodeableConcept": {
                            "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                        "code": "99999", "display": "Cancelled med"}],
                        },
                    },
                },
            ],
        }
        result = condition_scanner({"ips_bundle": bundle})
        assert result["medication_codes"] == []

    def test_allergy_extracted(self):
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "test-1"}},
                {
                    "resource": {
                        "resourceType": "AllergyIntolerance",
                        "clinicalStatus": {
                            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                        "code": "active"}],
                        },
                        "code": {
                            "coding": [{"system": "http://snomed.info/sct", "code": "91936005",
                                        "display": "Penicillin allergy"}],
                        },
                    },
                },
            ],
        }
        result = condition_scanner({"ips_bundle": bundle})
        allergy_codes = {c["code"] for c in result["allergy_codes"]}
        assert "91936005" in allergy_codes

    def test_display_included(self):
        bundle = _load_bundle("patient-bundle-medication.json")
        result = condition_scanner({"ips_bundle": bundle})
        htn_codes = [c for c in result["condition_codes"]
                     if c.get("code") == "59621000"]
        assert htn_codes[0]["display"] == "Essential hypertension"


class TestPipelineIntegration:
    def test_condition_scanner_in_pipeline(self):
        from unittest.mock import MagicMock, patch
        from .pipeline_helpers import build_pipeline

        with patch("acp_writer.nodes.plan_composer.get_llm") as mock_compose, \
             patch("acp_writer.nodes.brief_reviewer.get_llm") as mock_brief, \
             patch("acp_writer.nodes.fhir_semantic_reviewer.get_llm") as mock_fhir:
            for mock_llm in [mock_compose, mock_brief, mock_fhir]:
                resp = MagicMock()
                resp.content = '{"patient_reference":"Patient/patient-1","applicable_cpgs":[],"goals":[],"activities":[],"conflicts":[],"review_status":"pending"}'
                m = MagicMock()
                m.invoke.return_value = resp
                mock_llm.return_value = m

            graph = build_pipeline()
            compiled = graph.compile()
            bundle = _load_bundle("patient-bundle-medication.json")
            result = compiled.invoke({"ips_bundle": bundle})

            assert result["patient_reference"] not in ("Patient/", "Patient/unknown")
            assert len(result["condition_codes"]) >= 2
            assert "delivery_status" in result
