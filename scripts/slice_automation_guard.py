"""Evaluate and persist the live-automation guard used by the slice runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def evaluate_automation_guard(brief: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Return a reviewable guard result without raising on validation failures."""
    attached: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for activity in brief.get("activities", []) or []:
        if not isinstance(activity, Mapping):
            continue
        for automation in activity.get("automations", []) or []:
            if isinstance(automation, Mapping) and automation.get("template_refs"):
                attached.append((activity, automation))

    errors: list[str] = []
    issues = []
    if not attached:
        errors.append("Generated planning brief has no attached live automations")

    for activity, automation in attached:
        validation = automation.get("validation") or {}
        status = validation.get("status")
        findings = validation.get("errors") or []
        if status not in {"valid", "incomplete"} or findings:
            issues.append({
                "activity_id": activity.get("id"),
                "status": status,
                "errors": findings,
            })
    if issues:
        errors.append("Attached automation validation failed: " + json.dumps(issues, ensure_ascii=False))

    valid = [
        (activity, automation)
        for activity, automation in attached
        if (automation.get("validation") or {}).get("status") == "valid"
    ]
    # One instance may serve several activities (shared automation); count
    # instances by id, which is also how the FHIR Task and the automation
    # service record them.
    distinct_valid = {automation.get("id") for _, automation in valid}
    distinct_attached = {automation.get("id") for _, automation in attached}
    if valid:
        careplan = next(
            (
                entry.get("resource", {})
                for entry in bundle.get("entry", []) or []
                if entry.get("resource", {}).get("resourceType") == "CarePlan"
            ),
            {},
        )
        entries = {
            entry.get("fullUrl"): entry.get("resource", {})
            for entry in bundle.get("entry", []) or []
        }
        links = []
        for activity in careplan.get("activity", []) or []:
            for extension in activity.get("extension", []) or []:
                if str(extension.get("url", "")).endswith("/activity-automation"):
                    links.append({
                        child.get("url"): child.get("valueReference", {}).get("reference")
                        for child in extension.get("extension", []) or []
                    })
        if len(links) < len(distinct_valid):
            errors.append(
                "Generated CarePlan is missing an activity automation extension for a valid instance"
            )
        for link in links:
            task_ref = link.get("instance")
            document_ref = link.get("definition")
            if not task_ref or not document_ref:
                errors.append("Activity automation extension must reference a Task and DocumentReference")
                continue
            if entries.get(task_ref, {}).get("resourceType") != "Task":
                errors.append("Valid activity automation extension has no matching Task in the bundle")
            if entries.get(document_ref, {}).get("resourceType") != "DocumentReference":
                errors.append(
                    "Valid activity automation extension has no matching DocumentReference in the bundle"
                )

    result = {
        "ok": not errors,
        "errors": errors,
        "attached": [
            {
                "activity_id": activity.get("id"),
                "activity_description": activity.get("description", ""),
                "automation_id": automation.get("id"),
                "template_refs": automation.get("template_refs", []),
                "status": (automation.get("validation") or {}).get("status"),
                "errors": (automation.get("validation") or {}).get("errors", []),
            }
            for activity, automation in attached
        ],
        "valid_instances": len(distinct_valid),
        "attached_count": len(distinct_attached),
    }
    if errors:
        result["message"] = errors[0]
    return result


def write_automation_guard(
    brief: Mapping[str, Any],
    bundle: Mapping[str, Any],
    result_path: str | Path,
) -> dict[str, Any]:
    """Persist the guard result before the caller decides whether to fail."""
    result = evaluate_automation_guard(brief, bundle)
    path = Path(result_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def write_automation_guard_or_raise(
    brief: Mapping[str, Any],
    bundle: Mapping[str, Any],
    result_path: str | Path,
) -> dict[str, Any]:
    """Write the evidence first, then fail the caller when the guard rejects."""
    result = write_automation_guard(brief, bundle, result_path)
    if not result["ok"]:
        raise SystemExit(result["message"])
    return result
