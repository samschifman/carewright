"""Clinical concept resolver — maps natural language terms to FHIR extraction actions.

Deterministic, no LLM calls. Provides the "smart inference" layer
between free-text variable names and the extraction functions.
"""

import re
from dataclasses import dataclass
from typing import Any

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"


@dataclass
class ResolvedConcept:
    """Result of resolving a clinical term to a FHIR extraction action."""
    action: str
    system: str | None = None
    code: str | None = None
    resource_type: str | None = None
    codes: list[str] | None = None
    params: dict[str, Any] | None = None


_OBSERVATION_MAP: dict[str, tuple[str, str]] = {
    "systolic bp": (LOINC, "8480-6"),
    "systolic blood pressure": (LOINC, "8480-6"),
    "sbp": (LOINC, "8480-6"),
    "diastolic bp": (LOINC, "8462-4"),
    "diastolic blood pressure": (LOINC, "8462-4"),
    "dbp": (LOINC, "8462-4"),
    "heart rate": (LOINC, "8867-4"),
    "pulse": (LOINC, "8867-4"),
    "hr": (LOINC, "8867-4"),
    "body temperature": (LOINC, "8310-5"),
    "temperature": (LOINC, "8310-5"),
    "temp": (LOINC, "8310-5"),
    "respiratory rate": (LOINC, "9279-1"),
    "resp rate": (LOINC, "9279-1"),
    "rr": (LOINC, "9279-1"),
    "oxygen saturation": (LOINC, "2708-6"),
    "spo2": (LOINC, "2708-6"),
    "o2 sat": (LOINC, "2708-6"),
    "body weight": (LOINC, "29463-7"),
    "weight": (LOINC, "29463-7"),
    "body height": (LOINC, "8302-2"),
    "height": (LOINC, "8302-2"),
    "bmi": (LOINC, "39156-5"),
    "body mass index": (LOINC, "39156-5"),
    "hba1c": (LOINC, "4548-4"),
    "hemoglobin a1c": (LOINC, "4548-4"),
    "a1c": (LOINC, "4548-4"),
    "glycated hemoglobin": (LOINC, "4548-4"),
    "hba1c value": (LOINC, "4548-4"),
    "egfr": (LOINC, "33914-3"),
    "estimated glomerular filtration rate": (LOINC, "33914-3"),
    "glomerular filtration rate": (LOINC, "33914-3"),
    "serum creatinine": (LOINC, "2160-0"),
    "creatinine": (LOINC, "2160-0"),
    "serum potassium": (LOINC, "2823-3"),
    "potassium": (LOINC, "2823-3"),
    "fasting blood glucose": (LOINC, "1558-6"),
    "fasting glucose": (LOINC, "1558-6"),
    "fasting plasma glucose": (LOINC, "1558-6"),
    "fpg": (LOINC, "1558-6"),
    "blood sugar": (LOINC, "1558-6"),
    "blood sugar level": (LOINC, "1558-6"),
    "blood glucose": (LOINC, "2345-7"),
    "glucose": (LOINC, "2345-7"),
    "ldl cholesterol": (LOINC, "2089-1"),
    "ldl": (LOINC, "2089-1"),
    "hdl cholesterol": (LOINC, "2085-9"),
    "hdl": (LOINC, "2085-9"),
    "total cholesterol": (LOINC, "2093-3"),
    "cholesterol": (LOINC, "2093-3"),
    "triglycerides": (LOINC, "2571-8"),
    "urine albumin creatinine ratio": (LOINC, "9318-7"),
    "uacr": (LOINC, "9318-7"),
    "albumin creatinine ratio": (LOINC, "9318-7"),
    "serum sodium": (LOINC, "2951-2"),
    "sodium": (LOINC, "2951-2"),
    "bnp": (LOINC, "30934-4"),
    "b-type natriuretic peptide": (LOINC, "30934-4"),
    "tsh": (LOINC, "3016-3"),
    "thyroid stimulating hormone": (LOINC, "3016-3"),
    "inr": (LOINC, "6301-6"),
    "ejection fraction": (LOINC, "10230-1"),
    "lvef": (LOINC, "10230-1"),
    "smoking status": (LOINC, "72166-2"),
    "tobacco use": (LOINC, "72166-2"),
}

_CONDITION_MAP: dict[str, tuple[str, str]] = {
    "hypertension": (SNOMED, "59621000"),
    "essential hypertension": (SNOMED, "59621000"),
    "high blood pressure": (SNOMED, "59621000"),
    "htn": (SNOMED, "59621000"),
    "diabetes": (SNOMED, "44054006"),
    "type 2 diabetes": (SNOMED, "44054006"),
    "type 2 diabetes mellitus": (SNOMED, "44054006"),
    "t2dm": (SNOMED, "44054006"),
    "dm2": (SNOMED, "44054006"),
    "diabetes mellitus": (SNOMED, "44054006"),
    "chronic kidney disease": (SNOMED, "709044004"),
    "ckd": (SNOMED, "709044004"),
    "kidney disease": (SNOMED, "709044004"),
    "heart failure": (SNOMED, "84114007"),
    "chf": (SNOMED, "84114007"),
    "congestive heart failure": (SNOMED, "84114007"),
    "obesity": (SNOMED, "414916001"),
    "hyperlipidemia": (SNOMED, "55822004"),
    "dyslipidemia": (SNOMED, "55822004"),
    "asthma": (SNOMED, "195967001"),
    "copd": (SNOMED, "13645005"),
    "atrial fibrillation": (SNOMED, "49436004"),
    "depression": (SNOMED, "35489007"),
    "osteoarthritis": (SNOMED, "396275006"),
}

_MEDICATION_MAP: dict[str, tuple[str, str]] = {
    "metformin": (RXNORM, "861004"),
    "lisinopril": (RXNORM, "314076"),
    "amlodipine": (RXNORM, "329526"),
    "atorvastatin": (RXNORM, "259255"),
    "aspirin": (RXNORM, "243670"),
    "omeprazole": (RXNORM, "198053"),
    "gabapentin": (RXNORM, "310429"),
    "glipizide": (RXNORM, "310490"),
    "hydrochlorothiazide": (RXNORM, "310798"),
    "hctz": (RXNORM, "310798"),
    "acetaminophen": (RXNORM, "198440"),
    "warfarin": (RXNORM, "11289"),
    "losartan": (RXNORM, "979492"),
    "furosemide": (RXNORM, "197417"),
}

_DRUG_CLASS_MAP: dict[str, list[tuple[str, str]]] = {
    "ace inhibitor": [(RXNORM, "314076"), (RXNORM, "29046"), (RXNORM, "898687")],
    "acei": [(RXNORM, "314076"), (RXNORM, "29046"), (RXNORM, "898687")],
    "angiotensin converting enzyme inhibitor": [(RXNORM, "314076"), (RXNORM, "29046")],
    "arb": [(RXNORM, "979492"), (RXNORM, "349483")],
    "angiotensin receptor blocker": [(RXNORM, "979492"), (RXNORM, "349483")],
    "statin": [(RXNORM, "259255"), (RXNORM, "617311"), (RXNORM, "861634")],
    "calcium channel blocker": [(RXNORM, "329526"), (RXNORM, "329528"), (RXNORM, "898687")],
    "ccb": [(RXNORM, "329526"), (RXNORM, "329528")],
    "beta blocker": [(RXNORM, "866924"), (RXNORM, "200031")],
    "beta-blocker": [(RXNORM, "866924"), (RXNORM, "200031")],
    "thiazide": [(RXNORM, "310798")],
    "thiazide diuretic": [(RXNORM, "310798")],
    "sglt2 inhibitor": [(RXNORM, "1545653"), (RXNORM, "1992679")],
    "sglt-2 inhibitor": [(RXNORM, "1545653"), (RXNORM, "1992679")],
    "opioid": [(RXNORM, "856903"), (RXNORM, "197696")],
}

_CONDITION_HIERARCHY: dict[str, list[tuple[str, str]]] = {
    f"{SNOMED}|709044004": [
        (SNOMED, "431855005"),  # CKD stage 3
        (SNOMED, "431856006"),  # CKD stage 2
        (SNOMED, "431857002"),  # CKD stage 4
        (SNOMED, "433144002"),  # CKD stage 5
        (SNOMED, "90688005"),   # CKD stage 1
    ],
    f"{SNOMED}|44054006": [
        (SNOMED, "313436004"),  # Type 2 diabetes mellitus
        (ICD10CM, "E11"),       # ICD-10-CM T2DM
    ],
}

_ALLERGY_MAP: dict[str, tuple[str, str]] = {
    "penicillin": (SNOMED, "91936005"),
    "sulfonamide": (SNOMED, "91939002"),
    "codeine": (SNOMED, "387494007"),
}

_COMPUTED_TERMS = {"age", "patient age", "patient's age"}
_BMI_TERMS = {"bmi", "body mass index"}

_BOOLEAN_PREFIXES = [
    "has ", "does the patient have ", "is the patient ",
    "does patient have ", "is patient ",
    "currently on ", "is the patient currently on ",
    "is the patient on ", "on ",
]

_OBSERVATION_PREFIXES = [
    "what is the patients most recent ",
    "what is the patients ", "what is the patient's most recent ",
    "what is the patient's ", "what is the ",
    "patients ", "most recent ", "latest ", "current ",
]


def _normalize(text: str) -> str:
    """Normalize text for matching: lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip().rstrip("?").rstrip(".")
    text = re.sub(r"[''`]s\b", "s", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def resolve(text: str) -> ResolvedConcept | None:
    """Resolve a clinical term or question to a FHIR extraction action.

    Returns None if the term cannot be resolved.
    """
    norm = _normalize(text)

    if norm in _COMPUTED_TERMS:
        return ResolvedConcept(action="compute_age")

    if norm in _BMI_TERMS:
        return ResolvedConcept(action="compute_bmi")

    for prefix in _BOOLEAN_PREFIXES:
        if norm.startswith(prefix):
            term = norm[len(prefix):].strip()
            return _resolve_boolean(term) or _resolve_boolean(norm)

    for prefix in _OBSERVATION_PREFIXES:
        if norm.startswith(prefix):
            term = norm[len(prefix):].strip()
            if term in _COMPUTED_TERMS:
                return ResolvedConcept(action="compute_age")
            if term in _BMI_TERMS:
                return ResolvedConcept(action="compute_bmi")
            result = _resolve_observation(term)
            if result:
                return result

    result = _resolve_observation(norm)
    if result:
        return result

    result = _resolve_boolean(norm)
    if result:
        return result

    return None


def _resolve_observation(term: str) -> ResolvedConcept | None:
    if term in _OBSERVATION_MAP:
        system, code = _OBSERVATION_MAP[term]
        return ResolvedConcept(
            action="extract_observation", system=system, code=code,
            resource_type="Observation",
        )
    return None


def _strip_article(term: str) -> str:
    """Remove leading article ('a ' or 'an ') from a term."""
    return re.sub(r"^an?\s+", "", term)


def _resolve_boolean(term: str) -> ResolvedConcept | None:
    stripped = _strip_article(term)
    candidates = [term] if stripped == term else [term, stripped]
    for alias_term in candidates:
        if alias_term in _CONDITION_MAP:
            system, code = _CONDITION_MAP[alias_term]
            hierarchy_key = f"{system}|{code}"
            related = _CONDITION_HIERARCHY.get(hierarchy_key, [])
            all_codes = [(system, code)] + related
            return ResolvedConcept(
                action="extract_condition", system=system, code=code,
                resource_type="Condition",
                codes=[f"{s}|{c}" for s, c in all_codes],
            )

        if alias_term in _ALLERGY_MAP:
            system, code = _ALLERGY_MAP[alias_term]
            return ResolvedConcept(
                action="extract_allergy", system=system, code=code,
                resource_type="AllergyIntolerance",
            )

        for drug_class, members in _DRUG_CLASS_MAP.items():
            if len(alias_term) >= 3 and (drug_class in alias_term or alias_term in drug_class):
                return ResolvedConcept(
                    action="extract_drug_class",
                    resource_type="MedicationRequest",
                    codes=[f"{s}|{c}" for s, c in members],
                )

        if alias_term in _MEDICATION_MAP:
            system, code = _MEDICATION_MAP[alias_term]
            return ResolvedConcept(
                action="extract_medication", system=system, code=code,
                resource_type="MedicationRequest",
            )

        for med_name, (system, code) in _MEDICATION_MAP.items():
            if med_name in alias_term:
                return ResolvedConcept(
                    action="extract_medication", system=system, code=code,
                    resource_type="MedicationRequest",
                )

    return None
