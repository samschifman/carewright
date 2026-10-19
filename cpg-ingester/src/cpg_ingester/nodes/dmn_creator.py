"""DMN Creator — generates DMN 1.4 XML per decision item."""

import logging
import json
import re
import time

import mlflow
from cpg_contracts import content_to_text, decision_model_id, get_llm
from cpg_ingester.output import write_artifact
from cpg_ingester.prompts.dmn_creator import DMN_CREATOR_SYSTEM, DMN_CREATOR_USER
from cpg_ingester.reference.dmn_error_patterns import format_error_pattern_hints
from cpg_ingester.reference.dmn_examples import REFERENCE_EXAMPLES
from cpg_ingester.review_feedback import render_reviewer_feedback

logger = logging.getLogger(__name__)


def _build_feedback(syntax_errors: list, semantic_discrepancies: list,
                    previous_dmn_xml: str, engine_errors: list | None = None,
                    human_feedback: str = "") -> str:
    """Assemble repair-mode feedback: previous attempt + all labeled errors.

    Both error kinds are rendered when both are present (no masking), and matched
    known-error patterns are appended so the model gets a concrete fix, not just
    the raw message.
    """
    engine_errors = engine_errors or []
    if not syntax_errors and not semantic_discrepancies and not engine_errors and not human_feedback:
        return ""

    sections = [human_feedback] if human_feedback else []
    if syntax_errors or semantic_discrepancies or engine_errors:
        sections.append(
            "PREVIOUS ATTEMPT NEEDS CORRECTION — return a complete, corrected DMN "
            "document (not a diff, not a fragment)."
        )
    if previous_dmn_xml:
        sections.append("## Previous attempt\n" + previous_dmn_xml)
    if syntax_errors:
        sections.append("## Syntax errors to fix\n"
                        + "\n".join(f"- {e}" for e in syntax_errors))
    if semantic_discrepancies:
        sections.append("## Semantic discrepancies to fix\n"
                        + "\n".join(f"- {d}" for d in semantic_discrepancies))
    if engine_errors:
        sections.append("## Engine validation errors to fix\n"
                        + "\n".join(f"- {e}" for e in engine_errors))

    hints = format_error_pattern_hints(
        list(syntax_errors) + list(semantic_discrepancies) + list(engine_errors))
    if hints:
        sections.append("## Known error patterns\n" + hints)

    return "\n\n".join(sections)


def _format_inputs(inputs: list[dict]) -> str:
    lines = []
    for inp in inputs:
        desc = inp.get("description", "")
        codes = inp.get("codes") or []
        code_text = f" Codes: {', '.join(codes)}." if codes else ""
        extraction = inp.get("extraction")
        extraction_text = f" Extraction: {json.dumps(extraction, sort_keys=True)}." if extraction else ""
        lines.append(f"- {inp['name']} ({inp.get('type', 'string')}): {desc}{code_text}{extraction_text}")
    return "\n".join(lines) if lines else "(none specified)"


def _format_outputs(outputs: list) -> str:
    if not outputs:
        return "(none specified)"
    return "\n".join(f"- {o}" for o in outputs)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return stripped.strip()


def _ensure_model_id(dmn_xml: str, model_id: str) -> str:
    """Ensure the DMN definitions element carries the stable model id."""
    if not model_id:
        return dmn_xml
    escaped = model_id.replace('"', "")

    def replace_root(match: re.Match) -> str:
        attrs = match.group(1)
        if re.search(r"\bid\s*=", attrs):
            attrs = re.sub(r"\bid\s*=\s*(['\"]).*?\1", f'id="{escaped}"', attrs, count=1)
        else:
            attrs = f' id="{escaped}"{attrs}'
        return f"<definitions{attrs}>"

    return re.sub(r"<definitions\b([^>]*)>", replace_root, dmn_xml, count=1)


def _ensure_extraction_annotations(dmn_xml: str, inputs: list[dict]) -> str:
    """Carry manifest temporal extraction blocks into inputData extensions."""
    annotated = [value for value in inputs if value.get("extraction")]
    if not annotated:
        return dmn_xml
    if "xmlns:acp=" not in dmn_xml:
        dmn_xml = re.sub(
            r"<definitions\b",
            '<definitions xmlns:acp="https://redhat.com/cpg-to-acp/dmn"',
            dmn_xml,
            count=1,
        )

    for value in annotated:
        name = value.get("name", "")
        payload = json.dumps(value["extraction"], separators=(",", ":"), sort_keys=True)
        annotation = f"<acp:extraction><![CDATA[{payload}]]></acp:extraction>"
        blocks = re.findall(r"<inputData\b[^>]*>.*?</inputData\s*>", dmn_xml, flags=re.DOTALL)
        for block in blocks:
            variable = re.search(r"<variable\b[^>]*\bname=(['\"])(.*?)\1", block)
            if not variable or variable.group(2) != name:
                continue
            extraction_tag = re.compile(
                r"<acp:extraction\b[^>]*(?:/>|>.*?</acp:extraction\s*>)",
                flags=re.DOTALL,
            )
            if extraction_tag.search(block):
                updated = extraction_tag.sub(annotation, block, count=1)
            elif re.search(r"<extensionElements\b[^>]*>", block):
                updated = re.sub(
                    r"</extensionElements\s*>", annotation + "</extensionElements>", block, count=1
                )
            else:
                updated = re.sub(
                    r"(<variable\b)",
                    "<extensionElements>" + annotation + "</extensionElements>\\n\\1",
                    block,
                    count=1,
                )
            dmn_xml = dmn_xml.replace(block, updated, 1)
            break
    return dmn_xml


@mlflow.trace(name="dmn_creator")
def dmn_creator(state: dict) -> dict:
    """Generate DMN 1.4 XML for a decision item."""
    logger.info("── DMN Creator ──")
    item = state.get("item", {})
    source_pages = state.get("source_pages", "")
    abbreviations = state.get("abbreviations", {})
    output_dir = state.get("output_dir", "output")
    syntax_errors = state.get("syntax_errors", [])
    semantic_discrepancies = state.get("semantic_discrepancies", [])
    engine_errors = state.get("engine_errors", [])
    previous_dmn_xml = state.get("dmn_xml", "")

    name = item.get("name", "Unknown Decision")
    model_id = item.get("model_id") or decision_model_id(name)
    description = item.get("description", "")
    category = item.get("category", "treatment")
    hit_policy = item.get("hit_policy", "UNIQUE")
    inputs = item.get("inputs", [])
    outputs = item.get("outputs", [])

    human_feedback = state.get("human_feedback", "") or render_reviewer_feedback(
        state.get("review_feedback"),
        state.get("review_comment", ""),
        state.get("review_iteration", 0),
    )
    feedback = _build_feedback(
        syntax_errors, semantic_discrepancies, previous_dmn_xml, engine_errors,
        human_feedback=human_feedback,
    )

    abbr_str = "\n".join(f"- {k}: {v}" for k, v in abbreviations.items()) if abbreviations else "(none)"

    llm = get_llm(state)

    logger.info("Calling LLM...")
    t0 = time.time()
    response = llm.invoke([
        {"role": "system", "content": DMN_CREATOR_SYSTEM.format(reference=REFERENCE_EXAMPLES)},
        {"role": "user", "content": DMN_CREATOR_USER.format(
            name=name,
            model_id=model_id,
            description=description,
            category=category,
            hit_policy=hit_policy,
            inputs=_format_inputs(inputs),
            outputs=_format_outputs(outputs),
            source_pages=source_pages,
            abbreviations=abbr_str,
            feedback=feedback,
        )},
    ])
    logger.info("LLM responded in %.1fs", time.time() - t0)

    dmn_xml = _ensure_model_id(
        _strip_markdown_fences(content_to_text(response.content)), model_id)
    dmn_xml = _ensure_extraction_annotations(dmn_xml, inputs)

    safe_name = name.lower().replace(" ", "-").replace("/", "-")[:50]
    write_artifact(output_dir, f"dmn/{safe_name}.dmn", dmn_xml)

    # Separate budgets: a syntax retry and a semantic retry are counted
    # independently so one loop cannot exhaust the other's budget.
    syntax_retry_count = state.get("syntax_retry_count", 0)
    semantic_retry_count = state.get("semantic_retry_count", 0)
    if syntax_errors or engine_errors:
        syntax_retry_count += 1
    if semantic_discrepancies:
        semantic_retry_count += 1

    logger.info("DMN Creator produced XML for '%s' (%d chars)", name, len(dmn_xml))

    return {
        "dmn_xml": dmn_xml,
        "syntax_errors": [],
        "syntax_warnings": [],
        "semantic_discrepancies": [],
        "engine_errors": [],
        "engine_validation_warnings": [],
        "syntax_retry_count": syntax_retry_count,
        "semantic_retry_count": semantic_retry_count,
    }
