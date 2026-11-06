"""Bundle code inventory — extract all coded entries from a FHIR IPS bundle.

Walks the bundle once and produces a complete inventory of every coded
concept across all resource types. This is the closed set that the
concept-resolution pipeline matches against.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from acp_writer.tools.ips_extractor import (
    _get_effective_date,
    _get_resources_with_entry,
    _resource_ref,
)
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


@dataclass
class InventoryEntry:
    """A single coded entry from the bundle."""
    resource_type: str
    fhir_reference: str
    system: str
    code: str
    display: str
    text: str | None = None
    status: str | None = None
    date: str | None = None

    @property
    def code_token(self) -> str:
        return f"{self.system}|{self.code}"

    def __str__(self) -> str:
        parts = [self.display or self.code]
        if self.system:
            short_sys = self.system.rsplit("/", 1)[-1]
            parts.append(f"[{short_sys} {self.code}]")
        if self.status:
            parts.append(f"({self.status})")
        return " ".join(parts)


@dataclass
class BundleInventory:
    """Complete coded inventory of a FHIR IPS bundle."""
    entries: list[InventoryEntry] = field(default_factory=list)

    def by_resource_type(self, resource_type: str) -> list[InventoryEntry]:
        return [e for e in self.entries if e.resource_type == resource_type]

    def conditions(self) -> list[InventoryEntry]:
        return self.by_resource_type("Condition")

    def medications(self) -> list[InventoryEntry]:
        return [e for e in self.entries if e.resource_type in ("MedicationStatement", "MedicationRequest")]

    def observations(self) -> list[InventoryEntry]:
        return self.by_resource_type("Observation")

    def allergies(self) -> list[InventoryEntry]:
        return self.by_resource_type("AllergyIntolerance")

    def all_code_tokens(self) -> set[str]:
        return {e.code_token for e in self.entries if e.system and e.code}

    def render_for_llm(self, max_entries: int = 100) -> str:
        """Compact string rendering grouped by resource type for LLM consumption."""
        groups: dict[str, list[str]] = {}
        for entry in self.entries[:max_entries]:
            rt = entry.resource_type
            if rt in ("MedicationStatement", "MedicationRequest"):
                rt = "Medications"
            elif rt == "AllergyIntolerance":
                rt = "Allergies"
            groups.setdefault(rt, []).append(str(entry))

        lines = []
        for rt in ["Condition", "Medications", "Observation", "Allergies",
                    "Procedure", "DiagnosticReport", "FamilyMemberHistory"]:
            items = groups.get(rt, [])
            if items:
                seen = set()
                deduped = []
                for item in items:
                    if item not in seen:
                        seen.add(item)
                        deduped.append(item)
                lines.append(f"{rt}: {'; '.join(deduped)}")

        return "\n".join(lines)


def _extract_codings(codeable_concept: dict) -> list[tuple[str, str, str]]:
    """Extract (system, code, display) tuples from a CodeableConcept."""
    results = []
    for coding in codeable_concept.get("coding", []):
        system = coding.get("system", "")
        code = coding.get("code", "")
        display = coding.get("display", "")
        if system or code:
            results.append((system, code, display))
    return results


def _get_status(resource: dict) -> str | None:
    """Extract clinical/resource status."""
    cs = resource.get("clinicalStatus", {})
    for coding in cs.get("coding", []):
        if coding.get("code"):
            return coding["code"]
    return resource.get("status")


@trace_safe(name="build_bundle_inventory")
def build_bundle_inventory(bundle: dict) -> BundleInventory:
    """Walk a FHIR IPS bundle and produce the complete coded inventory."""
    inventory = BundleInventory()

    for rt in ["Condition"]:
        for resource, entry in _get_resources_with_entry(bundle, rt):
            ref = _resource_ref(resource, entry)
            status = _get_status(resource)
            for system, code, display in _extract_codings(resource.get("code", {})):
                inventory.entries.append(InventoryEntry(
                    resource_type=rt, fhir_reference=ref,
                    system=system, code=code, display=display,
                    text=resource.get("code", {}).get("text"),
                    status=status,
                ))

    for rt in ["MedicationStatement", "MedicationRequest"]:
        for resource, entry in _get_resources_with_entry(bundle, rt):
            ref = _resource_ref(resource, entry)
            status = resource.get("status")
            med_cc = resource.get("medicationCodeableConcept", {})
            codings = _extract_codings(med_cc)
            if codings:
                for system, code, display in codings:
                    inventory.entries.append(InventoryEntry(
                        resource_type=rt, fhir_reference=ref,
                        system=system, code=code, display=display,
                        text=med_cc.get("text"),
                        status=status,
                    ))
            elif med_cc.get("text"):
                inventory.entries.append(InventoryEntry(
                    resource_type=rt, fhir_reference=ref,
                    system="", code="",
                    display=med_cc["text"],
                    text=med_cc["text"],
                    status=status,
                ))

    for rt in ["Observation"]:
        for resource, entry in _get_resources_with_entry(bundle, rt):
            ref = _resource_ref(resource, entry)
            date_str = _get_effective_date(resource)
            for system, code, display in _extract_codings(resource.get("code", {})):
                inventory.entries.append(InventoryEntry(
                    resource_type=rt, fhir_reference=ref,
                    system=system, code=code, display=display,
                    date=date_str,
                ))
            for component in resource.get("component", []):
                for system, code, display in _extract_codings(component.get("code", {})):
                    inventory.entries.append(InventoryEntry(
                        resource_type=rt, fhir_reference=ref,
                        system=system, code=code, display=display,
                        date=date_str,
                    ))

    for rt in ["AllergyIntolerance"]:
        for resource, entry in _get_resources_with_entry(bundle, rt):
            ref = _resource_ref(resource, entry)
            status = _get_status(resource)
            for system, code, display in _extract_codings(resource.get("code", {})):
                inventory.entries.append(InventoryEntry(
                    resource_type=rt, fhir_reference=ref,
                    system=system, code=code, display=display,
                    status=status,
                ))

    for rt in ["Procedure"]:
        for resource, entry in _get_resources_with_entry(bundle, rt):
            ref = _resource_ref(resource, entry)
            status = resource.get("status")
            date_str = resource.get("performedDateTime")
            for system, code, display in _extract_codings(resource.get("code", {})):
                inventory.entries.append(InventoryEntry(
                    resource_type=rt, fhir_reference=ref,
                    system=system, code=code, display=display,
                    status=status, date=date_str,
                ))

    for rt in ["DiagnosticReport"]:
        for resource, entry in _get_resources_with_entry(bundle, rt):
            ref = _resource_ref(resource, entry)
            date_str = resource.get("effectiveDateTime")
            for system, code, display in _extract_codings(resource.get("code", {})):
                inventory.entries.append(InventoryEntry(
                    resource_type=rt, fhir_reference=ref,
                    system=system, code=code, display=display,
                    date=date_str,
                ))

    for rt in ["FamilyMemberHistory"]:
        for resource, entry in _get_resources_with_entry(bundle, rt):
            ref = _resource_ref(resource, entry)
            for condition in resource.get("condition", []):
                for system, code, display in _extract_codings(condition.get("code", {})):
                    inventory.entries.append(InventoryEntry(
                        resource_type=rt, fhir_reference=ref,
                        system=system, code=code, display=display,
                    ))

    logger.debug('Bundle inventory (details redacted)')

    return inventory
