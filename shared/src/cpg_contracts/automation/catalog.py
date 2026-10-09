"""Versioned capability catalog used by automation templates and compilers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.resources import files
import json
from typing import Any, Literal

from cpg_contracts.automation._tracing import trace


CapabilityType = Literal[
    "code",
    "duration",
    "integer",
    "decimal",
    "boolean",
    "string",
    "quantity",
    "enum",
    "unit",
]


@dataclass(slots=True)
class CapabilityIO:
    """One named input or output exposed by a capability."""

    name: str
    type: CapabilityType
    required: bool = False
    default: Any = None
    values: list[str] | None = None
    free_text: bool = False


@dataclass(slots=True)
class Capability:
    """A catalog capability and the data it accepts and returns."""

    id: str
    version: str
    direction: Literal["patient", "system", "clinician"]
    description: str
    inputs: list[CapabilityIO]
    outputs: list[CapabilityIO]
    human_in_loop: bool


@dataclass(slots=True)
class Catalog:
    """A versioned collection of capabilities."""

    version: str
    capabilities: list[Capability]

    @trace
    def get(self, capability_id: str) -> Capability:
        """Return the capability with this id, or raise ``KeyError``."""
        for capability in self.capabilities:
            if capability.id == capability_id:
                return capability
        raise KeyError(capability_id)


RESERVED_TASK_NAMES = frozenset({"Rest", "Service Task"})


@trace
@cache
def load_catalog(version: str = "1.0") -> Catalog:
    """Load a packaged capability catalog by version."""
    if version != "1.0":
        raise ValueError(f"unsupported capability catalog version {version!r}")

    catalog_path = files("cpg_contracts").joinpath("automation/catalog.v1.json")
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    if data.get("version") != version:
        raise ValueError(
            f"catalog file version {data.get('version')!r} does not match {version!r}"
        )

    def parse_io(item: dict[str, Any]) -> CapabilityIO:
        return CapabilityIO(
            name=item["name"],
            type=item["type"],
            required=item.get("required", False),
            default=item.get("default"),
            values=item.get("values"),
            free_text=item.get("free_text", False),
        )

    capabilities = [
        Capability(
            id=item["id"],
            version=item["version"],
            direction=item["direction"],
            description=item["description"],
            inputs=[parse_io(io) for io in item["inputs"]],
            outputs=[parse_io(io) for io in item["outputs"]],
            human_in_loop=item["human_in_loop"],
        )
        for item in data["capabilities"]
    ]
    return Catalog(version=data["version"], capabilities=capabilities)
