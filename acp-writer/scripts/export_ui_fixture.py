#!/usr/bin/env python3
"""Export a planning brief and its FHIR transaction as the UI CarePlanView fixture."""

import argparse
import json
from pathlib import Path
from typing import Any

import mlflow

from acp_writer.services.artifact_resolver import (
    plan_activity_from_entry,
    plan_conflict_from_entry,
    plan_goal_from_entry,
)
from acp_writer.services.bff import _extract_view_from_bundle


@mlflow.trace(name="export_careplan_ui_fixture")
def export_fixture(brief: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """Combine brief descriptions with automation read-back from the approved bundle."""
    goals = [
        plan_goal_from_entry(item, index)
        for index, item in enumerate(brief.get("goals", []))
        if isinstance(item, dict)
    ]
    activities = [
        plan_activity_from_entry(item, index)
        for index, item in enumerate(brief.get("activities", []))
        if isinstance(item, dict)
    ]
    _persisted_goals, persisted_activities, persisted_conflicts = _extract_view_from_bundle(bundle)
    persisted_by_id = {item.get("id"): item for item in persisted_activities}
    for activity in activities:
        persisted = persisted_by_id.get(activity.get("id"), {})
        if persisted.get("automations"):
            activity["automations"] = persisted["automations"]
    conflicts = persisted_conflicts or [
        plan_conflict_from_entry(item)
        for item in brief.get("conflicts", [])
        if isinstance(item, dict)
    ]
    return {
        "goals": goals,
        "activities": activities,
        "conflicts": conflicts,
        "fhirBundle": bundle,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", required=True, type=Path, help="planning_brief.json")
    parser.add_argument("--bundle", required=True, type=Path, help="approved FHIR transaction bundle JSON")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parents[1] / "ui/src/mocks/fixtures/slice-careplan.json",
    )
    args = parser.parse_args()
    brief = json.loads(args.brief.read_text())
    brief = brief.get("planning_brief", brief)
    bundle = json.loads(args.bundle.read_text())
    view = export_fixture(brief, bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(view, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
