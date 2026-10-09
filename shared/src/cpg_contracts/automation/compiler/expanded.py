"""Intermediate, non-XML representation of expanded BPMN elements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cpg_contracts.automation.ir import ProcessIR


@dataclass(slots=True)
class XIo:
    """Expanded data input/output and its source, target, or literal value."""

    id: str
    name: str
    direction: Literal["input", "output"]
    type: str | None = None
    drools_type: str | None = None
    item_subject_ref: str | None = None
    source_ref: str | None = None
    target_ref: str | None = None
    literal: str | None = None


@dataclass(slots=True)
class XProperty:
    """Expanded process property or declared automation parameter."""

    id: str
    name: str
    type: str
    structure_ref: str
    unit: str | None = None


@dataclass(slots=True)
class XFlow:
    """Expanded sequence flow, including an optional Java or IR condition."""

    id: str
    source_ref: str
    target_ref: str
    name: str | None = None
    condition_expression: str | None = None
    condition_is_java: bool = False
    element_payload: str | None = None
    resets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class XNode:
    """Expanded BPMN node with optional I/O, script, timer, or child scope."""

    type: str
    id: str
    name: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    work_name: str | None = None
    implementation: str | None = None
    io: list[XIo] = field(default_factory=list)
    children: list[XNode] = field(default_factory=list)
    flows: list[XFlow] = field(default_factory=list)
    script_format: str | None = None
    script: str | None = None
    timer_kind: str | None = None
    timer_expression: str | None = None
    element_payload: str | None = None
    counter: str | None = None
    outcome: str | None = None


@dataclass(slots=True)
class XProcess:
    """Expanded process tree ready for deterministic BPMN serialization."""

    id: str
    name: str
    definitions_id: str
    target_namespace: str
    version: str
    catalog_version: str
    profile_version: str
    ir: ProcessIR
    properties: list[XProperty] = field(default_factory=list)
    nodes: list[XNode] = field(default_factory=list)
    flows: list[XFlow] = field(default_factory=list)
    item_definitions: list[tuple[str, str]] = field(default_factory=list)
    template_payload: dict[str, Any] = field(default_factory=dict)
