"""LLM-assisted query plan synthesis for clinical QA.

Uses structured output to generate validated query plans directly
from the LLM — no JSON parsing or manual validation needed.
"""

import logging
from datetime import date
from enum import Enum
from typing import Any

from acp_writer.tools.ips_serializer import serialize_ips
from cpg_contracts.tracing import trace_safe
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class QueryFunction(str, Enum):
    latest_value = "latest_value"
    has_condition = "has_condition"
    has_medication = "has_medication"
    has_allergy = "has_allergy"
    has_procedure = "has_procedure"
    has_family_history = "has_family_history"
    patient_age = "patient_age"
    compute_bmi = "compute_bmi"
    observation_count = "observation_count"
    observations_in_window = "observations_in_window"
    consecutive_above = "consecutive_above"
    rate_of_change = "rate_of_change"
    cross_resource_temporal = "cross_resource_temporal"
    trend_declining = "trend_declining"


class QueryParams(BaseModel):
    code: str | None = Field(None, description="system|code token, e.g. http://loinc.org|8480-6")
    duration: str | None = Field(None, description="ISO 8601 duration, e.g. P3M")
    threshold: float | None = None
    comparator: str | None = Field(None, description="ge, gt, le, lt, or eq")
    anchor_code: str | None = None
    target_code: str | None = None
    window: str | None = None
    target_date: str | None = None


class QueryPlan(BaseModel):
    """A structured query plan for extracting clinical data from a FHIR IPS bundle."""
    function: QueryFunction = Field(description="The extraction function to call")
    params: QueryParams = Field(default_factory=QueryParams, description="Parameters for the function")


SYSTEM_PROMPT = """You are a clinical data query planner. Given a clinical question about a patient,
select the appropriate extraction function and parameters.

Available functions:
- latest_value: Get the most recent observation value. Params: code (system|code token)
- has_condition: Check if patient has an active condition. Params: code (SNOMED system|code)
- has_medication: Check if patient is on a medication. Params: code (RxNorm system|code)
- has_allergy: Check if patient has an allergy. Params: code (SNOMED system|code)
- has_procedure: Check if patient has had a procedure. Params: code (SNOMED system|code)
- has_family_history: Check for family history of a condition. Params: code (SNOMED system|code)
- patient_age: Compute patient's age. No params needed.
- compute_bmi: Compute BMI from height and weight. No params needed.
- observation_count: Count observations matching criteria in a time window. Params: code, duration, threshold (optional), comparator (optional)
- observations_in_window: Get all observations in a time window. Params: code, duration
- consecutive_above: Count consecutive readings above threshold from most recent. Params: code, threshold
- rate_of_change: Compute slope of values over time. Params: code, duration
- cross_resource_temporal: Check if target observation exists within window of anchor medication start. Params: anchor_code, target_code, window
- trend_declining: Check if most recent reading is lower than previous. Params: code

Common code tokens:
- Systolic BP: http://loinc.org|8480-6
- Diastolic BP: http://loinc.org|8462-4
- HbA1c: http://loinc.org|4548-4
- eGFR: http://loinc.org|33914-3
- Creatinine: http://loinc.org|2160-0
- Potassium: http://loinc.org|2823-3
- Fasting glucose: http://loinc.org|1558-6
- LDL: http://loinc.org|2089-1
- BNP: http://loinc.org|30934-4
- Ejection fraction: http://loinc.org|10230-1
- Heart rate: http://loinc.org|8867-4
- TSH: http://loinc.org|3016-3
- Hypertension: http://snomed.info/sct|59621000
- Type 2 diabetes: http://snomed.info/sct|44054006
- CKD: http://snomed.info/sct|709044004
- Heart failure: http://snomed.info/sct|84114007
- Atrial fibrillation: http://snomed.info/sct|49436004

Duration format: P followed by number and unit (Y=years, M=months, W=weeks, D=days)."""


def validate_plan(plan: dict) -> list[str]:
    """Validate a query plan dict. Returns list of errors (empty = valid)."""
    errors = []
    if not isinstance(plan, dict):
        return ["Plan must be a dict"]
    if "parameters" in plan and "params" not in plan:
        plan["params"] = plan.pop("parameters")
    if "function" not in plan:
        errors.append("Missing function")
    if "params" not in plan:
        errors.append("Missing params")
    return errors


@trace_safe(name="qa_generate_query_plan")
def generate_query_plan(
    question: str,
    condensed_ips: str,
    reference_date: date,
    llm_client: Any,
    inventory_text: str | None = None,
) -> dict | None:
    """Use structured output to generate a validated query plan."""
    parts = [f"Patient data (condensed):\n{condensed_ips}"]
    if inventory_text:
        parts.append(f"\nBundle code inventory (use these codes in your plan):\n{inventory_text}")
    parts.append(f"\nReference date: {reference_date.isoformat()}")
    parts.append(f"\nQuestion: {question}")
    parts.append("\nSelect the function and parameters to answer this question. Use codes from the inventory above when available.")
    user_prompt = "\n".join(parts)

    try:
        structured_llm = llm_client.with_structured_output(QueryPlan)
        plan: QueryPlan = structured_llm.invoke([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])

        return {
            "function": plan.function.value,
            "params": {k: v for k, v in plan.params.model_dump().items() if v is not None},
        }
    except Exception as exc:
        logger.warning('Structured query plan generation failed exception_type=%s (details redacted)', type(exc).__name__)
        return None
