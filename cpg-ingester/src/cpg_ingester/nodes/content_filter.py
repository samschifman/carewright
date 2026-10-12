"""Content Filter — removes irrelevant sections with deterministic safety checks."""

import logging
import re

import mlflow

from cpg_ingester.output import write_artifact

logger = logging.getLogger(__name__)

HIGH_VALUE_KEYWORDS = {
    "drug", "dose", "dosing", "dosage", "medication", "pharmacotherapy",
    "monitor", "monitoring", "threshold", "criteria", "classification",
    "stage", "staging", "algorithm", "recommendation", "table",
    "treatment", "titration", "contraindication", "risk score",
}

ABBREVIATION_HEADINGS = {
    "abbreviation", "glossary", "definitions", "acronym", "terminology",
}


def _section_contains_high_value_keywords(heading: str, section_text: str) -> list[str]:
    """Check if a section marked as 'skip' contains high-value clinical keywords."""
    combined = f"{heading} {section_text}".lower()
    found = [kw for kw in HIGH_VALUE_KEYWORDS if kw in combined]
    return found


def _is_abbreviation_section(heading: str) -> bool:
    """Check if a heading indicates an abbreviation/glossary section."""
    heading_lower = heading.lower()
    return any(term in heading_lower for term in ABBREVIATION_HEADINGS)


def _extract_section_text(markdown: str, heading: str, next_heading: str | None) -> str:
    """Extract text between two headings in markdown."""
    lines = markdown.split("\n")
    heading_lower = heading.strip().lower()
    next_lower = next_heading.strip().lower() if next_heading else None

    capturing = False
    content = []

    for line in lines:
        stripped = line.lstrip("#").strip().lower()
        if not capturing and stripped == heading_lower:
            capturing = True
            continue
        if capturing and next_lower and stripped == next_lower:
            break
        if capturing and not next_lower:
            content.append(line)
        elif capturing:
            content.append(line)

    return "\n".join(content)


def _remove_section_from_markdown(markdown: str, heading: str, next_heading: str | None) -> str:
    """Remove a section (heading + content) from markdown."""
    lines = markdown.split("\n")
    heading_lower = heading.strip().lower()
    next_lower = next_heading.strip().lower() if next_heading else None

    result = []
    skipping = False

    for line in lines:
        stripped = line.lstrip("#").strip().lower()
        if not skipping and stripped == heading_lower:
            skipping = True
            continue
        if skipping and next_lower and stripped == next_lower:
            skipping = False
        if not skipping:
            result.append(line)

    return "\n".join(result)


@mlflow.trace(name="content_filter")
def content_filter(state: dict) -> dict:
    """Remove sections classified as 'skip', with safety checks."""
    logger.info("── Content Filter ──")
    section_map = state.get("section_map", [])
    markdown = state.get("markdown", "")
    source_markdown = state.get("source_markdown") or markdown
    output_dir = state.get("output_dir", "output")

    if not section_map:
        logger.warning("No section map available, passing through unfiltered")
        return {}

    skip_sections = [s for s in section_map if s.get("classification") == "skip"]
    kept_sections = [s for s in section_map if s.get("classification") != "skip"]

    removed = []
    restored = []
    filtered_markdown = markdown

    for i, section in enumerate(section_map):
        if section.get("classification") != "skip":
            continue

        heading = section["heading"]
        next_heading = section_map[i + 1]["heading"] if i + 1 < len(section_map) else None

        if _is_abbreviation_section(heading):
            section["classification"] = "reference"
            restored.append({"heading": heading, "reason": "abbreviation/glossary section"})
            logger.info("Restored abbreviation section: %s", heading)
            continue

        section_text = _extract_section_text(markdown, heading, next_heading)
        found_keywords = _section_contains_high_value_keywords(heading, section_text)

        if found_keywords:
            section["classification"] = "recommendation"
            restored.append({"heading": heading, "reason": f"contains keywords: {', '.join(found_keywords)}"})
            logger.warning("Restored section '%s' — contains high-value keywords: %s", heading, found_keywords)
            continue

        filtered_markdown = _remove_section_from_markdown(filtered_markdown, heading, next_heading)
        removed.append(heading)

    filter_report = {
        "total_sections": len(section_map),
        "removed": removed,
        "restored": restored,
        "kept": len(section_map) - len(removed),
    }

    write_artifact(output_dir, "filter-report.json", filter_report)
    write_artifact(output_dir, "filtered.md", filtered_markdown)

    logger.info(
        "Content filter: %d sections total, %d removed, %d restored by safety check",
        len(section_map), len(removed), len(restored),
    )

    return {
        "markdown": filtered_markdown,
        # Keep the parsed source available to nodes such as metadata extraction
        # whose inputs may be removed by section filtering.
        "source_markdown": source_markdown,
        "section_map": section_map,
    }
