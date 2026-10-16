"""Load and validate operational automation parameter defaults."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import mlflow
import yaml

from cpg_contracts.automation.compiler.kogito_profile import normalize_duration
from cpg_contracts.automation.ir import Process, ProcessIR


_DEFAULT_POLICY = Path(__file__).with_name("automation_policy.yaml")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _process(template: Any) -> Process | None:
    if isinstance(template, ProcessIR):
        return template.process
    if isinstance(template, Process):
        return template
    candidate = _field(template, "ir")
    if isinstance(candidate, ProcessIR):
        return candidate.process
    if isinstance(candidate, Mapping):
        try:
            return ProcessIR.model_validate({"ir_version": "1.0", "process": candidate}).process
        except Exception:
            return None
    return None


def _template_parameters(templates: Sequence[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for template in templates:
        process = _process(template)
        if process is None:
            continue
        for parameter in process.acp.parameters:
            result.setdefault(parameter.name, []).append(parameter)
    return result


def _source_text(parameter: Any) -> str:
    sources = [
        _field(_field(parameter, "source"), "source_text", ""),
        _field(_field(_field(parameter, "constraints"), "source"), "source_text", ""),
    ]
    return "\n".join(text for text in sources if isinstance(text, str))


def _validate_value(name: str, entry: Mapping[str, Any], parameter: Any) -> None:
    parameter_type = _field(parameter, "type")
    if parameter_type in {"quantity", "code"}:
        raise ValueError(f"automation policy cannot bind {parameter_type} parameter {name!r}")
    if "threshold" in str(_field(parameter, "description", "")).casefold() or "threshold" in _source_text(parameter).casefold():
        raise ValueError(f"automation policy cannot bind threshold parameter {name!r}")
    if set(entry) - {"value", "unit", "enum", "note"}:
        raise ValueError(f"automation policy parameter {name!r} has unsupported fields")
    if "value" not in entry or not isinstance(entry.get("note"), str) or not entry["note"].strip():
        raise ValueError(f"automation policy parameter {name!r} requires value and note")

    value = entry["value"]
    type_ok = {
        "duration": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "decimal": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "string": isinstance(value, str),
    }.get(parameter_type, False)
    if not type_ok:
        raise ValueError(f"automation policy value for {name!r} does not match {parameter_type!r}")
    if parameter_type == "duration":
        try:
            normalize_duration(value)
        except ValueError as exc:
            raise ValueError(f"automation policy duration for {name!r} is invalid") from exc

    unit = entry.get("unit")
    parameter_unit = _field(parameter, "unit")
    if unit is not None and (not parameter_unit or unit != parameter_unit):
        raise ValueError(f"automation policy unit for {name!r} does not match its parameter")
    constraints = _field(parameter, "constraints")
    allowed_enum = _field(constraints, "enum")
    enum = entry.get("enum")
    if enum is not None:
        if not isinstance(allowed_enum, list) or enum != allowed_enum:
            raise ValueError(f"automation policy enum for {name!r} does not match its parameter")
    if allowed_enum is not None and value not in allowed_enum:
        raise ValueError(f"automation policy value for {name!r} is outside its enum")


@mlflow.trace(name="automation.validate_policy")
def validate_automation_policy(
    templates: Sequence[Any],
    policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate configured defaults against the selected template snapshots."""
    normalized = dict(policy or {})
    parameters = normalized.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise ValueError("automation_policy.parameters must be a mapping")
    inventory = _template_parameters(templates)
    for name, entry in parameters.items():
        if not isinstance(name, str) or not isinstance(entry, Mapping):
            raise ValueError("automation policy entries must map names to objects")
        declared_type = str(entry.get("type", "")).casefold()
        if declared_type in {"quantity", "code"} or name.casefold().endswith("__code"):
            raise ValueError(f"automation policy cannot contain quantity/code entry {name!r}")
        if "unit" in entry and "enum" in entry:
            raise ValueError(f"automation policy parameter {name!r} cannot specify both unit and enum")
        for parameter in inventory.get(name, []):
            _validate_value(name, entry, parameter)
        if not isinstance(entry.get("note"), str) or not entry["note"].strip():
            raise ValueError(f"automation policy parameter {name!r} requires a note")
    normalized["parameters"] = {str(name): dict(entry) for name, entry in parameters.items()}
    return normalized


@mlflow.trace(name="automation.load_policy")
def load_automation_policy(
    templates: Sequence[Any],
    *,
    configured: Mapping[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Load packaged operational defaults, apply runtime overrides, and validate."""
    policy_path = path or _DEFAULT_POLICY
    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not load automation policy: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ValueError("automation policy file must contain a mapping")
    base = loaded.get("automation_policy", loaded)
    if not isinstance(base, Mapping):
        raise ValueError("automation_policy must be a mapping")
    merged = dict(base)
    supplied = dict(configured or {})
    merged.update({key: value for key, value in supplied.items() if key != "parameters"})
    parameters = dict(merged.get("parameters", {}) or {})
    overrides = supplied.get("parameters", {}) or {}
    if not isinstance(overrides, Mapping):
        raise ValueError("automation_policy.parameters must be a mapping")
    parameters.update(overrides)
    merged["parameters"] = parameters
    return validate_automation_policy(templates, merged)


__all__ = ["load_automation_policy", "validate_automation_policy"]
