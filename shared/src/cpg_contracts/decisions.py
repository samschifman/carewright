"""Contract types for the decision model boundary between cpg-ingester and acp-writer.

These types define the API contract. Both components depend on this package;
neither depends on the other.
"""

from datetime import datetime
from enum import Enum
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from cpg_contracts.recommendations import SourceLocation


def decision_model_id(name: str) -> str:
    """Return the stable wire identifier shared by ingester and writer."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "unknown").lower()).strip("-")
    return slug or "unknown"


class DecisionCategory(str, Enum):
    TREATMENT = "treatment"
    SCREENING = "screening"
    MONITORING = "monitoring"
    RISK_ASSESSMENT = "risk-assessment"
    DIAGNOSTIC = "diagnostic"


EXTRACTION_FUNCTIONS = (
    "observations_in_window",
    "observation_count",
    "consecutive_above",
    "rate_of_change",
    "cross_resource_temporal",
)
COMPARATORS = ("ge", "gt", "le", "lt", "eq")
REQUIRED_EXTRACTION_PARAMS = {
    "observations_in_window": ("code", "duration"),
    "observation_count": ("code", "duration"),
    "consecutive_above": ("code", "threshold"),
    "rate_of_change": ("code", "duration"),
    "cross_resource_temporal": ("anchor_code", "target_code", "window"),
}
_DURATION_RE = re.compile(r"^P(?:\d+[YMWD])+$")
_CODE_TOKEN_RE = re.compile(r"^https?://[^|\s]+\|[^|\s]+$")


class Extraction(BaseModel):
    function: Literal[
        "observations_in_window", "observation_count", "consecutive_above",
        "rate_of_change", "cross_resource_temporal",
    ]
    params: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_params(self) -> "Extraction":
        required = REQUIRED_EXTRACTION_PARAMS[self.function]
        missing = [name for name in required if name not in self.params]
        if missing:
            raise ValueError(f"missing required parameter(s): {', '.join(missing)}")
        params = self.params
        if "comparator" in params and params["comparator"] not in COMPARATORS:
            raise ValueError(f"comparator must be one of {COMPARATORS}")
        if self.function == "consecutive_above" and "comparator" in params:
            raise ValueError("comparator is not supported by consecutive_above")
        if self.function == "observation_count" and (
            ("threshold" in params) != ("comparator" in params)
        ):
            raise ValueError("threshold and comparator must be provided together")
        if "threshold" in params and (
            isinstance(params["threshold"], bool)
            or not isinstance(params["threshold"], (int, float))
        ):
            raise ValueError("threshold must be numeric and not boolean")
        for key in ("duration", "window"):
            if key in params and (
                not isinstance(params[key], str) or not _DURATION_RE.fullmatch(params[key])
            ):
                raise ValueError(f"{key} must be an ISO-8601 duration")
        for key in ("code", "anchor_code", "target_code"):
            if key in params and (
                not isinstance(params[key], str) or not _CODE_TOKEN_RE.fullmatch(params[key])
            ):
                raise ValueError(f"{key} must be a system|code token")
        return self


class DecisionVariable(BaseModel):
    name: str
    type: str
    description: str | None = None
    codes: list[str] | None = None
    extraction: Extraction | None = None
    """Clinical terminology codes for this variable.

    Format: ``["<system-url>|<code>", ...]`` using the FHIR token search
    format, e.g. ``["http://loinc.org|8480-6"]`` for systolic BP.

    Populated by cpg-ingester during DMN generation (see GitHub #85).
    acp-writer uses these to map DMN inputs to FHIR patient data.
    """


class DecisionModelSummary(BaseModel):
    id: str
    name: str
    inputs: list[DecisionVariable]
    outputs: list[DecisionVariable]
    deployed_at: datetime | None = None
    source_cpg: str | None = None
    category: DecisionCategory | None = None
    modifies: list[str] | None = None
    source_location: SourceLocation | None = None
    namespace: str | None = None


class DecisionEvaluationRequest(BaseModel):
    model_id: str
    inputs: dict


class DecisionEvaluationResponse(BaseModel):
    model_id: str
    outputs: dict
