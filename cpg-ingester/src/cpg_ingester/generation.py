"""DMN, recommendation, and BPMN generation subgraphs with review loops.

Extracted from pipeline.py to avoid importing Docling in the
LLM Analysis pod. This module only imports generation nodes — no Docling,
structure analysis, assembly, or delivery.
"""

import logging
import os
import re
from urllib.parse import urlencode

import mlflow
import requests
from langgraph.graph import END, START, StateGraph
from lxml import etree
from cpg_contracts import DecisionCategory, DecisionModelSummary, DecisionVariable, SourceLocation, decision_model_id
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.ir import ProcessIR, Task, ir_version
from cpg_contracts.automation.templates import (
    Automatability,
    AutomationTemplate,
    AutomationTemplateSummary,
    ValidationRecord,
)

from cpg_ingester.nodes.dmn_creator import dmn_creator
from cpg_ingester.nodes.dmn_semantic_reviewer import dmn_semantic_reviewer
from cpg_ingester.nodes.dmn_syntax_validator import dmn_syntax_validator
from cpg_ingester.nodes.rec_extractor import rec_extractor
from cpg_ingester.nodes.rec_schema_validator import rec_schema_validator
from cpg_ingester.nodes.rec_semantic_reviewer import rec_semantic_reviewer
from cpg_ingester.state import BPMNPipelineState, DMNPipelineState, RecPipelineState
from cpg_ingester.nodes.bpmn_ir_creator import bpmn_ir_creator
from cpg_ingester.nodes.bpmn_semantic_reviewer import bpmn_semantic_reviewer
from cpg_ingester.nodes.bpmn_validator import bpmn_compile_and_validate
from cpg_ingester.output import write_artifact

logger = logging.getLogger(__name__)

# Separate retry budgets for the two failure modes so a run of syntax retries
# cannot starve the semantic review (and vice versa).
MAX_DMN_SYNTAX_RETRIES = 3
MAX_DMN_SEMANTIC_RETRIES = 2
MAX_REC_REVIEWS = 2
MAX_BPMN_STATIC_RETRIES = 3
MAX_BPMN_SEMANTIC_RETRIES = 2


def _extract_section_text(markdown: str, section_map: list, section_id: str) -> str:
    """Extract the markdown text for a section by matching its heading.

    Finds the section heading in the markdown and returns all text from that
    heading to the next heading of equal or higher level.
    """
    if not markdown or not section_id:
        return ""

    matching = [s for s in section_map if section_id in s.get("heading", "")]
    if not matching:
        return ""

    heading = matching[0].get("heading", "")
    lines = markdown.split("\n")

    start_idx = None
    heading_level = 0
    for i, line in enumerate(lines):
        if heading in line and line.strip().startswith("#"):
            start_idx = i
            heading_level = len(line) - len(line.lstrip("#"))
            break

    if start_idx is None:
        return ""

    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        line = lines[i].strip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= heading_level:
                text_after_hash = line.lstrip("#").strip()
                if re.match(r"\d", text_after_hash):
                    end_idx = i
                    break

    return "\n".join(lines[start_idx:end_idx]).strip()


# --- DMN subgraph routing ---

def _route_after_dmn_syntax(state: DMNPipelineState) -> str:
    if state.get("syntax_errors"):
        if state.get("syntax_retry_count", 0) >= MAX_DMN_SYNTAX_RETRIES:
            return "dmn_escalate"
        return "dmn_creator"
    return "dmn_semantic_reviewer"


def _route_after_dmn_semantic(state: DMNPipelineState) -> str:
    # A hard escalation from the reviewer (no source text, unparseable output)
    # bypasses the retry budget — retrying cannot fix either condition.
    if state.get("force_escalate"):
        return "dmn_escalate"
    if state.get("semantic_discrepancies"):
        if state.get("semantic_retry_count", 0) >= MAX_DMN_SEMANTIC_RETRIES:
            return "dmn_escalate"
        return "dmn_creator"
    return "dmn_accept"


def _dmn_accept(state: DMNPipelineState) -> dict:
    logger.info("DMN accepted: %s", state.get("item", {}).get("name", "unknown"))
    return {"escalated": False}


def _dmn_escalate(state: DMNPipelineState) -> dict:
    name = state.get("item", {}).get("name", "unknown")
    reason = state.get("escalation_reason")
    if not reason:
        if state.get("syntax_errors"):
            reason = "syntax-budget-exhausted"
        elif state.get("engine_errors"):
            reason = "engine-validation-failed"
        elif state.get("semantic_discrepancies"):
            reason = "semantic-budget-exhausted"
        else:
            reason = "unknown"
    errors = (
        state.get("syntax_errors")
        or state.get("engine_errors")
        or state.get("semantic_discrepancies")
        or []
    )
    logger.warning("DMN escalated for human review: %s (%s)", name, reason)
    return {"escalated": True, "escalation_reason": reason, "escalation_errors": list(errors)}


def _coerce_page_number(value):
    """Return an integer page number, ignoring compound/non-numeric labels."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _decision_summary(item: dict, state: dict, decision_items: list[dict], dmn_xml: str = "") -> dict:
    """Build the stable decision contract from the approved manifest item."""
    decision_by_guid = {entry.get("id"): entry for entry in decision_items}
    modifies: list[str] = []
    explicit = item.get("modifies")
    target = decision_by_guid.get(explicit)
    if target and target.get("model_id"):
        modifies.append(target["model_id"])

    inputs = [DecisionVariable.model_validate({
        "name": value.get("name", ""),
        "type": value.get("type", "string"),
        "description": value.get("description"),
        "codes": value.get("codes"),
        "extraction": value.get("extraction"),
    }) for value in item.get("inputs", [])]
    outputs = []
    namespace = None
    if dmn_xml:
        root = etree.fromstring(dmn_xml.encode("utf-8"))
        namespace = root.get("namespace")
        output_columns = root.xpath(
            './/*[local-name()="decisionTable"]/*[local-name()="output"]'
        )
        outputs = [
            DecisionVariable(
                name=output.get("name", ""),
                type=output.get("typeRef", "string"),
            )
            for output in output_columns
        ]
    location = None
    page_start = _coerce_page_number(item.get("page_start"))
    page_end = _coerce_page_number(item.get("page_end"))
    if page_start is not None:
        location = SourceLocation(page_start=page_start, page_end=page_end)
    category = None
    try:
        category = DecisionCategory(item.get("category")) if item.get("category") else None
    except ValueError:
        logger.warning("Unknown decision category for %s: %s", item.get("name"), item.get("category"))

    summary = DecisionModelSummary(
        id=item.get("model_id") or decision_model_id(item.get("name", "")),
        name=item.get("name", ""),
        inputs=inputs,
        outputs=outputs,
        source_cpg=(state.get("cpg_metadata") or {}).get("cpg_id"),
        category=category,
        modifies=modifies or None,
        source_location=location,
        namespace=namespace,
    )
    return summary.model_dump(mode="json", exclude_none=True)


def _bpmn_source_sections(item: dict, state: dict, manifest: list[dict]) -> str:
    """Collect the process section and any explicitly linked source sections."""
    linked_names = {
        str(value).casefold()
        for key in ("linked_recommendations", "linked_decisions", "cross_references")
        for value in (item.get(key, []) or [])
    }
    linked_items = [
        candidate for candidate in manifest
        if any(
            str(candidate.get(field, "")).casefold() in linked_names
            for field in ("id", "model_id", "name", "title")
        )
    ]
    sections = []
    for section in [item.get("section"), *(entry.get("section") for entry in linked_items)]:
        if section and section not in sections:
            sections.append(section)
    markdown = state.get("markdown", "")
    section_map = state.get("section_map", [])
    excerpts = []
    for section in sections:
        text = _extract_section_text(markdown, section_map, str(section))
        if text:
            excerpts.append(f"## CPG section {section}\n{text}")
    return "\n\n".join(excerpts)


def _linked_dmn_summaries(item: dict, dmn_results: list[dict]) -> list[dict]:
    """Return summaries for decisions named by this process manifest item."""
    names = {str(value).casefold() for value in item.get("linked_decisions", []) or []}
    linked = []
    for entry in dmn_results:
        summary = entry.get("decision_model_summary") or {}
        if not summary:
            continue
        if str(summary.get("name", "")).casefold() in names or str(summary.get("id", "")).casefold() in names:
            linked.append(summary)
    return linked


def _linked_recommendations(
    item: dict, manifest: list[dict], extracted: list[dict]
) -> list[dict]:
    """Return extracted recommendation objects selected by linked manifest names."""
    names = {
        str(value).casefold() for value in item.get("linked_recommendations", []) or []
    }
    ids = {
        str(candidate.get("id"))
        for candidate in manifest
        if str(candidate.get("name") or candidate.get("title", "")).casefold() in names
    }
    result = [
        recommendation for recommendation in extracted
        if str(recommendation.get("id", "")) in ids
        or str(recommendation.get("title", "")).casefold() in names
    ]
    if result:
        return result
    return [{"name": value} for value in item.get("linked_recommendations", []) or []]


def _process_figures(item: dict, state: dict) -> list[dict]:
    """Select interpreted flowcharts whose pages overlap the process section."""
    location = _source_location(item)
    page_start = location.page_start if location else None
    page_end = (location.page_end or page_start) if location else None
    if page_start is None:
        section = str(item.get("section", ""))
        matches = [
            value for value in state.get("section_map", [])
            if section and section in value.get("heading", "")
        ]
        if matches:
            page_start = _coerce_page_number(matches[0].get("page_start"))
            page_end = _coerce_page_number(matches[0].get("page_end")) or page_start

    if page_start is None:
        return []
    return [
        figure for figure in state.get("figures", []) or []
        if (page := _coerce_page_number(figure.get("page"))) is not None
        and page_start <= page <= (page_end or page_start)
        and (figure.get("interpretation") or {}).get("mermaid_valid")
    ]


@mlflow.trace(name="dmn_engine_preflight")
def dmn_engine_preflight(state: DMNPipelineState) -> dict:
    """Optionally validate an accepted DMN against the KIE engine."""
    preflight_url = os.environ.get("DMN_PREFLIGHT_URL", "").strip()
    if not preflight_url:
        return {"engine_errors": [], "engine_validation_warnings": []}

    separator = "&" if "?" in preflight_url else "?"
    url = f"{preflight_url}{separator}validate_only=true"
    try:
        response = requests.post(
            url,
            data=state.get("dmn_xml", ""),
            headers={"Content-Type": "application/xml"},
            timeout=15,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        validation_body = isinstance(payload, dict) and any(
            key in payload for key in ("valid", "messages", "errors")
        )
        messages = payload.get("messages", payload.get("errors", [])) if validation_body else []
        errors = []
        warnings = []
        for message in messages:
            if isinstance(message, dict):
                text = message.get("text", str(message))
                (errors if message.get("severity", "ERROR") == "ERROR" else warnings).append(text)
            else:
                errors.append(str(message))

        if response.status_code == 422 or (validation_body and payload.get("valid") is False):
            return {
                "engine_errors": errors,
                "engine_validation_warnings": warnings,
            }

        if response.ok and validation_body:
            return {
                "engine_errors": errors,
                "engine_validation_warnings": warnings,
            }

        if not response.ok or not validation_body:
            logger.warning(
                "DMN preflight unavailable or returned a non-validation body (%s)",
                response.status_code,
            )
            return {
                "engine_errors": [],
                "engine_validation_warnings": [
                    f"DMN preflight unavailable (HTTP {response.status_code}; no validation result)"
                ],
            }
    except requests.RequestException as exc:
        logger.warning("DMN preflight unavailable; continuing without engine validation: %s", exc)
        return {
            "engine_errors": [],
            "engine_validation_warnings": [f"DMN preflight unavailable: {exc}"],
        }

    return {"engine_errors": [], "engine_validation_warnings": []}


def _route_after_dmn_engine(state: DMNPipelineState) -> str:
    if not state.get("engine_errors"):
        return "dmn_complete"
    if state.get("syntax_retry_count", 0) >= MAX_DMN_SYNTAX_RETRIES:
        return "dmn_escalate"
    return "dmn_creator"


# --- BPMN subgraph routing ---

def _route_after_bpmn_validation(state: BPMNPipelineState) -> str:
    errors = [
        finding for finding in state.get("static_findings", [])
        if finding.get("severity") == "ERROR"
    ]
    if not errors:
        return "bpmn_semantic_reviewer"
    if state.get("static_retry_count", 0) >= MAX_BPMN_STATIC_RETRIES:
        return "bpmn_escalate"
    return "bpmn_ir_creator"


def _route_after_bpmn_semantic(state: BPMNPipelineState) -> str:
    if state.get("force_escalate"):
        return "bpmn_escalate"
    if state.get("semantic_discrepancies"):
        if state.get("semantic_retry_count", 0) >= MAX_BPMN_SEMANTIC_RETRIES:
            return "bpmn_escalate"
        return "bpmn_ir_creator"
    return "bpmn_accept"


def _route_after_bpmn_engine(state: BPMNPipelineState) -> str:
    if not state.get("engine_errors"):
        return "bpmn_complete"
    if state.get("static_retry_count", 0) >= MAX_BPMN_STATIC_RETRIES:
        return "bpmn_escalate"
    return "bpmn_ir_creator"


def _source_location(item: dict) -> SourceLocation | None:
    pages = item.get("pages")
    if isinstance(pages, (list, tuple)) and pages and isinstance(pages[0], int):
        page_end = pages[1] if len(pages) > 1 and isinstance(pages[1], int) else None
        return SourceLocation(page_start=pages[0], page_end=page_end)
    start = _coerce_page_number(item.get("page_start"))
    end = _coerce_page_number(item.get("page_end"))
    return SourceLocation(page_start=start, page_end=end) if start is not None else None


def _automatability_rationale(item: dict) -> str:
    tier = item.get("tier_hint")
    if tier == "A":
        timing = ", ".join(
            cue for cue in item.get("timing_cues", []) or []
            if isinstance(cue, str) and cue.strip()
        )
        return (
            "The source describes a repeatable operational sequence with explicit "
            f"timing or trigger{': ' + timing if timing else ''}."
        )
    return (
        "The source states the sequence, while at least one timing or threshold "
        "remains unspecified."
    )


def _bpmn_accept(state: BPMNPipelineState) -> dict:
    """Build the public template summary after static and semantic acceptance."""
    ir = ProcessIR.model_validate(state["process_ir"])
    process = ir.process
    item = state.get("item", {})
    tier = item.get("tier_hint")
    if tier not in {"A", "B"}:
        raise ValueError(f"process tier {tier!r} is not eligible for automation")

    dmn_values = state.get("dmn_summaries", {}) or {}
    if isinstance(dmn_values, list):
        dmn_values = {entry.get("id", ""): entry for entry in dmn_values if isinstance(entry, dict)}
    linked_decisions = {str(name).casefold() for name in item.get("linked_decisions", []) or []}
    linked_dmn_ids = [
        model_id for model_id, summary in dmn_values.items()
        if model_id.casefold() in linked_decisions
        or str(summary.get("name", "")).casefold() in linked_decisions
    ]

    static_findings = state.get("static_findings", []) or []
    warnings = [
        finding.get("message", "") for finding in static_findings
        if finding.get("severity") == "WARNING"
    ]
    warnings.extend(state.get("engine_validation_warnings", []) or [])
    catalog = load_catalog()
    summary = AutomationTemplateSummary(
        id=process.id,
        version=ir_version(ir),
        name=process.name,
        description=process.description,
        source_cpg=(state.get("cpg_metadata") or {}).get("cpg_id", ""),
        section=process.acp.provenance.section or item.get("section"),
        source_location=_source_location(item),
        triggers=process.acp.triggers,
        linked_recommendation_ids=item.get("linked_recommendations", []) or [],
        linked_decision_model_ids=linked_dmn_ids,
        parameters=process.acp.parameters,
        capabilities_used=sorted({
            element.taskName for element in process.flowElements
            if isinstance(element, Task)
        }),
        catalog_version=catalog.version,
        ir_version=ir.ir_version,
        pattern_family=(
            item.get("pattern_family_hint")
            if item.get("pattern_family_hint") in {
                "schedule-and-check", "wait-for-result-then-decide",
                "remind-until-done", "escalate-on-threshold", "do-confirm-repeat",
            }
            else "other"
        ),
        automatability=Automatability(
            tier=tier,
            rationale=_automatability_rationale(item),
        ),
        validation=ValidationRecord(
            status="valid",
            rungs_passed=state.get("static_rungs_passed", []),
            deferred_invariants=state.get("deferred_invariants", []),
            kogito_checked=False,
            warnings=warnings,
        ),
        artifact_id=item.get("id") or process.id,
    )
    write_artifact(
        state.get("output_dir", "output"),
        f"bpmn/{process.id}.template-summary.json",
        summary.model_dump(mode="json", exclude_none=True),
    )
    return {
        "template_summary": summary.model_dump(mode="json", exclude_none=True),
        "escalated": False,
    }


def _bpmn_escalate(state: BPMNPipelineState) -> dict:
    """Record why a process template needs informaticist review."""
    reason = state.get("escalation_reason")
    if not reason:
        if state.get("semantic_discrepancies") or state.get("force_escalate"):
            reason = "semantic-budget-exhausted"
        elif state.get("engine_errors"):
            reason = "engine-validation-failed"
        else:
            reason = "static-budget-exhausted"
    errors = (
        state.get("semantic_discrepancies")
        or state.get("engine_errors")
        or [finding.get("message", str(finding)) for finding in state.get("static_findings", [])]
    )
    logger.warning(
        "BPMN template escalated for %s (%s)",
        state.get("item", {}).get("name", "unknown"), reason,
    )
    return {
        "escalated": True,
        "escalation_reason": reason,
        "escalation_errors": list(errors),
    }


@mlflow.trace(name="bpmn_engine_preflight")
def bpmn_engine_preflight(state: BPMNPipelineState) -> dict:
    """Optionally validate the accepted template with the Kogito endpoint."""
    preflight_url = os.environ.get("BPMN_PREFLIGHT_URL", "").strip()
    if not preflight_url:
        return {"engine_errors": [], "engine_validation_warnings": []}

    try:
        candidate = AutomationTemplate.model_validate({
            "summary": state.get("template_summary", {}),
            "ir": state.get("process_ir", {}),
            "bpmn_xml": state.get("bpmn_xml", ""),
        })
        source_cpg = (state.get("cpg_metadata") or {}).get("cpg_id", "")
        query = urlencode({
            "validate_only": "true",
            **({"source_cpg": source_cpg} if source_cpg else {}),
        })
        separator = "&" if "?" in preflight_url else "?"
        response = requests.post(
            f"{preflight_url}{separator}{query}",
            json=candidate.model_dump(mode="json", exclude_none=True),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        validation_body = isinstance(payload, dict) and any(
            key in payload for key in ("valid", "messages", "errors")
        )
        messages = payload.get("messages", payload.get("errors", [])) if validation_body else []
        if isinstance(messages, (str, dict)):
            messages = [messages]
        errors = []
        warnings = []
        for message in messages or []:
            if isinstance(message, dict):
                text = message.get("text", message.get("message", str(message)))
                (errors if message.get("severity", "ERROR").upper() == "ERROR" else warnings).append(text)
            else:
                errors.append(str(message))
        invalid = payload.get("valid") is False if validation_body else False
        if response.status_code == 422 or invalid:
            if not errors:
                detail = payload.get("detail") if isinstance(payload, dict) else None
                errors.append(str(detail or "Kogito rejected the BPMN template"))
            summary = state.get("template_summary", {})
            validation = summary.setdefault("validation", {})
            validation["status"] = "escalated"
            validation["errors"] = errors
            validation["warnings"] = list(validation.get("warnings", [])) + warnings
            return {
                "template_summary": summary,
                "engine_errors": errors,
                "engine_validation_warnings": warnings,
                "static_retry_count": state.get("static_retry_count", 0) + 1,
            }
        if response.ok and validation_body:
            summary = state.get("template_summary", {})
            validation = summary.setdefault("validation", {})
            validation["kogito_checked"] = not errors
            validation["warnings"] = list(validation.get("warnings", [])) + warnings
            if errors:
                validation["status"] = "escalated"
                validation["errors"] = errors
            return {
                "template_summary": summary,
                "engine_errors": errors,
                "engine_validation_warnings": warnings,
                "static_retry_count": state.get("static_retry_count", 0) + int(bool(errors)),
            }
        logger.warning("BPMN preflight unavailable or returned a non-validation body (HTTP %s)", response.status_code)
        warning = f"BPMN preflight unavailable (HTTP {response.status_code}; no validation result)"
        summary = state.get("template_summary", {})
        validation = summary.setdefault("validation", {})
        validation["warnings"] = list(validation.get("warnings", [])) + [warning]
        return {
            "template_summary": summary,
            "engine_errors": [],
            "engine_validation_warnings": [warning],
        }
    except requests.RequestException as exc:
        logger.warning("BPMN preflight unavailable: %s", exc)
        warning = f"BPMN preflight unavailable: {exc}"
        summary = state.get("template_summary", {})
        validation = summary.setdefault("validation", {})
        validation["warnings"] = list(validation.get("warnings", [])) + [warning]
        return {
            "template_summary": summary,
            "engine_errors": [],
            "engine_validation_warnings": [warning],
        }


def _build_bpmn_subgraph() -> StateGraph:
    """Build the ProcessIR validation, semantic review, and preflight loop."""
    graph = StateGraph(BPMNPipelineState)
    graph.add_node("bpmn_ir_creator", bpmn_ir_creator)
    graph.add_node("bpmn_validator", bpmn_compile_and_validate)
    graph.add_node("bpmn_semantic_reviewer", bpmn_semantic_reviewer)
    graph.add_node("bpmn_accept", _bpmn_accept)
    graph.add_node("bpmn_engine_preflight", bpmn_engine_preflight)
    graph.add_node("bpmn_escalate", _bpmn_escalate)

    graph.add_edge(START, "bpmn_ir_creator")
    graph.add_edge("bpmn_ir_creator", "bpmn_validator")
    graph.add_conditional_edges("bpmn_validator", _route_after_bpmn_validation, {
        "bpmn_semantic_reviewer": "bpmn_semantic_reviewer",
        "bpmn_ir_creator": "bpmn_ir_creator",
        "bpmn_escalate": "bpmn_escalate",
    })
    graph.add_conditional_edges("bpmn_semantic_reviewer", _route_after_bpmn_semantic, {
        "bpmn_accept": "bpmn_accept",
        "bpmn_ir_creator": "bpmn_ir_creator",
        "bpmn_escalate": "bpmn_escalate",
    })
    graph.add_edge("bpmn_accept", "bpmn_engine_preflight")
    graph.add_conditional_edges("bpmn_engine_preflight", _route_after_bpmn_engine, {
        "bpmn_complete": END,
        "bpmn_ir_creator": "bpmn_ir_creator",
        "bpmn_escalate": "bpmn_escalate",
    })
    graph.add_edge("bpmn_escalate", END)
    return graph


# --- Rec subgraph routing ---

def _route_after_rec_schema(state: RecPipelineState) -> str:
    if state.get("schema_errors"):
        if state.get("review_count", 0) >= MAX_REC_REVIEWS:
            return "rec_escalate"
        return "rec_extractor"
    return "rec_semantic_reviewer"


def _route_after_rec_semantic(state: RecPipelineState) -> str:
    if state.get("force_escalate"):
        return "rec_escalate"
    if state.get("semantic_discrepancies"):
        if state.get("review_count", 0) >= MAX_REC_REVIEWS:
            return "rec_escalate"
        return "rec_extractor"
    return "rec_accept"


def _rec_accept(state: RecPipelineState) -> dict:
    logger.info("Recommendations accepted for section")
    return {"escalated": False}


def _rec_escalate(state: RecPipelineState) -> dict:
    logger.warning("Recommendations escalated for human review")
    reason = state.get("escalation_reason")
    if reason:
        errors = (
            state.get("escalation_errors")
            or state.get("semantic_discrepancies")
            or state.get("schema_errors")
            or []
        )
    elif state.get("schema_errors"):
        reason = "schema-budget-exhausted"
        errors = state["schema_errors"]
    elif state.get("semantic_discrepancies"):
        reason = "semantic-budget-exhausted"
        errors = state["semantic_discrepancies"]
    else:
        reason = "unknown"
        errors = []
    return {
        "escalated": True,
        "escalation_reason": reason,
        "escalation_errors": list(errors),
    }


# --- Subgraph builders ---

def _build_dmn_subgraph() -> StateGraph:
    graph = StateGraph(DMNPipelineState)

    graph.add_node("dmn_creator", dmn_creator)
    graph.add_node("dmn_syntax_validator", dmn_syntax_validator)
    graph.add_node("dmn_semantic_reviewer", dmn_semantic_reviewer)
    graph.add_node("dmn_engine_preflight", dmn_engine_preflight)
    graph.add_node("dmn_accept", _dmn_accept)
    graph.add_node("dmn_escalate", _dmn_escalate)

    graph.add_edge(START, "dmn_creator")
    graph.add_edge("dmn_creator", "dmn_syntax_validator")
    graph.add_conditional_edges("dmn_syntax_validator", _route_after_dmn_syntax, {
        "dmn_semantic_reviewer": "dmn_semantic_reviewer",
        "dmn_creator": "dmn_creator",
        "dmn_escalate": "dmn_escalate",
    })
    graph.add_conditional_edges("dmn_semantic_reviewer", _route_after_dmn_semantic, {
        "dmn_accept": "dmn_accept",
        "dmn_creator": "dmn_creator",
        "dmn_escalate": "dmn_escalate",
    })
    graph.add_edge("dmn_accept", "dmn_engine_preflight")
    graph.add_conditional_edges("dmn_engine_preflight", _route_after_dmn_engine, {
        "dmn_complete": END,
        "dmn_creator": "dmn_creator",
        "dmn_escalate": "dmn_escalate",
    })
    graph.add_edge("dmn_escalate", END)

    return graph


def _build_rec_subgraph() -> StateGraph:
    graph = StateGraph(RecPipelineState)

    graph.add_node("rec_extractor", rec_extractor)
    graph.add_node("rec_schema_validator", rec_schema_validator)
    graph.add_node("rec_semantic_reviewer", rec_semantic_reviewer)
    graph.add_node("rec_accept", _rec_accept)
    graph.add_node("rec_escalate", _rec_escalate)

    graph.add_edge(START, "rec_extractor")
    graph.add_edge("rec_extractor", "rec_schema_validator")
    graph.add_conditional_edges("rec_schema_validator", _route_after_rec_schema, {
        "rec_semantic_reviewer": "rec_semantic_reviewer",
        "rec_extractor": "rec_extractor",
        "rec_escalate": "rec_escalate",
    })
    graph.add_conditional_edges("rec_semantic_reviewer", _route_after_rec_semantic, {
        "rec_accept": "rec_accept",
        "rec_extractor": "rec_extractor",
        "rec_escalate": "rec_escalate",
    })
    graph.add_edge("rec_accept", END)
    graph.add_edge("rec_escalate", END)

    return graph


def _process_generation_gate(item: dict, manifest: list[dict]) -> tuple[str, str] | None:
    """Reject non-automatable or unlinked processes before invoking an LLM."""
    tier = item.get("tier_hint")
    if tier not in {"A", "B"}:
        return "tier-not-automatable", f"Process tier {tier!r} is not eligible for automation."
    recommendation_names = {str(value).casefold() for value in item.get("linked_recommendations", []) or []}
    decision_names = {str(value).casefold() for value in item.get("linked_decisions", []) or []}
    has_linked_recommendation = any(
        str(candidate.get(field, "")).casefold() in recommendation_names
        for candidate in manifest if candidate.get("type") == "recommendation"
        for field in ("id", "name", "title")
    )
    has_linked_decision = any(
        str(candidate.get(field, "")).casefold() in decision_names
        for candidate in manifest if candidate.get("type") == "decision"
        for field in ("id", "model_id", "name")
    )
    if not has_linked_recommendation and not has_linked_decision:
        return "unlinked-process", "Process has no linked recommendation or decision item."
    return None


# --- Generation orchestrator ---

def generate_all(state: dict) -> dict:
    """Run DMN, recommendation, and BPMN subgraphs for manifest items.

    Results are stored in state for downstream use (assembly, delivery).
    """
    manifest = state.get("item_manifest", [])
    if not manifest:
        logger.warning("Empty manifest — skipping generation")
        return {}

    markdown = state.get("markdown", "")
    section_map = state.get("section_map", [])
    review_feedback = state.get("review_feedback", "") or ""
    review_comment = state.get("review_comment", "") or ""

    shared = {
        "abbreviations": state.get("abbreviations", {}),
        "litellm_url": state.get("litellm_url", ""),
        "llm_model": state.get("llm_model", ""),
        "llm_api_key": state.get("llm_api_key", ""),
        "output_dir": state.get("output_dir", ""),
    }

    dmn_graph = _build_dmn_subgraph().compile()
    rec_graph = _build_rec_subgraph().compile()

    dmn_results = []
    decisions = [i for i in manifest if i.get("type") == "decision"]
    for item in decisions:
        logger.info("Generating DMN for: %s", item.get("name", "?"))
        source_text = _extract_section_text(markdown, section_map, item.get("section", ""))
        try:
            result = dmn_graph.invoke({
                "item": item,
                "source_pages": source_text,
                "cpg_metadata": state.get("cpg_metadata", {}),
                "review_feedback": review_feedback,
                "review_comment": review_comment,
                **shared,
            })
        except Exception as e:
            # A crash must not make the decision silently disappear from the
            # output and the counts — flag it for human review instead.
            logger.error("DMN generation failed for '%s': %s", item.get("name"), e)
            dmn_results.append({
                "dmn_xml": "",
                "item": item,
                "decision_model_summary": {},
                "escalated": True,
                "escalation_reason": "generation-exception",
                "escalation_errors": [str(e)],
            })
            continue

        if not result.get("dmn_xml"):
            # Same rule for a run that finished but produced no XML.
            logger.error("DMN generation produced no XML for '%s'", item.get("name"))
            dmn_results.append({
                "dmn_xml": "",
                "item": item,
                "decision_model_summary": result.get("decision_model_summary", {}),
                "escalated": True,
                "escalation_reason": result.get("escalation_reason") or "empty-result",
                "escalation_errors": result.get("escalation_errors") or [
                    "Subgraph returned no DMN XML"
                ],
            })
            continue

        entry = {
            "dmn_xml": result["dmn_xml"],
            "item": item,
            "decision_model_summary": {},
        }
        try:
            entry["decision_model_summary"] = _decision_summary(
                item, state, decisions, result["dmn_xml"]
            )
            if ((item.get("page_start") is not None or item.get("page_end") is not None)
                    and "source_location" not in entry["decision_model_summary"]):
                entry.setdefault("validation_warnings", []).append(
                    "summary-build-failed: invalid source page range"
                )
        except Exception as exc:
            logger.error("Decision summary failed for '%s': %s", item.get("name"), exc)
            entry.setdefault("validation_warnings", []).append(
                f"summary-build-failed: {exc}"
            )
        if result.get("syntax_warnings"):
            entry.setdefault("validation_warnings", []).extend(result["syntax_warnings"])
        if result.get("engine_validation_warnings"):
            entry.setdefault("validation_warnings", []).extend(
                result["engine_validation_warnings"]
            )
        if result.get("escalated"):
            entry["escalated"] = True
            entry["escalation_reason"] = result.get("escalation_reason", "")
            entry["escalation_errors"] = (
                result.get("escalation_errors")
                or result.get("syntax_errors")
                or result.get("semantic_discrepancies")
                or []
            )
        dmn_results.append(entry)

    all_recs = []
    recommendation_escalations = []
    seen_sections = set()
    recommendations = [i for i in manifest if i.get("type") == "recommendation"]
    for item in recommendations:
        section = item.get("section", "default")
        if section in seen_sections:
            continue
        seen_sections.add(section)
        section_items = [i for i in recommendations if i.get("section") == section]
        logger.info("Extracting recommendations for section: %s (%d items)", section, len(section_items))
        source_text = _extract_section_text(markdown, section_map, section)
        try:
            result = rec_graph.invoke({
                "items": section_items,
                "source_pages": source_text,
                "grading_definitions": state.get("grading_definitions", ""),
                "review_feedback": review_feedback,
                "review_comment": review_comment,
                **shared,
            })
            recs = result.get("recommendations", [])
            if recs:
                if result.get("escalated"):
                    for rec in recs:
                        if isinstance(rec, dict):
                            rec["escalated"] = True
                            rec["escalation_reason"] = result.get("escalation_reason", "")
                            rec["escalation_errors"] = result.get("escalation_errors", [])
                all_recs.extend(recs)
            elif result.get("escalated"):
                recommendation_escalations.append({
                    "type": "recommendation",
                    "id": section,
                    "name": f"Section: {section}",
                    "section": section,
                    "escalation_reason": result.get("escalation_reason") or "empty-result",
                    "escalation_errors": result.get("escalation_errors") or [
                        "Recommendation loop returned no recommendations"
                    ],
                })
        except Exception as e:
            logger.error("Rec extraction failed for section '%s': %s", section, e)
            recommendation_escalations.append({
                "type": "recommendation",
                "id": section,
                "name": f"Section: {section}",
                "section": section,
                "escalation_reason": "generation-exception",
                "escalation_errors": [str(e)],
            })

    bpmn_results = []
    process_items = [item for item in manifest if item.get("type") == "process"]
    bpmn_graph = None
    for item in process_items:
        gate = _process_generation_gate(item, manifest)
        if gate:
            reason, message = gate
            bpmn_results.append({
                "bpmn_xml": "",
                "process_ir": {},
                "template_summary": {},
                "item": item,
                "escalated": True,
                "escalation_reason": reason,
                "escalation_errors": [message],
            })
            continue
        if bpmn_graph is None:
            bpmn_graph = _build_bpmn_subgraph().compile()
        logger.info("Generating BPMN template for: %s", item.get("name", "?"))
        try:
            result = bpmn_graph.invoke({
                "item": item,
                "source_pages": _bpmn_source_sections(item, state, manifest),
                "figures": _process_figures(item, state),
                "dmn_summaries": _linked_dmn_summaries(item, dmn_results),
                "linked_recs": _linked_recommendations(item, manifest, all_recs),
                "human_feedback": "\n".join(value for value in (
                    state.get("human_feedback", ""), review_feedback, review_comment,
                ) if value),
                "cpg_metadata": state.get("cpg_metadata", {}),
                **shared,
            })
        except Exception as exc:
            logger.error("BPMN generation failed for '%s': %s", item.get("name"), exc)
            bpmn_results.append({
                "bpmn_xml": "",
                "process_ir": {},
                "template_summary": {},
                "item": item,
                "escalated": True,
                "escalation_reason": "generation-exception",
                "escalation_errors": [str(exc)],
            })
            continue

        entry = {
            "bpmn_xml": result.get("bpmn_xml", ""),
            "process_ir": result.get("process_ir", {}),
            "template_summary": result.get("template_summary", {}),
            "item": item,
            "static_findings": result.get("static_findings", []),
            "deferred_invariants": result.get("deferred_invariants", []),
        }
        if result.get("escalated") or not entry["bpmn_xml"]:
            entry["escalated"] = True
            entry["escalation_reason"] = result.get("escalation_reason") or "empty-result"
            entry["escalation_errors"] = result.get("escalation_errors") or [
                finding.get("message", str(finding))
                for finding in entry["static_findings"]
            ]
        bpmn_results.append(entry)

    return {
        "dmn_results": dmn_results,
        "recommendation_results": all_recs,
        "recommendation_escalations": recommendation_escalations,
        "bpmn_results": bpmn_results,
    }
