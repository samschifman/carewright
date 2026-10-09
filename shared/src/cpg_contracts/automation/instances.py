"""Contracts for per-plan automation instances and publication."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from cpg_contracts.automation.ir import ProcessIR, TemplateRef, canonical_json, ir_version
from cpg_contracts.automation.templates import ContractModel, ValidationRecord


BindingSource = Literal[
    "cpg-default",
    "clinician",
    "plan-derived",
    "reviewer",
    "authored",
]
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ParameterBinding(ContractModel):
    """A parameter value supplied for a specific patient plan."""

    # `{task}__code` bindings carry resolved concept codes.
    name: str = Field(min_length=1)
    value: Any
    source: BindingSource
    unit: str | None = None
    note: str | None = None


class Evidence(ContractModel):
    """Patient-plan evidence referenced by a composition or prune record."""

    kind: str = Field(min_length=1)
    payload: Any


class ActivityAutomation(ContractModel):
    """A reviewable automation definition attached to care-plan activities."""

    id: str = Field(pattern=_UUID_PATTERN)
    template_refs: list[TemplateRef] = Field(min_length=1)
    template_snapshots: list[ProcessIR] = Field(min_length=1)
    ir: ProcessIR
    bpmn_xml: str = Field(min_length=1)
    bindings: list[ParameterBinding] = Field(default_factory=list)
    derivation_evidence: dict[str, Evidence] = Field(default_factory=dict)
    capabilities_used: list[str] = Field(default_factory=list)
    validation: ValidationRecord
    enabled: bool | None = None
    revision: str | None = None
    review_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_instance_contract(self) -> ActivityAutomation:
        if self.ir.process.kind != "instance":
            raise ValueError("activity automation IR must have kind 'instance'")
        refs = {(ref.template_id, ref.version) for ref in self.template_refs}
        if len(refs) != len(self.template_refs):
            raise ValueError("activity automation template references must be unique")
        snapshots: dict[str, str] = {}
        for snapshot in self.template_snapshots:
            if snapshot.process.kind != "template":
                raise ValueError("template snapshots must have kind 'template'")
            if snapshot.process.id in snapshots:
                raise ValueError("template snapshots must have unique ids")
            snapshots[snapshot.process.id] = ir_version(snapshot)
        if set(snapshots) != {template_id for template_id, _ in refs}:
            raise ValueError("template snapshots must cover exactly the referenced templates")
        if any(snapshots[template_id] != version for template_id, version in refs):
            raise ValueError("template reference versions must match their snapshots")

        process_refs = {
            (ref.template_id, ref.version) for ref in self.ir.process.acp.template_refs
        }
        if process_refs != refs:
            raise ValueError("instance IR template references must match the activity references")

        binding_names = [binding.name for binding in self.bindings]
        if len(binding_names) != len(set(binding_names)):
            raise ValueError("activity automation bindings must have unique parameter names")

        task_capabilities = {
            element.taskName
            for element in self.ir.process.flowElements
            if getattr(element, "type", None) == "task"
        }
        if len(self.capabilities_used) != len(set(self.capabilities_used)) or set(
            self.capabilities_used
        ) != task_capabilities:
            raise ValueError("capabilities_used must list each task capability exactly once")

        if self.enabled is None:
            self.enabled = self.validation.status == "valid"
        elif self.validation.status != "valid" and self.enabled:
            raise ValueError("incomplete or escalated automations cannot be enabled")

        expected_revision = _revision(self.ir, self.bindings)
        if self.revision is None:
            self.revision = expected_revision
        elif self.revision != expected_revision:
            raise ValueError("revision must match the canonical IR and bindings")
        return self


class PublishedAutomation(ContractModel):
    """One automation's FHIR resources in the publication transaction."""

    automation_id: str = Field(min_length=1)
    activity_ids: list[str] = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{12}$")
    template_refs: list[TemplateRef] = Field(min_length=1)
    task: dict[str, Any]
    document_reference: dict[str, Any]

    @model_validator(mode="after")
    def fhir_resource_types(self) -> PublishedAutomation:
        if self.task.get("resourceType") != "Task":
            raise ValueError("published automation task must be a FHIR Task resource")
        if self.document_reference.get("resourceType") != "DocumentReference":
            raise ValueError(
                "published automation document_reference must be a FHIR DocumentReference"
            )
        if len(self.activity_ids) != len(set(self.activity_ids)):
            raise ValueError("published automation activity ids must be unique")
        return self


class PublicationPayload(ContractModel):
    """Complete approved automation set sent to the automation service."""

    job_id: str = Field(min_length=1)
    careplan_id: str = Field(min_length=1)
    careplan_server_id: str = Field(min_length=1)
    patient_server_id: str = Field(min_length=1)
    replaces_careplan_id: str | None = None
    approved_at: datetime
    reviewer: str = Field(min_length=1)
    automations: list[PublishedAutomation] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_automation_ids(self) -> PublicationPayload:
        ids = [automation.automation_id for automation in self.automations]
        if len(ids) != len(set(ids)):
            raise ValueError("publication automation ids must be unique")
        return self


def _revision(ir: ProcessIR, bindings: list[ParameterBinding]) -> str:
    canonical_bindings = json.dumps(
        [
            binding.model_dump(mode="json", exclude_none=True)
            for binding in sorted(bindings, key=lambda item: item.name)
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload = canonical_json(ir) + canonical_bindings
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
