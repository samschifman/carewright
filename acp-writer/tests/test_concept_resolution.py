"""Tests for the concept-resolution pipeline."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from acp_writer.tools.bundle_inventory import build_bundle_inventory
from acp_writer.tools.concept_resolution import resolve_concept_in_bundle
from acp_writer.tools.terminology_lookup import LookupResult

PROJECT_ROOT = Path(__file__).parent.parent.parent
BUNDLES_DIR = PROJECT_ROOT / "acp-writer" / "benchmarks" / "bundles"


def _load(name: str) -> dict:
    return json.loads((BUNDLES_DIR / name).read_text())


class TestCacheHit:
    def test_known_condition_resolves_via_cache(self):
        bundle = _load("complex-patient-01.json")
        inventory = build_bundle_inventory(bundle)
        result = resolve_concept_in_bundle("hypertension", inventory, "condition")
        assert result.resolved
        assert result.match_basis == "cache"
        assert "cache" in result.steps_run
        assert any("59621000" in e.code for e in result.entries)


class TestTerminologyResolution:
    @patch("acp_writer.tools.terminology_lookup.find_candidates")
    def test_icd10_condition_found_via_terminology(self, mock_find):
        mock_find.return_value = [
            LookupResult(found=True, system="http://hl7.org/fhir/sid/icd-10-cm",
                        code="E03.9", display="Hypothyroidism, unspecified"),
        ]
        bundle = _load("messy-data-01.json")
        inventory = build_bundle_inventory(bundle)
        result = resolve_concept_in_bundle("hypothyroidism", inventory, "condition")
        assert result.resolved
        assert result.match_basis == "terminology"
        assert any("E03" in e.code for e in result.entries)

    @patch("acp_writer.tools.terminology_lookup.find_candidates")
    def test_blood_pressure_panel_uses_terminology_fallback(self, mock_find):
        mock_find.return_value = [
            LookupResult(
                found=True,
                system="http://loinc.org",
                code="85354-9",
                display="Blood pressure panel",
            ),
        ]
        bundle = {
            "entry": [{
                "resource": {
                    "resourceType": "Observation",
                    "id": "bp-panel",
                    "status": "final",
                    "code": {"coding": [{
                        "system": "http://loinc.org",
                        "code": "85354-9",
                        "display": "Blood pressure panel",
                    }]},
                },
            }],
        }
        inventory = build_bundle_inventory(bundle)

        result = resolve_concept_in_bundle("blood pressure", inventory, "observation")

        assert result.resolved
        assert result.match_basis == "terminology"
        mock_find.assert_called_once_with("http://loinc.org", "blood pressure", n=5)


class TestDisplayTextMatch:
    def test_free_text_medication_found_via_display(self):
        bundle = _load("messy-data-01.json")
        inventory = build_bundle_inventory(bundle)
        result = resolve_concept_in_bundle(
            "levothyroxine", inventory, "medication", llm_client=None,
        )
        assert result.resolved
        assert result.match_basis == "display_text"
        assert any("levothyroxine" in (e.display or e.text or "").lower() for e in result.entries)

    def test_wrong_loinc_observation_found_via_display(self):
        bundle = _load("messy-data-01.json")
        inventory = build_bundle_inventory(bundle)
        result = resolve_concept_in_bundle(
            "thyroid stimulating hormone", inventory, "observation", llm_client=None,
        )
        assert result.resolved
        assert result.match_basis == "display_text"


class TestLLMInventoryMatch:
    def test_llm_fallback_resolves_unknown_term(self):
        bundle = _load("messy-data-01.json")
        inventory = build_bundle_inventory(bundle)

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        cond_entries = inventory.conditions()
        icd10_ref = next(
            (e.fhir_reference for e in cond_entries if "E03" in e.code), None
        )
        assert icd10_ref is not None

        from pydantic import BaseModel, Field
        class FakeMatch(BaseModel):
            matched_references: list[str] = [icd10_ref]
            reasoning: str = "Hypothyroidism matches thyroid disorder"

        mock_structured.invoke.return_value = FakeMatch()

        with patch("acp_writer.tools.terminology_lookup.find_candidates", return_value=[]):
            result = resolve_concept_in_bundle(
                "thyroid disorder", inventory, "condition", llm_client=mock_llm,
            )

        assert result.resolved
        assert result.match_basis == "llm_inventory"
        assert "llm_inventory" in result.steps_run


class TestDeterministicOnlyMode:
    def test_returns_unresolved_without_llm(self):
        bundle = _load("messy-data-01.json")
        inventory = build_bundle_inventory(bundle)

        with patch("acp_writer.tools.terminology_lookup.find_candidates", return_value=[]):
            result = resolve_concept_in_bundle(
                "completely unknown concept xyz123", inventory, "condition", llm_client=None,
            )

        assert not result.resolved
        assert result.unresolved
        assert not result.definitive_miss

    def test_definitive_miss_with_llm(self):
        bundle = _load("messy-data-01.json")
        inventory = build_bundle_inventory(bundle)

        mock_llm = MagicMock()
        mock_structured = MagicMock()
        mock_llm.with_structured_output.return_value = mock_structured

        from pydantic import BaseModel
        class EmptyMatch(BaseModel):
            matched_references: list[str] = []
            reasoning: str = "No match found"

        mock_structured.invoke.return_value = EmptyMatch()

        with patch("acp_writer.tools.terminology_lookup.find_candidates", return_value=[]):
            result = resolve_concept_in_bundle(
                "completely unknown concept xyz123", inventory, "condition", llm_client=mock_llm,
            )

        assert not result.resolved
        assert result.definitive_miss
