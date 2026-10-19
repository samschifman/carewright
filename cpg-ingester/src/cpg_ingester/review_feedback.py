"""Helpers for rendering item-scoped review feedback into generation prompts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def feedback_entries(value: Any) -> list[dict[str, Any]]:
    """Normalize review feedback accepted by the workflow callback."""
    if isinstance(value, str):
        text = value.strip()
        return [{"itemId": "", "itemType": "overall", "comment": text}] if text else []
    if isinstance(value, Mapping):
        nested = value.get("feedback")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            value = nested
        else:
            value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(entry) for entry in value if isinstance(entry, Mapping)]


def feedback_matches(
    entries: Sequence[Mapping[str, Any]],
    *,
    item_id: str,
    item_type: str,
) -> bool:
    """Return whether a review entry targets this manifest item."""
    normalized_type = "process" if item_type == "automation" else item_type
    for entry in entries:
        entry_id = str(entry.get("itemId", entry.get("item_id", "")))
        entry_type = str(entry.get("itemType", entry.get("item_type", "")))
        if entry_id == item_id and (entry_type == normalized_type or (
            normalized_type == "process" and entry_type == "automation"
        )):
            return True
    return False


def has_item_feedback(value: Any) -> bool:
    """Whether the review contains at least one item-scoped entry."""
    return any(
        str(entry.get("itemId", entry.get("item_id", ""))).strip()
        and str(entry.get("itemType", entry.get("item_type", ""))) != "overall"
        and str(entry.get("comment", entry.get("feedback", ""))).strip()
        for entry in feedback_entries(value)
    )


def item_feedback_ids(value: Any, item_type: str) -> set[str]:
    """Return ids targeted by a review for one manifest item type."""
    aliases = {item_type}
    if item_type == "process":
        aliases.add("automation")
    if item_type == "automation":
        aliases.add("process")
    return {
        str(entry.get("itemId", entry.get("item_id", "")))
        for entry in feedback_entries(value)
        if str(entry.get("itemType", entry.get("item_type", ""))) in aliases
        and str(entry.get("itemId", entry.get("item_id", ""))).strip()
        and str(entry.get("comment", entry.get("feedback", ""))).strip()
    }


def render_reviewer_feedback(
    feedback: Any,
    overall_comment: Any = "",
    iteration: Any = 0,
    *,
    item_types: set[str] | None = None,
    item_context: Mapping[str, str] | None = None,
    item_id: str | None = None,
    item_ids: set[str] | None = None,
) -> str:
    """Render a feedback block; return empty text when review supplied none."""
    entries = feedback_entries(feedback)
    context = item_context or {}
    lines: list[str] = []
    for entry in entries:
        comment = str(entry.get("comment", entry.get("feedback", ""))).strip()
        if not comment:
            continue
        entry_id = str(entry.get("itemId", entry.get("item_id", "")))
        item_type = str(entry.get("itemType", entry.get("item_type", "item")))
        is_overall = not entry_id or item_type == "overall"
        if item_types is not None and not is_overall:
            compatible_types = set(item_types)
            if "process" in compatible_types:
                compatible_types.add("automation")
            if item_type not in compatible_types:
                continue
        if item_id is not None and not is_overall and entry_id != str(item_id):
            continue
        if item_ids is not None and not is_overall and entry_id not in item_ids:
            continue
        label = context.get(entry_id) or entry_id or "overall"
        lines.append(f"- [{item_type}] {label}: {comment}")

    comment = str(overall_comment or "").strip()
    if comment:
        lines.append(f"- Overall comment: {comment}")
    if not lines:
        return ""
    round_number = iteration if iteration else 1
    return f"## Reviewer feedback from round {round_number}\n" + "\n".join(lines)
