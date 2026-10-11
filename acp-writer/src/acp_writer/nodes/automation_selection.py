"""Deterministic matching of plan activities to published templates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import mlflow

from cpg_contracts.automation.templates import AutomationTemplate


class TemplateRegistry(Protocol):
    def list(
        self,
        *,
        recommendation_id: str | None = None,
        dmn_model_id: str | None = None,
    ) -> list[AutomationTemplate]: ...


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


@mlflow.trace(name="automation.select_templates")
def select_templates(
    activity: Any,
    registry: TemplateRegistry,
    dmn_audit_trail: list[Any],
) -> list[AutomationTemplate]:
    """Select linked-recommendation and matching DMN-output templates."""
    recommendation_id = _field(activity, "source_recommendation_id")
    rec_candidates = (
        registry.list(recommendation_id=recommendation_id)
        if recommendation_id
        else []
    )

    dmn_candidates: list[AutomationTemplate] = []
    dmn_call_index = _field(activity, "source_dmn_call")
    if (
        isinstance(dmn_call_index, int)
        and not isinstance(dmn_call_index, bool)
        and 0 <= dmn_call_index < len(dmn_audit_trail)
    ):
        audit_entry = dmn_audit_trail[dmn_call_index]
        model_id = _field(audit_entry, "model_id")
        outputs = _field(audit_entry, "outputs", {}) or {}
        if model_id and isinstance(outputs, Mapping):
            for candidate in registry.list(dmn_model_id=model_id):
                matches = any(
                    trigger.kind == "dmn-output"
                    and trigger.model_id == model_id
                    and trigger.output_name in outputs
                    and outputs[trigger.output_name] == trigger.output_value
                    for trigger in candidate.summary.triggers
                )
                if matches:
                    dmn_candidates.append(candidate)

    selected: dict[tuple[str, str], AutomationTemplate] = {}
    for candidate in [*rec_candidates, *dmn_candidates]:
        if (
            recommendation_id
            and recommendation_id in candidate.summary.linked_recommendation_ids
        ) or candidate in dmn_candidates:
            key = (candidate.summary.id, candidate.summary.version)
            selected[key] = candidate
    return [selected[key] for key in sorted(selected)]
