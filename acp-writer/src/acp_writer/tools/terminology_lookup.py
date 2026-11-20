"""Terminology Lookup — multi-system clinical code find/verify.

Supports SNOMED CT (tx.fhir.org), RxNorm (rxnav.nlm.nih.gov),
LOINC and ICD-10-CM (clinicaltables.nlm.nih.gov). Results cached
with 30-day TTL. Graceful degradation on network errors.
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

SNOMED_SYSTEM = "http://snomed.info/sct"
RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC_SYSTEM = "http://loinc.org"
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"

_TX_FHIR_BASE = "https://tx.fhir.org/r4"
_RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST"
_NLM_BASE = "https://clinicaltables.nlm.nih.gov/api"


class LookupResult:
    """Result of a terminology lookup."""

    def __init__(
        self,
        found: bool,
        system: str | None = None,
        code: str | None = None,
        display: str | None = None,
        error: str | None = None,
    ):
        self.found = found
        self.system = system
        self.code = code
        self.display = display
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"found": self.found}
        if self.system:
            d["system"] = self.system
        if self.code:
            d["code"] = self.code
        if self.display:
            d["display"] = self.display
        if self.error:
            d["error"] = self.error
        return d


class TerminologyCache:
    """Simple in-memory cache with TTL."""

    def __init__(self):
        self._cache: dict[str, tuple[float, Any]] = {}

    def _key(self, system: str, operation: str, value: str) -> str:
        raw = f"{system}|{operation}|{value}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, system: str, operation: str, value: str) -> Any | None:
        key = self._key(system, operation, value)
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.time() - ts > CACHE_TTL_SECONDS:
            del self._cache[key]
            return None
        return data

    def set(self, system: str, operation: str, value: str, data: Any) -> None:
        key = self._key(system, operation, value)
        self._cache[key] = (time.time(), data)


_cache = TerminologyCache()


def _safe_get(url: str, params: dict | None = None, timeout: int = 10) -> requests.Response | None:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        logger.warning('Terminology API error exception_type=%s (details redacted)', type(e).__name__)
        return None


# --- SNOMED CT via tx.fhir.org ---


def _snomed_find(text: str) -> LookupResult:
    cached = _cache.get(SNOMED_SYSTEM, "find", text)
    if cached is not None:
        return cached

    r = _safe_get(
        f"{_TX_FHIR_BASE}/ValueSet/$expand",
        params={
            "url": "http://snomed.info/sct?fhir_vs",
            "filter": text,
            "count": 1,
        },
    )
    if r is None:
        return LookupResult(found=False, system=SNOMED_SYSTEM, error="network_error")

    data = r.json()
    contains = data.get("expansion", {}).get("contains", [])
    if not contains:
        result = LookupResult(found=False, system=SNOMED_SYSTEM)
    else:
        entry = contains[0]
        result = LookupResult(
            found=True,
            system=SNOMED_SYSTEM,
            code=entry.get("code"),
            display=entry.get("display"),
        )
    _cache.set(SNOMED_SYSTEM, "find", text, result)
    return result


def _snomed_verify(code: str) -> LookupResult:
    cached = _cache.get(SNOMED_SYSTEM, "verify", code)
    if cached is not None:
        return cached

    r = _safe_get(
        f"{_TX_FHIR_BASE}/CodeSystem/$lookup",
        params={"system": SNOMED_SYSTEM, "code": code},
    )
    if r is None:
        return LookupResult(found=False, system=SNOMED_SYSTEM, code=code, error="network_error")

    data = r.json()
    if data.get("resourceType") == "Parameters":
        display = None
        for param in data.get("parameter", []):
            if param.get("name") == "display":
                display = param.get("valueString")
                break
        result = LookupResult(found=True, system=SNOMED_SYSTEM, code=code, display=display)
    else:
        result = LookupResult(found=False, system=SNOMED_SYSTEM, code=code)
    _cache.set(SNOMED_SYSTEM, "verify", code, result)
    return result


# --- RxNorm via rxnav.nlm.nih.gov ---


def _rxnorm_find(text: str) -> LookupResult:
    cached = _cache.get(RXNORM_SYSTEM, "find", text)
    if cached is not None:
        return cached

    for search_type in [0, 1, 2]:  # exact, normalized, approximate
        r = _safe_get(
            f"{_RXNAV_BASE}/approximateTerm.json",
            params={"term": text, "maxEntries": 1, "option": search_type},
        )
        if r is None:
            return LookupResult(found=False, system=RXNORM_SYSTEM, error="network_error")

        candidates = r.json().get("approximateGroup", {}).get("candidate", [])
        if candidates:
            rxcui = candidates[0].get("rxcui")
            r2 = _safe_get(f"{_RXNAV_BASE}/rxcui/{rxcui}/properties.json")
            display = None
            if r2:
                props = r2.json().get("properties", {})
                display = props.get("name")
            result = LookupResult(found=True, system=RXNORM_SYSTEM, code=rxcui, display=display)
            _cache.set(RXNORM_SYSTEM, "find", text, result)
            return result

    result = LookupResult(found=False, system=RXNORM_SYSTEM)
    _cache.set(RXNORM_SYSTEM, "find", text, result)
    return result


def _rxnorm_verify(code: str) -> LookupResult:
    cached = _cache.get(RXNORM_SYSTEM, "verify", code)
    if cached is not None:
        return cached

    r = _safe_get(f"{_RXNAV_BASE}/rxcui/{code}/properties.json")
    if r is None:
        return LookupResult(found=False, system=RXNORM_SYSTEM, code=code, error="network_error")

    props = r.json().get("properties")
    if props:
        result = LookupResult(
            found=True, system=RXNORM_SYSTEM, code=code, display=props.get("name")
        )
    else:
        result = LookupResult(found=False, system=RXNORM_SYSTEM, code=code)
    _cache.set(RXNORM_SYSTEM, "verify", code, result)
    return result


# --- LOINC via NLM Clinical Tables ---


def _loinc_find(text: str) -> LookupResult:
    cached = _cache.get(LOINC_SYSTEM, "find", text)
    if cached is not None:
        return cached

    r = _safe_get(
        f"{_NLM_BASE}/loinc_items/v3/search",
        params={"terms": text, "maxList": 1},
    )
    if r is None:
        return LookupResult(found=False, system=LOINC_SYSTEM, error="network_error")

    data = r.json()
    codes = data[1] if len(data) > 1 else []
    displays = data[3] if len(data) > 3 else []
    if codes:
        display = displays[0][0] if displays and displays[0] else None
        result = LookupResult(found=True, system=LOINC_SYSTEM, code=codes[0], display=display)
    else:
        result = LookupResult(found=False, system=LOINC_SYSTEM)
    _cache.set(LOINC_SYSTEM, "find", text, result)
    return result


def _loinc_verify(code: str) -> LookupResult:
    cached = _cache.get(LOINC_SYSTEM, "verify", code)
    if cached is not None:
        return cached

    r = _safe_get(
        f"{_NLM_BASE}/loinc_items/v3/search",
        params={"terms": code, "maxList": 1, "sf": "LOINC_NUM"},
    )
    if r is None:
        return LookupResult(found=False, system=LOINC_SYSTEM, code=code, error="network_error")

    data = r.json()
    codes = data[1] if len(data) > 1 else []
    displays = data[3] if len(data) > 3 else []
    if codes and codes[0] == code:
        display = displays[0][0] if displays and displays[0] else None
        result = LookupResult(found=True, system=LOINC_SYSTEM, code=code, display=display)
    else:
        result = LookupResult(found=False, system=LOINC_SYSTEM, code=code)
    _cache.set(LOINC_SYSTEM, "verify", code, result)
    return result


# --- ICD-10-CM via NLM Clinical Tables ---


def _icd10_find(text: str) -> LookupResult:
    cached = _cache.get(ICD10_SYSTEM, "find", text)
    if cached is not None:
        return cached

    r = _safe_get(
        f"{_NLM_BASE}/icd10cm/v3/search",
        params={"sf": "name,code", "terms": text, "maxList": 1},
    )
    if r is None:
        return LookupResult(found=False, system=ICD10_SYSTEM, error="network_error")

    data = r.json()
    codes = data[1] if len(data) > 1 else []
    displays = data[3] if len(data) > 3 else []
    if codes:
        display = displays[0][0] if displays and displays[0] else None
        result = LookupResult(found=True, system=ICD10_SYSTEM, code=codes[0], display=display)
    else:
        result = LookupResult(found=False, system=ICD10_SYSTEM)
    _cache.set(ICD10_SYSTEM, "find", text, result)
    return result


def _icd10_verify(code: str) -> LookupResult:
    cached = _cache.get(ICD10_SYSTEM, "verify", code)
    if cached is not None:
        return cached

    r = _safe_get(
        f"{_NLM_BASE}/icd10cm/v3/search",
        params={"sf": "code", "terms": code, "maxList": 1},
    )
    if r is None:
        return LookupResult(found=False, system=ICD10_SYSTEM, code=code, error="network_error")

    data = r.json()
    codes = data[1] if len(data) > 1 else []
    displays = data[3] if len(data) > 3 else []
    if codes and codes[0] == code:
        display = displays[0][0] if displays and displays[0] else None
        result = LookupResult(found=True, system=ICD10_SYSTEM, code=code, display=display)
    else:
        result = LookupResult(found=False, system=ICD10_SYSTEM, code=code)
    _cache.set(ICD10_SYSTEM, "verify", code, result)
    return result


# --- Public API ---

_FIND_DISPATCH = {
    SNOMED_SYSTEM: _snomed_find,
    RXNORM_SYSTEM: _rxnorm_find,
    LOINC_SYSTEM: _loinc_find,
    ICD10_SYSTEM: _icd10_find,
}

_VERIFY_DISPATCH = {
    SNOMED_SYSTEM: _snomed_verify,
    RXNORM_SYSTEM: _rxnorm_verify,
    LOINC_SYSTEM: _loinc_verify,
    ICD10_SYSTEM: _icd10_verify,
}


@trace_safe(name="terminology_find")
def find(system: str, text: str) -> LookupResult:
    """Find the best code for a clinical concept in a terminology system."""
    fn = _FIND_DISPATCH.get(system)
    if fn is None:
        return LookupResult(found=False, error=f"Unsupported system: {system}")
    return fn(text)


@trace_safe(name="terminology_verify")
def verify(system: str, code: str) -> LookupResult:
    """Verify a code exists in a terminology system and return its display."""
    fn = _VERIFY_DISPATCH.get(system)
    if fn is None:
        return LookupResult(found=False, error=f"Unsupported system: {system}")
    return fn(code)


@trace_safe(name="terminology_find_candidates")
def find_candidates(system: str, text: str, n: int = 5) -> list[LookupResult]:
    """Find top-N candidate codes for a clinical term in a terminology system.

    Like find() but returns multiple candidates for concept resolution.
    Falls back gracefully to an empty list on network failure.
    """
    dispatch = {
        SNOMED_SYSTEM: lambda t, n: _snomed_find_multi(t, n),
        RXNORM_SYSTEM: lambda t, n: _rxnorm_find_multi(t, n),
        LOINC_SYSTEM: lambda t, n: _loinc_find_multi(t, n),
        ICD10_SYSTEM: lambda t, n: _icd10_find_multi(t, n),
    }
    fn = dispatch.get(system)
    if fn is None:
        return []
    try:
        return fn(text, n)
    except Exception as exc:
        logger.warning('Terminology candidate lookup failed exception_type=%s (details redacted)', type(exc).__name__)
        return []


def _snomed_find_multi(text: str, n: int) -> list[LookupResult]:
    r = _safe_get(
        f"{_TX_FHIR_BASE}/ValueSet/$expand",
        params={"filter": text, "url": "http://snomed.info/sct?fhir_vs", "count": n},
    )
    if r is None:
        return []
    data = r.json()
    results = []
    for entry in data.get("expansion", {}).get("contains", [])[:n]:
        results.append(LookupResult(
            found=True, system=SNOMED_SYSTEM,
            code=entry.get("code"), display=entry.get("display"),
        ))
    return results


def _rxnorm_find_multi(text: str, n: int) -> list[LookupResult]:
    r = _safe_get(
        f"{_RXNAV_BASE}/approximateTerm.json",
        params={"term": text, "maxEntries": n},
    )
    if r is None:
        return []
    data = r.json()
    results = []
    for candidate in data.get("approximateGroup", {}).get("candidate", [])[:n]:
        rxcui = candidate.get("rxcui")
        if rxcui:
            results.append(LookupResult(
                found=True, system=RXNORM_SYSTEM,
                code=rxcui, display=candidate.get("name", ""),
            ))
    return results


def _loinc_find_multi(text: str, n: int) -> list[LookupResult]:
    r = _safe_get(
        f"{_NLM_BASE}/loinc_items/v3/search",
        params={"terms": text, "maxList": n},
    )
    if r is None:
        return []
    data = r.json()
    codes = data[1] if len(data) > 1 else []
    displays = data[3] if len(data) > 3 else []
    results = []
    for i, code in enumerate(codes[:n]):
        display = displays[i][0] if i < len(displays) and displays[i] else None
        results.append(LookupResult(
            found=True, system=LOINC_SYSTEM, code=code, display=display,
        ))
    return results


def _icd10_find_multi(text: str, n: int) -> list[LookupResult]:
    r = _safe_get(
        f"{_NLM_BASE}/icd10cm/v3/search",
        params={"sf": "name,code", "terms": text, "maxList": n},
    )
    if r is None:
        return []
    data = r.json()
    codes = data[1] if len(data) > 1 else []
    displays = data[3] if len(data) > 3 else []
    results = []
    for i, code in enumerate(codes[:n]):
        display = displays[i][0] if i < len(displays) and displays[i] else None
        results.append(LookupResult(
            found=True, system=ICD10_SYSTEM, code=code, display=display,
        ))
    return results


def verify_bundle_codes(bundle: dict) -> list[dict[str, Any]]:
    """Walk all coded fields in a FHIR Bundle and verify each code.

    Returns a list of issues found (empty if all codes are valid).
    """
    issues = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        resource_type = resource.get("resourceType", "unknown")
        resource_id = resource.get("id", "unknown")

        coded_fields = _extract_coded_fields(resource)
        for field_path, coding in coded_fields:
            system = coding.get("system", "")
            code = coding.get("code", "")
            if not system or not code:
                continue
            if system not in _VERIFY_DISPATCH:
                continue

            result = verify(system, code)
            if not result.found and not result.error:
                issues.append({
                    "resource": f"{resource_type}/{resource_id}",
                    "field": field_path,
                    "system": system,
                    "code": code,
                    "status": "invalid",
                })
            elif result.error:
                issues.append({
                    "resource": f"{resource_type}/{resource_id}",
                    "field": field_path,
                    "system": system,
                    "code": code,
                    "status": "unverifiable",
                    "error": result.error,
                })
    return issues


def _extract_coded_fields(
    obj: Any, path: str = "", results: list | None = None
) -> list[tuple[str, dict]]:
    """Recursively find all coding entries in a FHIR resource."""
    if results is None:
        results = []
    if isinstance(obj, dict):
        if "system" in obj and "code" in obj:
            results.append((path, obj))
        for key, val in obj.items():
            _extract_coded_fields(val, f"{path}.{key}" if path else key, results)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _extract_coded_fields(item, f"{path}[{i}]", results)
    return results
