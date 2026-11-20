"""IPS Extractor — targeted data extraction from IPS Bundle.

Used by DMN Executor for on-demand extraction of specific
observations, conditions, medications, allergies, procedures,
family history, and diagnostic reports by code.
Returns FHIR resource references for audit trail.
"""

import logging
from datetime import date, datetime
from typing import Any

from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)


class ExtractionResult:
    """Result of a targeted IPS extraction."""

    def __init__(
        self,
        found: bool,
        value: Any = None,
        unit: str | None = None,
        date: str | None = None,
        fhir_reference: str | None = None,
        resource_type: str | None = None,
        match_basis: str | None = None,
    ):
        self.found = found
        self.value = value
        self.unit = unit
        self.date = date
        self.fhir_reference = fhir_reference
        self.resource_type = resource_type
        self.match_basis = match_basis

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"found": self.found}
        if self.value is not None:
            d["value"] = self.value
        if self.unit:
            d["unit"] = self.unit
        if self.date:
            d["date"] = self.date
        if self.fhir_reference:
            d["fhir_reference"] = self.fhir_reference
        if self.resource_type:
            d["resource_type"] = self.resource_type
        return d


def _get_resources(bundle: dict, resource_type: str) -> list[dict]:
    return [
        e["resource"]
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == resource_type
    ]


def _get_resources_with_entry(bundle: dict, resource_type: str) -> list[tuple[dict, dict]]:
    """Return (resource, entry) pairs for provenance-aware extraction."""
    return [
        (e["resource"], e)
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == resource_type
    ]


def _resource_ref(resource: dict, entry: dict | None = None) -> str:
    """Compute a resource reference for provenance.

    Prefers resource.id, falls back to entry.fullUrl, then 'unknown'.
    """
    rt = resource.get("resourceType", "Resource")
    rid = resource.get("id")
    if rid:
        return f"{rt}/{rid}"
    if entry:
        full_url = entry.get("fullUrl")
        if full_url:
            return full_url
    return f"{rt}/unknown"


def _has_code(resource: dict, code_field: str, system: str, code: str) -> bool:
    cc = resource.get(code_field, {})
    for coding in cc.get("coding", []):
        if coding.get("system") == system and coding.get("code") == code:
            return True
    return False


def _has_any_code(resource: dict, code_field: str, code_tokens: list[str]) -> tuple[bool, str | None]:
    """Match if any coding on the resource matches any candidate token.

    Supports ICD-10 prefix matching (E03 matches E03.9).
    Returns (matched, match_basis) where match_basis is 'code' or None.
    """
    cc = resource.get(code_field, {})
    for coding in cc.get("coding", []):
        c_system = coding.get("system", "")
        c_code = coding.get("code", "")
        for token in code_tokens:
            if "|" not in token:
                continue
            t_system, t_code = token.rsplit("|", 1)
            if c_system == t_system and c_code == t_code:
                return True, "code"
            if c_system == t_system and (c_code.startswith(t_code + ".") or t_code.startswith(c_code + ".")):
                return True, "code"
    return False, None


def _normalize_display(text: str) -> str:
    """Normalize display text for comparison."""
    import re as _re
    return _re.sub(r"[,.:;()\[\]]+", " ", text.lower()).strip()


def _has_display_match(resource: dict, code_field: str, terms: list[str]) -> tuple[bool, str | None]:
    """Match resource by display text or text field against normalized terms.

    Returns (matched, matched_display) where matched_display is the display string that matched.
    """
    cc = resource.get(code_field, {})
    texts = []
    for coding in cc.get("coding", []):
        if coding.get("display"):
            texts.append(coding["display"])
    if cc.get("text"):
        texts.append(cc["text"])

    for text in texts:
        norm = _normalize_display(text)
        for term in terms:
            norm_term = _normalize_display(term)
            if norm_term in norm or norm in norm_term:
                return True, text
    return False, None


def _match_resource(
    resource: dict,
    code_field: str,
    code_tokens: list[str] | None = None,
    display_terms: list[str] | None = None,
    system: str | None = None,
    code: str | None = None,
) -> tuple[bool, str]:
    """Try to match a resource using codes first, then display text.

    Returns (matched, match_basis) where match_basis is 'code', 'display_text', or ''.
    """
    if system and code:
        if _has_code(resource, code_field, system, code):
            return True, "code"

    if code_tokens:
        matched, basis = _has_any_code(resource, code_field, code_tokens)
        if matched:
            return True, "code"

    if display_terms:
        matched, display = _has_display_match(resource, code_field, display_terms)
        if matched:
            return True, "display_text"

    return False, ""


def _get_effective_date(resource: dict) -> str | None:
    for field in ["effectiveDateTime", "effectivePeriod", "issued"]:
        val = resource.get(field)
        if val:
            if isinstance(val, dict):
                return val.get("start") or val.get("end")
            return val
    return None


def _parse_date_key(date_str: str | None) -> str:
    """Return a sortable string for date comparison."""
    if not date_str:
        return ""
    return date_str


def _extract_obs_value(obs_or_component: dict) -> tuple[Any, str | None]:
    """Extract value from an Observation or component, trying all FHIR value types."""
    vq = obs_or_component.get("valueQuantity")
    if vq and vq.get("value") is not None:
        return vq["value"], vq.get("unit")

    vcc = obs_or_component.get("valueCodeableConcept")
    if vcc:
        codings = vcc.get("coding", [])
        if codings:
            return codings[0].get("code"), None
        text = vcc.get("text")
        if text:
            return text, None

    vs = obs_or_component.get("valueString")
    if vs is not None:
        return vs, None

    vb = obs_or_component.get("valueBoolean")
    if vb is not None:
        return vb, None

    vr = obs_or_component.get("valueRange")
    if vr:
        low = vr.get("low", {}).get("value")
        high = vr.get("high", {}).get("value")
        if low is not None and high is not None:
            return {"low": low, "high": high}, vr.get("low", {}).get("unit")
        return low or high, None

    return None, None


@trace_safe(name="ips_extract_observation")
def extract_observation(
    ips_bundle: dict, system: str, code: str
) -> ExtractionResult:
    """Extract the most recent observation matching a LOINC/SNOMED code.

    For panel observations (like BP), checks both top-level code and components.
    """
    obs_entries = _get_resources_with_entry(ips_bundle, "Observation")

    candidates: list[tuple[str, dict, dict, Any, str | None]] = []

    for obs, entry in obs_entries:
        effective = _get_effective_date(obs)

        if _has_code(obs, "code", system, code):
            value, unit = _extract_obs_value(obs)
            if value is not None:
                candidates.append((_parse_date_key(effective), obs, entry, value, unit))
            continue

        for component in obs.get("component", []):
            if _has_code(component, "code", system, code):
                value, unit = _extract_obs_value(component)
                if value is not None:
                    candidates.append((_parse_date_key(effective), obs, entry, value, unit))

    if not candidates:
        return ExtractionResult(found=False)

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, obs, entry, value, unit = candidates[0]

    return ExtractionResult(
        found=True,
        value=value,
        unit=unit,
        date=_get_effective_date(obs),
        fhir_reference=_resource_ref(obs, entry),
        resource_type="Observation",
    )


@trace_safe(name="ips_extract_condition")
def extract_condition(
    ips_bundle: dict, system: str, code: str
) -> ExtractionResult:
    """Check if an active condition matching the code is present."""
    cond_entries = _get_resources_with_entry(ips_bundle, "Condition")

    for condition, entry in cond_entries:
        if not _has_code(condition, "code", system, code):
            continue

        clinical_status = condition.get("clinicalStatus", {})
        is_active = True
        for coding in clinical_status.get("coding", []):
            if coding.get("code") in ("resolved", "inactive", "remission"):
                is_active = False
                break

        if is_active:
            return ExtractionResult(
                found=True,
                value=True,
                fhir_reference=_resource_ref(condition, entry),
                resource_type="Condition",
            )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_medication")
def extract_medication(
    ips_bundle: dict, system: str, code: str
) -> ExtractionResult:
    """Check if an active medication matching the code is present."""
    for resource_type in ["MedicationStatement", "MedicationRequest"]:
        med_entries = _get_resources_with_entry(ips_bundle, resource_type)
        for resource, entry in med_entries:
            status = resource.get("status", "")
            if status in ("cancelled", "entered-in-error", "stopped"):
                continue

            if _has_code(resource, "medicationCodeableConcept", system, code):
                return ExtractionResult(
                    found=True,
                    value=True,
                    fhir_reference=_resource_ref(resource, entry),
                    resource_type=resource_type,
                )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_allergy")
def extract_allergy(
    ips_bundle: dict, system: str, code: str
) -> ExtractionResult:
    """Check if an active allergy matching the code is present."""
    allergy_entries = _get_resources_with_entry(ips_bundle, "AllergyIntolerance")

    for allergy, entry in allergy_entries:
        if not _has_code(allergy, "code", system, code):
            continue

        clinical_status = allergy.get("clinicalStatus", {})
        is_active = True
        for coding in clinical_status.get("coding", []):
            if coding.get("code") in ("resolved", "inactive"):
                is_active = False
                break

        if is_active:
            return ExtractionResult(
                found=True,
                value=True,
                fhir_reference=_resource_ref(allergy, entry),
                resource_type="AllergyIntolerance",
            )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_procedure")
def extract_procedure(
    ips_bundle: dict, system: str, code: str
) -> ExtractionResult:
    """Check if a procedure matching the code is present."""
    proc_entries = _get_resources_with_entry(ips_bundle, "Procedure")

    for proc, entry in proc_entries:
        if not _has_code(proc, "code", system, code):
            continue

        status = proc.get("status", "")
        if status in ("entered-in-error", "not-done"):
            continue

        return ExtractionResult(
            found=True,
            value=True,
            date=proc.get("performedDateTime") or (
                proc.get("performedPeriod", {}).get("start")
            ),
            fhir_reference=_resource_ref(proc, entry),
            resource_type="Procedure",
        )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_family_history")
def extract_family_history(
    ips_bundle: dict, system: str, code: str
) -> ExtractionResult:
    """Check if a family history entry matching the condition code is present."""
    fmh_entries = _get_resources_with_entry(ips_bundle, "FamilyMemberHistory")

    for fmh, entry in fmh_entries:
        status = fmh.get("status", "")
        if status in ("entered-in-error",):
            continue

        for condition in fmh.get("condition", []):
            if _has_code(condition, "code", system, code):
                return ExtractionResult(
                    found=True,
                    value=True,
                    fhir_reference=_resource_ref(fmh, entry),
                    resource_type="FamilyMemberHistory",
                )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_diagnostic_report")
def extract_diagnostic_report(
    ips_bundle: dict, system: str, code: str
) -> ExtractionResult:
    """Check if a diagnostic report matching the code is present."""
    report_entries = _get_resources_with_entry(ips_bundle, "DiagnosticReport")

    for report, entry in report_entries:
        if not _has_code(report, "code", system, code):
            continue

        status = report.get("status", "")
        if status in ("entered-in-error", "cancelled"):
            continue

        return ExtractionResult(
            found=True,
            value=True,
            date=report.get("effectiveDateTime") or (
                report.get("effectivePeriod", {}).get("start")
            ),
            fhir_reference=_resource_ref(report, entry),
            resource_type="DiagnosticReport",
        )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_condition_concept")
def extract_condition_concept(
    ips_bundle: dict,
    code_tokens: list[str] | None = None,
    display_terms: list[str] | None = None,
) -> ExtractionResult:
    """Check if an active condition matches any code or display term.

    Coding-robust: tries exact code match across multiple systems,
    then falls back to display-text matching.
    """
    cond_entries = _get_resources_with_entry(ips_bundle, "Condition")

    for condition, entry in cond_entries:
        clinical_status = condition.get("clinicalStatus", {})
        is_active = True
        for coding in clinical_status.get("coding", []):
            if coding.get("code") in ("resolved", "inactive", "remission"):
                is_active = False
                break
        if not is_active:
            continue

        matched, basis = _match_resource(
            condition, "code",
            code_tokens=code_tokens,
            display_terms=display_terms,
        )
        if matched:
            return ExtractionResult(
                found=True,
                value=True,
                fhir_reference=_resource_ref(condition, entry),
                resource_type="Condition",
                match_basis=basis,
            )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_medication_concept")
def extract_medication_concept(
    ips_bundle: dict,
    code_tokens: list[str] | None = None,
    display_terms: list[str] | None = None,
) -> ExtractionResult:
    """Check if an active medication matches any code or display term.

    Checks both MedicationStatement and MedicationRequest. Also matches
    medicationCodeableConcept.text for free-text medications.
    """
    for resource_type in ["MedicationStatement", "MedicationRequest"]:
        med_entries = _get_resources_with_entry(ips_bundle, resource_type)
        for resource, entry in med_entries:
            status = resource.get("status", "")
            if status in ("cancelled", "entered-in-error", "stopped"):
                continue

            matched, basis = _match_resource(
                resource, "medicationCodeableConcept",
                code_tokens=code_tokens,
                display_terms=display_terms,
            )
            if matched:
                return ExtractionResult(
                    found=True,
                    value=True,
                    fhir_reference=_resource_ref(resource, entry),
                    resource_type=resource_type,
                    match_basis=basis,
                )

    return ExtractionResult(found=False, value=False)


@trace_safe(name="ips_extract_observation_concept")
def extract_observation_concept(
    ips_bundle: dict,
    code_tokens: list[str] | None = None,
    display_terms: list[str] | None = None,
) -> ExtractionResult:
    """Extract the most recent observation matching any code or display term.

    Coding-robust: tries code match first, then display-text fallback.
    """
    obs_entries = _get_resources_with_entry(ips_bundle, "Observation")
    candidates: list[tuple[str, dict, dict, Any, str | None, str]] = []

    for obs, entry in obs_entries:
        effective = _get_effective_date(obs)

        matched, basis = _match_resource(
            obs, "code",
            code_tokens=code_tokens,
            display_terms=display_terms,
        )
        if matched:
            value, unit = _extract_obs_value(obs)
            if value is not None:
                candidates.append((_parse_date_key(effective), obs, entry, value, unit, basis))
            continue

        for component in obs.get("component", []):
            matched, basis = _match_resource(
                component, "code",
                code_tokens=code_tokens,
                display_terms=display_terms,
            )
            if matched:
                value, unit = _extract_obs_value(component)
                if value is not None:
                    candidates.append((_parse_date_key(effective), obs, entry, value, unit, basis))

    if not candidates:
        return ExtractionResult(found=False)

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, obs, entry, value, unit, basis = candidates[0]

    return ExtractionResult(
        found=True,
        value=value,
        unit=unit,
        date=_get_effective_date(obs),
        fhir_reference=_resource_ref(obs, entry),
        resource_type="Observation",
        match_basis=basis,
    )


def extract_patient_age(
    ips_bundle: dict, reference_date: date
) -> ExtractionResult:
    """Compute patient age from birthDate relative to a reference date."""
    patients = _get_resources(ips_bundle, "Patient")
    if not patients:
        return ExtractionResult(found=False)

    patient = patients[0]
    birth_date_str = patient.get("birthDate")
    if not birth_date_str:
        return ExtractionResult(found=False)

    try:
        birth_date = date.fromisoformat(birth_date_str)
    except ValueError:
        return ExtractionResult(found=False)

    age = reference_date.year - birth_date.year
    if (reference_date.month, reference_date.day) < (birth_date.month, birth_date.day):
        age -= 1

    return ExtractionResult(
        found=True,
        value=age,
        unit="years",
        fhir_reference=f"Patient/{patient.get('id', 'unknown')}",
        resource_type="Patient",
    )
