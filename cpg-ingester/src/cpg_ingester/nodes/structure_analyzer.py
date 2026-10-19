"""Structure Analyzer — detects archetype, builds section map, extracts abbreviations and grading definitions."""

import json
import logging
import re
import time

import mlflow
from cpg_contracts import content_to_text, get_llm
from cpg_ingester.output import write_artifact
from cpg_ingester.review_feedback import render_reviewer_feedback
from cpg_ingester.prompts.structure_analyzer import (
    ARCHETYPE_DETECTION_SYSTEM,
    ARCHETYPE_DETECTION_USER,
    GRADING_EXTRACTION_SYSTEM,
    GRADING_EXTRACTION_USER,
    SECTION_CLASSIFICATION_SYSTEM,
    SECTION_CLASSIFICATION_USER,
)

logger = logging.getLogger(__name__)

VALID_CLASSIFICATIONS = {"decision", "recommendation", "both", "reference", "skip"}


def _extract_sections_from_docling(docling_json: dict) -> list[dict]:
    """Extract section headings with page numbers from Docling JSON."""
    sections = []
    for text_item in docling_json.get("texts", []):
        label = text_item.get("label")
        if label in ("section_header", "title"):
            text = text_item.get("text", "")
            prov = text_item.get("prov", [])
            page_no = prov[0]["page_no"] if prov else None
            bbox = None
            if prov and "bbox" in prov[0]:
                b = prov[0]["bbox"]
                bbox = [b.get("l", 0), b.get("t", 0), b.get("r", 0), b.get("b", 0)]
            sections.append({
                "heading": text,
                "page_no": page_no,
                "bbox": bbox,
                "label": label,
            })
    return sections


def _extract_section_content(markdown: str, heading: str) -> str:
    """Extract the content under a section heading from markdown."""
    lines = markdown.split("\n")
    capturing = False
    content_lines = []
    heading_clean = heading.strip().lower()

    for line in lines:
        if line.startswith("#") and heading_clean in line.strip().lower():
            capturing = True
            continue
        if capturing and line.startswith("#"):
            break
        if capturing:
            content_lines.append(line)

    return "\n".join(content_lines).strip()


def _extract_abbreviations(markdown: str) -> dict[str, str]:
    """Extract abbreviations from the document."""
    abbreviations = {}
    pattern = re.compile(r"\b([A-Z]{2,})\s*[-–—=:]\s*(.+?)(?:\n|$)")
    for match in pattern.finditer(markdown):
        abbr = match.group(1).strip()
        definition = match.group(2).strip()
        if len(definition) > 3:
            abbreviations[abbr] = definition

    paren_pattern = re.compile(r"([A-Za-z][\w\s]+?)\s*\(([A-Z]{2,})\)")
    for match in paren_pattern.finditer(markdown):
        definition = match.group(1).strip()
        abbr = match.group(2).strip()
        if abbr not in abbreviations and len(definition) > 3:
            abbreviations[abbr] = definition

    return abbreviations


def _build_section_page_ranges(sections: list[dict], total_pages: int) -> list[dict]:
    """Add page_end to each section based on the next section's start page."""
    for i, section in enumerate(sections):
        if i + 1 < len(sections):
            section["page_end"] = sections[i + 1]["page_no"]
        else:
            section["page_end"] = total_pages
    return sections


def _parse_llm_json(text: str) -> dict | list:
    """Parse JSON from an LLM response, tolerating fences and surrounding prose.

    Strips a leading/trailing markdown fence, then tries a plain ``json.loads``.
    If that fails (the model wrapped the JSON in prose, e.g. "Here you go: {…}"),
    it locates the first ``{`` or ``[`` and uses ``raw_decode`` to read exactly
    one JSON value from there — which correctly ignores braces inside string
    values (a naive brace-counter does not). Raises ``ValueError`` if no JSON
    value can be found, so callers can degrade rather than crash.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(cleaned[min(starts):])
            return value
        except json.JSONDecodeError:
            pass
    raise json.JSONDecodeError("no JSON value found in model reply", text, 0)


@mlflow.trace(name="structure_analyzer")
def structure_analyzer(state: dict) -> dict:
    """Analyze document structure, detect archetype, build section map."""
    logger.info("── Structure Analyzer ──")
    docling_json = state.get("docling_json", {})
    markdown = state.get("markdown", "")
    output_dir = state.get("output_dir", "output")

    sections = _extract_sections_from_docling(docling_json)
    total_pages = len(docling_json.get("pages", {}))
    sections = _build_section_page_ranges(sections, total_pages)

    logger.info("Found %d sections across %d pages", len(sections), total_pages)

    abbreviations = _extract_abbreviations(markdown)
    logger.info("Extracted %d abbreviations", len(abbreviations))

    llm = get_llm(state)

    # Classify sections
    section_list = "\n".join(
        f"- Page {s['page_no']}: \"{s['heading']}\""
        for s in sections
    )
    previous_sections = state.get("prior_section_map") or sections
    section_context = {
        f"section-{index}": str(section.get("heading", ""))
        for index, section in enumerate(previous_sections)
    }
    section_context.update({
        str(item.get("id")): str(item.get("name") or item.get("title") or item.get("id"))
        for item in state.get("prior_item_manifest", [])
        if isinstance(item, dict) and item.get("id")
    })
    feedback_block = render_reviewer_feedback(
        state.get("review_feedback"),
        state.get("review_comment", ""),
        state.get("review_iteration", 0),
        item_types={"classification", "process"},
        item_context=section_context,
    )
    classification_user = SECTION_CLASSIFICATION_USER.format(section_list=section_list)
    if feedback_block:
        classification_user += "\n\n" + feedback_block
    logger.info("Classifying %d sections (LLM)...", len(sections))
    t0 = time.time()
    classification_response = llm.invoke([
        {"role": "system", "content": SECTION_CLASSIFICATION_SYSTEM},
        {"role": "user", "content": classification_user},
    ])
    logger.info("Section classification: %.1fs", time.time() - t0)
    try:
        classifications = _parse_llm_json(content_to_text(classification_response.content))
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse section classifications, defaulting to 'recommendation'")
        classifications = [{"heading": s["heading"], "classification": "recommendation", "reason": "parse failure"} for s in sections]

    class_map = {c["heading"]: c["classification"] for c in classifications if c.get("classification") in VALID_CLASSIFICATIONS}

    section_map = []
    for section in sections:
        classification = class_map.get(section["heading"], "recommendation")
        section_map.append({
            "heading": section["heading"],
            "page_start": section["page_no"],
            "page_end": section["page_end"],
            "bbox": section.get("bbox"),
            "classification": classification,
        })

    # Detect archetype
    section_headings = "\n".join(f"- {s['heading']}" for s in sections)
    first_page_content = markdown[:2000]
    logger.info("Detecting archetype (LLM)...")
    t0 = time.time()
    archetype_response = llm.invoke([
        {"role": "system", "content": ARCHETYPE_DETECTION_SYSTEM},
        {"role": "user", "content": ARCHETYPE_DETECTION_USER.format(
            section_headings=section_headings,
            first_page_content=first_page_content,
        )},
    ])
    logger.info("Archetype detection: %.1fs", time.time() - t0)
    try:
        archetype_result = _parse_llm_json(content_to_text(archetype_response.content))
        archetype = archetype_result.get("archetype", "institutional")
    except (json.JSONDecodeError, ValueError):
        archetype = "institutional"
    logger.info("Detected archetype: %s", archetype)

    # Extract grading definitions
    logger.info("Extracting grading definitions (LLM)...")
    t0 = time.time()
    grading_response = llm.invoke([
        {"role": "system", "content": GRADING_EXTRACTION_SYSTEM},
        {"role": "user", "content": GRADING_EXTRACTION_USER.format(content=markdown[:8000])},
    ])
    logger.info("Grading extraction: %.1fs", time.time() - t0)
    try:
        grading_result = _parse_llm_json(content_to_text(grading_response.content))
        grading_definitions = grading_result.get("definitions") or ""
    except (json.JSONDecodeError, ValueError):
        grading_definitions = ""

    write_artifact(output_dir, "section-map.json", section_map)
    write_artifact(output_dir, "abbreviations.json", abbreviations)

    logger.info(
        "Structure analysis complete: %d sections, archetype=%s, %d abbreviations",
        len(section_map), archetype, len(abbreviations),
    )

    return {
        "section_map": section_map,
        "abbreviations": abbreviations,
        "grading_definitions": grading_definitions,
        "archetype": archetype,
    }
