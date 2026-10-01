"""Version 1 transport only: stage metrics and findings have no clinical semantics."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

SCHEMA_VERSION = "1.0.0"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Nonempty = Annotated[str, Field(min_length=1)]
Variant = Literal["development", "baseline", "ceiling", "tuned", "holdout"]
ErrorKind = Literal[
    "configuration",
    "persistence",
    "invocation",
    "timeout",
    "parse/contract",
    "empty-result",
    "internal",
]


def now() -> str:
    return datetime.now(UTC).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Revision(Model):
    version: Nonempty
    digest: Digest


class ModelSpec(Model):
    provider: Nonempty
    endpoint: Nonempty
    model: Nonempty
    service_tier: Nonempty
    inference_parameters: dict[str, Any]


class CaseRef(Model):
    case_id: Name
    corpus: Name
    split: Literal["tuning", "holdout", "validity-only"]
    source_digests: dict[Nonempty, Digest] = Field(min_length=1)


class StageConfig(Model):
    name: Name
    configuration: Revision
    parameters: dict[str, Any]
    evaluator: Revision
    prompt: Revision


class RunConfig(Model):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    experiment_id: Name
    source_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$")]
    production_behavior: Revision
    dataset: Revision
    model: ModelSpec
    repetitions: Annotated[int, Field(strict=True, ge=1)]
    variant: Variant
    split: Literal["tuning", "holdout", "validity-only"] = "tuning"
    stages: tuple[StageConfig, ...] = Field(min_length=1)
    cases: tuple[CaseRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        if len({s.name for s in self.stages}) != len(self.stages):
            raise ValueError("Stage names must be unique")
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("Case IDs must be unique")
        if not any(c.split == self.split for c in self.cases):
            raise ValueError("Selected split has no cases")
        if (self.variant == "holdout") != (self.split == "holdout"):
            raise ValueError("Holdout split requires holdout variant and release")
        if self.variant != "development" and self.split == "validity-only":
            raise ValueError("Validity-only cases are development only")
        return self


class OperationalError(Model):
    kind: ErrorKind
    message: str
    stage: str | None = None
    case_id: str | None = None
    repetition: int | None = None
    attempt: int | None = None
    exception_type: str | None = None


class CaseResult(Model):
    case_id: Name
    usable: bool = True
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    findings: Any = None
    artifacts: dict[Name, Any] = Field(default_factory=dict)
    errors: tuple[OperationalError, ...] = ()
    expected_count: Annotated[int, Field(ge=0)] = 0
    produced_count: Annotated[int, Field(ge=0)] = 0
    accounted_count: Annotated[int, Field(ge=0)] = 0
    accounting_known: bool = True
    known_limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid(self):
        if self.usable and (
            not self.accounting_known or self.produced_count != self.accounted_count
        ):
            raise ValueError("Every generated output must be accounted for")
        if not self.usable and not self.errors:
            raise ValueError("Unusable cases require operational errors")
        return self


class StageSummary(Model):
    metrics: dict[str, FiniteFloat]
    findings: Any = None
    known_limitations: tuple[str, ...] = ()
    artifacts: dict[Name, Any] = Field(default_factory=dict)


class ArtifactRef(Model):
    name: Nonempty
    uri: Nonempty
    sha256: Digest
    size: Annotated[int, Field(ge=0)]


class RepetitionRecord(Model):
    repetition: Annotated[int, Field(ge=1)]
    cases: tuple[CaseResult, ...]
    summary: StageSummary


class StageEnvelope(Model):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    stage: Name
    configuration: StageConfig
    repetitions: tuple[RepetitionRecord, ...]
    aggregate: StageSummary
    denominator: dict[str, int]
    operational_errors: tuple[OperationalError, ...]


class RunEnvelope(Model):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    run_id: str
    started_at: str
    completed_at: str
    status: Literal["complete", "failed"]
    config: RunConfig
    config_digest: Digest
    stages: tuple[StageEnvelope, ...]
    artifacts: tuple[ArtifactRef, ...]
    operational_errors: tuple[OperationalError, ...] = ()
    mlflow_run_id: str | None = None
    artifact_uri: str
    holdout_release_digest: Digest | None = None
    holdout_revealed_at: str | None = None


class Release(Model):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    release_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: str = Field(default_factory=now)
    experiment_id: Name
    dataset: Revision
    manifest_digest: Digest
    production_behavior: Revision
    stages: tuple[StageConfig, ...]
    baseline_prompts: dict[str, Revision]
    model: ModelSpec
    repetitions: Annotated[int, Field(ge=1)]
    baseline_run_id: str
    selected_run_id: str


class Receipt(Model):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    run_id: str
    envelope: ArtifactRef
    mlflow_run_id: str | None = None
