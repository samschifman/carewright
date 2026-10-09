"""Contracts for validated automation templates and their summaries."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cpg_contracts.automation.ir import Parameter, ProcessIR, Trigger, ir_version
from cpg_contracts.recommendations import SourceLocation


PatternFamily = Literal[
    "schedule-and-check",
    "wait-for-result-then-decide",
    "remind-until-done",
    "escalate-on-threshold",
    "do-confirm-repeat",
    "other",
]


class ContractModel(BaseModel):
    """Base for the public automation contracts."""

    model_config = ConfigDict(extra="forbid")


class Automatability(ContractModel):
    """Whether a CPG process is ready for automation and why."""

    tier: Literal["A", "B"]
    rationale: str = Field(min_length=1)


class ValidationRecord(ContractModel):
    """Validation outcome retained with a template or instance."""

    status: Literal["valid", "incomplete", "escalated"]
    rungs_passed: list[str] = Field(default_factory=list)
    deferred_invariants: list[str] = Field(default_factory=list)
    kogito_checked: bool = False
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    acknowledgement: str | None = None


class AutomationTemplateSummary(ContractModel):
    """Searchable metadata for one immutable automation template version."""

    id: str = Field(min_length=1)
    version: str = Field(pattern=r"^[0-9a-f]{12}$")
    name: str = Field(min_length=1)
    description: str = ""
    source_cpg: str = Field(min_length=1)
    section: str | None = None
    source_location: SourceLocation | None = None
    triggers: list[Trigger] = Field(min_length=1)
    linked_recommendation_ids: list[str] = Field(min_length=1)
    linked_decision_model_ids: list[str] = Field(default_factory=list)
    parameters: list[Parameter] = Field(default_factory=list)
    capabilities_used: list[str] = Field(default_factory=list)
    catalog_version: str = Field(min_length=1)
    ir_version: str = "1.0"
    pattern_family: PatternFamily
    automatability: Automatability
    validation: ValidationRecord
    artifact_id: str = Field(min_length=1)


class AutomationTemplate(ContractModel):
    """Published template metadata, source IR, and compiled BPMN artifact."""

    contract_version: Literal["1.1"] = "1.1"
    summary: AutomationTemplateSummary
    ir: ProcessIR
    bpmn_xml: str = Field(min_length=1)

    @model_validator(mode="after")
    def matches_ir(self) -> AutomationTemplate:
        process = self.ir.process
        if process.kind != "template":
            raise ValueError("automation template IR must have kind 'template'")
        if self.summary.id != process.id:
            raise ValueError("template summary id must match the IR process id")
        if self.summary.version != ir_version(self.ir):
            raise ValueError("template version must be the canonical IR hash")
        if self.summary.ir_version != self.ir.ir_version:
            raise ValueError("template summary IR version must match the IR")
        if self.summary.name != process.name or self.summary.description != process.description:
            raise ValueError("template summary name and description must match the IR")
        if self.summary.triggers != process.acp.triggers:
            raise ValueError("template summary triggers must match the IR")
        if self.summary.parameters != process.acp.parameters:
            raise ValueError("template summary parameters must match the IR")
        return self
