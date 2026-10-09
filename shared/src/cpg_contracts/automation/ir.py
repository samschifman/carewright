"""Pydantic models for the BPMN-shaped automation process IR.

The IR deliberately keeps BPMN element and attribute names. Validation here
covers the closed element subset, cross-reference structure, and identifiers;
expression typing and catalog conformance live in later validation rungs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from cpg_contracts.decisions import decision_model_id
from cpg_contracts.automation.expressions import ExpressionError, parse_duration
from cpg_contracts.recommendations import SourceLocation


IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
JAVA_RESERVED = frozenset(
    """
    abstract assert boolean break byte case catch char class const continue
    default do double else enum extends final finally float for goto if
    implements import instanceof int interface long native new package private
    protected public return short static strictfp super switch synchronized
    this throw throws transient try void volatile while true false null var
    record yield sealed permits
    """.split()
)
RESERVED_ELEMENT_IDS = frozenset(
    {
        "init",
        "main",
        "main_start",
        "gw_outcome",
        "end_completed",
        "f_init",
        "f_main",
        "f_after",
        "f_completed",
    }
)
RESERVED_PARAMETERS = ("max_duration",)


class StrictModel(BaseModel):
    """Base for IR models, rejecting fields outside the published contract."""

    model_config = ConfigDict(extra="forbid")


class SourceRef(StrictModel):
    kind: Literal["cpg", "authored", "reviewer", "policy"]
    source_text: str | None = None
    source_location: SourceLocation | None = None
    reviewer: str | None = None
    note: str | None = None
    previous: SourceRef | None = None
    policy: str | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> SourceRef:
        if self.kind == "cpg" and self.source_text is None:
            raise ValueError("cpg source requires source_text")
        if self.kind == "reviewer":
            missing = [
                field
                for field, value in (("previous", self.previous), ("note", self.note))
                if value is None
            ]
            if missing:
                raise ValueError(f"reviewer source requires {', '.join(missing)}")
        if self.kind == "policy" and self.policy is None:
            raise ValueError("policy source requires policy")
        return self


class Constraints(StrictModel):
    min: float | None = None
    max: float | None = None
    enum: list[str] | None = None
    pattern: str | None = None
    source: SourceRef = Field(default_factory=lambda: SourceRef(kind="authored"))


class Parameter(StrictModel):
    name: str
    type: Literal["duration", "integer", "decimal", "boolean", "string", "quantity"]
    unit: str | None = None
    default: Any | None = None
    constraints: Constraints | None = None
    required: bool = False
    description: str = ""
    source: SourceRef | None = None
    reserved: bool = False

    @model_validator(mode="after")
    def validate_parameter(self) -> Parameter:
        if self.type == "quantity" and self.unit is None:
            raise ValueError(f"quantity parameter {self.name!r} requires unit")
        if self.source is not None and self.source.kind == "authored" and self.type != "string":
            raise ValueError(
                f"authored source is allowed only on string parameter {self.name!r}"
            )
        if self.default is not None and not self.reserved and self.source is None:
            raise ValueError(f"parameter {self.name!r} default requires source")
        if self.reserved and self.default is not None:
            raise ValueError(f"reserved parameter {self.name!r} cannot have a default")
        return self


class Property(StrictModel):
    name: str
    type: Literal["duration", "integer", "decimal", "boolean", "string", "quantity", "code"]
    unit: str | None = None

    @model_validator(mode="after")
    def validate_quantity_unit(self) -> Property:
        if self.type == "quantity" and self.unit is None:
            raise ValueError(f"quantity property {self.name!r} requires unit")
        return self


class ParamRef(StrictModel):
    param: str


class PropertyRef(StrictModel):
    property: str


class ConceptRef(StrictModel):
    concept: str


class Literal_(StrictModel):
    literal: Any
    type: Literal["integer", "decimal", "boolean", "duration", "quantity", "enum", "unit", "code"]


_VALUE_REF_KEYS = {"param", "property", "concept", "literal"}


def _validate_value_ref_shape(value: Any) -> Any:
    if not isinstance(value, dict):
        raise ValueError("ValueRef must be an object")
    variant_keys = _VALUE_REF_KEYS.intersection(value)
    if len(variant_keys) != 1:
        raise ValueError("ValueRef must contain exactly one of param, property, concept, or literal")
    variant = next(iter(variant_keys))
    if variant == "literal":
        if "type" not in value:
            raise ValueError("literal ValueRef requires type")
        if value.get("type") == "string":
            raise ValueError("string literals are not allowed in ValueRef")
        if value.get("type") == "duration":
            duration = value.get("literal")
            if not isinstance(duration, str):
                raise ValueError("duration literals must be ISO-8601 strings")
            if "W" in duration:
                raise ValueError("duration literals must use day/hour form; week form is not allowed")
            try:
                parse_duration(duration)
            except ExpressionError as exc:
                raise ValueError(f"invalid duration literal: {exc}") from exc
    elif "type" in value:
        raise ValueError(f"{variant} ValueRef must not include type")
    return value


ValueRef: TypeAlias = Annotated[
    ParamRef | PropertyRef | ConceptRef | Literal_,
    BeforeValidator(_validate_value_ref_shape),
]


class SourceProvenance(StrictModel):
    kind: Literal["source"]
    source_text: str
    source_location: SourceLocation | None = None


class Justification(StrictModel):
    kind: Literal["trigger-chain", "plan-sequence", "clinician-directed"]
    evidence_id: str


class StructuralProvenance(StrictModel):
    kind: Literal["structural"]
    derivation_rule: Literal[
        "start-event",
        "end-event",
        "sequence",
        "exclusive-split",
        "exclusive-join",
        "default-flow",
        "plan-bound",
        "fragment-boundary",
        "fragment-merge",
        "parameter-binding",
        "branch-prune",
    ]
    supports: list[str]
    justification: Justification | None = None
    replaces: dict[str, Any] | None = None


Provenance: TypeAlias = Annotated[
    SourceProvenance | StructuralProvenance,
    Field(discriminator="kind"),
]


class TemplateRef(StrictModel):
    template_id: str
    version: str
    source_cpg: str | None = None
    element_id: str | None = None


class AcpElement(StrictModel):
    provenance: Provenance | None = None
    template_ref: TemplateRef | None = None
    counter: str | None = None
    resets: list[str] | None = None
    outcome: str | None = None
    structural_constant: bool = False


class FlowElementBase(StrictModel):
    id: str
    name: str = ""
    acp: AcpElement = Field(default_factory=AcpElement)


class StartEvent(FlowElementBase):
    type: Literal["startEvent"]


class EndEvent(FlowElementBase):
    type: Literal["endEvent"]


class Task(FlowElementBase):
    type: Literal["task"]
    taskName: str
    inputs: dict[str, ValueRef] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)


class UserTask(FlowElementBase):
    type: Literal["userTask"]
    groupId: Literal["clinicians", "care-team", "patients"]
    outputs: dict[str, str] = Field(default_factory=dict)


class BusinessRuleTask(FlowElementBase):
    type: Literal["businessRuleTask"]
    dmnModel: str
    inputs: dict[str, ValueRef] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)


class ExclusiveGateway(FlowElementBase):
    type: Literal["exclusiveGateway"]
    gatewayDirection: Literal["Diverging", "Converging"]
    default: str | None = None


class TimerEventDefinition(StrictModel):
    timeDuration: ValueRef | None = None
    timeCycle: ValueRef | None = None

    @model_validator(mode="after")
    def exactly_one_timer(self) -> TimerEventDefinition:
        if (self.timeDuration is None) == (self.timeCycle is None):
            raise ValueError("timerEventDefinition requires exactly one of timeDuration or timeCycle")
        return self


class IntermediateCatchEvent(FlowElementBase):
    type: Literal["intermediateCatchEvent"]
    timerEventDefinition: TimerEventDefinition


class BoundaryEvent(FlowElementBase):
    type: Literal["boundaryEvent"]
    attachedToRef: str
    cancelActivity: bool = True
    timerEventDefinition: TimerEventDefinition


class SequenceFlow(FlowElementBase):
    type: Literal["sequenceFlow"]
    sourceRef: str
    targetRef: str
    conditionExpression: str | None = None


FlowElement: TypeAlias = Annotated[
    StartEvent
    | EndEvent
    | Task
    | UserTask
    | BusinessRuleTask
    | ExclusiveGateway
    | IntermediateCatchEvent
    | BoundaryEvent
    | SequenceFlow,
    Field(discriminator="type"),
]


class Trigger(StrictModel):
    kind: Literal["dmn-output", "recommendation"]
    model_id: str | None = None
    output_name: str | None = None
    output_value: Any | None = None
    recommendation_id: str | None = None

    @model_validator(mode="after")
    def validate_trigger_fields(self) -> Trigger:
        if self.kind == "dmn-output":
            missing = [
                field
                for field, value in (
                    ("model_id", self.model_id),
                    ("output_name", self.output_name),
                    ("output_value", self.output_value),
                )
                if value is None
            ]
            if missing or self.recommendation_id is not None:
                raise ValueError(
                    "dmn-output trigger requires model_id, output_name, and output_value only"
                )
        elif (
            self.recommendation_id is None
            or self.model_id is not None
            or self.output_name is not None
            or self.output_value is not None
        ):
            raise ValueError("recommendation trigger requires recommendation_id only")
        return self


class Produces(StrictModel):
    outcome: str
    trigger: Trigger


class UnifyRecord(StrictModel):
    name: str
    members: list[str]


class PruneRecord(StrictModel):
    id: str
    template_id: str
    element_ids: list[str]
    reason: Literal["inapplicable-dmn-output", "inapplicable-category", "clinician-directed"]
    evidence_id: str


class ConsolidatedBoundary(StrictModel):
    template_id: str
    element_id: str


class ProcessProvenance(StrictModel):
    source_cpg: str
    section: str | None = None
    source_location: SourceLocation | None = None
    template_snapshots: list[Process] | None = None


class AcpProcess(StrictModel):
    triggers: list[Trigger] = Field(min_length=1)
    parameters: list[Parameter] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    produces: list[Produces] = Field(default_factory=list)
    provenance: ProcessProvenance
    template_refs: list[TemplateRef] = Field(default_factory=list)
    unify: list[UnifyRecord] = Field(default_factory=list)
    pruned: list[PruneRecord] = Field(default_factory=list)
    consolidated_boundaries: list[ConsolidatedBoundary] = Field(default_factory=list)


class Process(StrictModel):
    id: str
    name: str
    description: str = ""
    isExecutable: Literal[True] = True
    kind: Literal["template", "instance"]
    properties: list[Property] = Field(default_factory=list)
    flowElements: list[FlowElement]
    acp: AcpProcess

    @model_validator(mode="after")
    def validate_structure(self) -> Process:
        elements = self.flowElements
        nodes = [element for element in elements if not isinstance(element, SequenceFlow)]
        flows = [element for element in elements if isinstance(element, SequenceFlow)]
        ids = [element.id for element in elements]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate element id(s): {', '.join(duplicates)}")

        starts = [node for node in nodes if isinstance(node, StartEvent)]
        ends = [node for node in nodes if isinstance(node, EndEvent)]
        if len(starts) != 1:
            raise ValueError(f"process requires exactly one startEvent; found {len(starts)}")
        if not ends:
            raise ValueError("process requires at least one endEvent")

        node_by_id = {node.id: node for node in nodes}
        flow_by_id = {flow.id: flow for flow in flows}
        incoming: dict[str, list[SequenceFlow]] = {}
        outgoing: dict[str, list[SequenceFlow]] = {}
        for flow in flows:
            if flow.sourceRef not in node_by_id:
                raise ValueError(
                    f"sequenceFlow {flow.id!r} sourceRef {flow.sourceRef!r} does not resolve to a flow element"
                )
            if flow.targetRef not in node_by_id:
                raise ValueError(
                    f"sequenceFlow {flow.id!r} targetRef {flow.targetRef!r} does not resolve to a flow element"
                )
            outgoing.setdefault(flow.sourceRef, []).append(flow)
            incoming.setdefault(flow.targetRef, []).append(flow)

        for node in nodes:
            if not node.name.strip():
                raise ValueError(f"element {node.id!r} ({node.type}) requires a name")
            if isinstance(node, BoundaryEvent):
                if node.attachedToRef != "main":
                    host = node_by_id.get(node.attachedToRef)
                    if host is None:
                        raise ValueError(
                            f"boundaryEvent {node.id!r} attachedToRef {node.attachedToRef!r} does not resolve"
                        )
                    if not isinstance(host, (Task, UserTask, BusinessRuleTask)):
                        raise ValueError(
                            f"boundaryEvent {node.id!r} attachedToRef {node.attachedToRef!r} must reference an activity or 'main'"
                        )
            count = len(incoming.get(node.id, []))
            if count > 1 and not (
                isinstance(node, ExclusiveGateway) and node.gatewayDirection == "Converging"
            ):
                raise ValueError(
                    f"implicit join at {node.id!r}: {count} incoming flows require a converging exclusiveGateway"
                )

        for gateway in (node for node in nodes if isinstance(node, ExclusiveGateway)):
            gateway_outgoing = outgoing.get(gateway.id, [])
            if gateway.gatewayDirection == "Diverging":
                if gateway.default is None:
                    raise ValueError(f"diverging gateway {gateway.id!r} requires exactly one default flow")
                default_flow = flow_by_id.get(gateway.default)
                if default_flow is None or default_flow.sourceRef != gateway.id:
                    raise ValueError(
                        f"diverging gateway {gateway.id!r} default {gateway.default!r} must resolve to its outgoing flow"
                    )
                if default_flow.conditionExpression is not None:
                    raise ValueError(f"default flow {default_flow.id!r} must not have a conditionExpression")
                for flow in gateway_outgoing:
                    if flow.id != gateway.default and not flow.conditionExpression:
                        raise ValueError(
                            f"non-default outgoing flow {flow.id!r} from {gateway.id!r} requires conditionExpression"
                        )
            elif gateway.default is not None:
                raise ValueError(f"converging gateway {gateway.id!r} cannot declare a default flow")

        main_boundaries = [
            node
            for node in nodes
            if isinstance(node, BoundaryEvent) and node.attachedToRef == "main"
        ]
        if len(main_boundaries) != 1:
            raise ValueError(
                "process requires exactly one boundaryEvent attached to 'main'"
            )
        max_duration_boundary = main_boundaries[0]
        timer = max_duration_boundary.timerEventDefinition.timeDuration
        if not isinstance(timer, ParamRef) or timer.param != "max_duration":
            raise ValueError(
                f"boundaryEvent {max_duration_boundary.id!r} must use timeDuration {{param: 'max_duration'}}"
            )
        provenance = max_duration_boundary.acp.provenance
        if not (
            isinstance(provenance, StructuralProvenance)
            and provenance.derivation_rule == "plan-bound"
        ):
            raise ValueError(
                f"boundaryEvent {max_duration_boundary.id!r} requires structural provenance 'plan-bound'"
            )

        max_duration = [
            parameter for parameter in self.acp.parameters if parameter.name == "max_duration"
        ]
        if len(max_duration) != 1:
            raise ValueError("process requires exactly one reserved parameter 'max_duration'")
        reserved = max_duration[0]
        if reserved.type != "duration" or not reserved.required or not reserved.reserved:
            raise ValueError(
                "parameter 'max_duration' must be reserved, required, and have type duration"
            )
        if reserved.default is not None:
            raise ValueError("reserved parameter 'max_duration' must not have a default")

        property_names = [prop.name for prop in self.properties]
        if len(property_names) != len(set(property_names)):
            raise ValueError("property names must be unique")
        parameter_names = [parameter.name for parameter in self.acp.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("parameter names must be unique")
        if set(property_names).intersection(parameter_names):
            collisions = sorted(set(property_names).intersection(parameter_names))
            raise ValueError(f"property and parameter names must not collide: {', '.join(collisions)}")

        if self.kind == "template":
            for parameter in self.acp.parameters:
                if parameter.default is not None and parameter.source is None:
                    raise ValueError(
                        f"template parameter {parameter.name!r} with a default requires source"
                    )
        return self

    @model_validator(mode="after")
    def validate_identifiers(self) -> Process:
        for element in self.flowElements:
            self._check_identifier(element.id, "element id")
            if element.id in RESERVED_ELEMENT_IDS:
                raise ValueError(f"element id {element.id!r} is compiler-reserved")
            if element.id.startswith("main_"):
                raise ValueError(
                    f"element id {element.id!r} uses compiler-reserved prefix 'main_'"
                )
            for marker in ("_counter", "_reset", "_outcome", "_route"):
                if marker in element.id:
                    raise ValueError(
                        f"element id {element.id!r} contains compiler-reserved sequence {marker!r}"
                    )
        for prop in self.properties:
            self._check_identifier(prop.name, "property name")
            if prop.name == "outcome":
                raise ValueError("property name 'outcome' is compiler-reserved")
        for parameter in self.acp.parameters:
            self._check_identifier(parameter.name, "parameter name")
            if parameter.name == "outcome":
                raise ValueError("parameter name 'outcome' is compiler-reserved")
        return self

    @staticmethod
    def _check_identifier(value: str, kind: str) -> None:
        if not IDENTIFIER_RE.fullmatch(value):
            raise ValueError(
                f"{kind} {value!r} must match ^[a-z][a-z0-9_]*$"
            )
        if value in JAVA_RESERVED:
            raise ValueError(f"{kind} {value!r} is a Java keyword or literal")


Process.model_rebuild()
ProcessProvenance.model_rebuild()


class ProcessIR(StrictModel):
    ir_version: Literal["1.0"]
    process: Process


def canonical_json(ir: ProcessIR) -> str:
    """Return stable compact JSON for hashing and artifact comparisons."""
    data = ir.model_dump(mode="json", exclude_none=True)
    data["process"]["flowElements"] = sorted(
        data["process"]["flowElements"], key=lambda element: element["id"]
    )
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ir_version(ir: ProcessIR) -> str:
    """Return the first 12 hex characters of the canonical IR SHA-256."""
    return hashlib.sha256(canonical_json(ir).encode("utf-8")).hexdigest()[:12]


def automation_template_id(name: str) -> str:
    """Return the same stable slug used for decision model identifiers."""
    return decision_model_id(name)
