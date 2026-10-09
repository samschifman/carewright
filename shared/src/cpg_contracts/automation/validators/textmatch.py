"""Token-level normalization and fuzzy containment for clinical source text."""

from __future__ import annotations

from collections.abc import Mapping
from difflib import SequenceMatcher
import re

from cpg_contracts.automation._tracing import trace


@trace(name="bpmn.l5.text_normalize")
def normalize(text: str, abbreviations: Mapping[str, str] | None = None) -> str:
    """Lowercase text, expand known abbreviations, and return normalized tokens."""
    expanded = text
    for abbreviation, expansion in sorted(
        (abbreviations or {}).items(), key=lambda item: len(item[0]), reverse=True
    ):
        expanded = re.sub(
            rf"(?<!\w){re.escape(abbreviation)}(?!\w)",
            f" {expansion} ",
            expanded,
            flags=re.IGNORECASE,
        )
    return " ".join(re.findall(r"[a-z0-9]+", expanded.lower()))


@trace(name="bpmn.l5.text_contains")
def contains(
    section: str,
    snippet: str,
    threshold: float = 0.9,
    *,
    abbreviations: Mapping[str, str] | None = None,
) -> bool:
    """Check whether a normalized source snippet occurs in a section text window."""
    section_tokens = normalize(section, abbreviations).split()
    snippet_tokens = normalize(snippet, abbreviations).split()
    if not snippet_tokens:
        return False
    if len(snippet_tokens) > len(section_tokens):
        return False

    width = len(snippet_tokens)
    if any(
        section_tokens[start : start + width] == snippet_tokens
        for start in range(len(section_tokens) - width + 1)
    ):
        return True

    tolerance = max(1, round(width * (1 - threshold)))
    min_width = max(1, width - tolerance)
    max_width = min(len(section_tokens), width + tolerance)
    for window_width in range(min_width, max_width + 1):
        for start in range(len(section_tokens) - window_width + 1):
            window = section_tokens[start : start + window_width]
            ratio = SequenceMatcher(
                None, snippet_tokens, window, autojunk=False
            ).ratio()
            if ratio >= threshold:
                return True
    return False
