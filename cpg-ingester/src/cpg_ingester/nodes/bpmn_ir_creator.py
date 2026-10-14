"""Create and repair a Process IR template for an identified CPG process."""

import json
import logging
import re
import time

import mlflow
from pydantic import ValidationError

from cpg_contracts import content_to_text, get_llm
from cpg_ingester.nodes.structure_analyzer import _parse_llm_json
from cpg_ingester.output import write_artifact
from cpg_ingester.llm_responses import persist_llm_response
from cpg_ingester.prompts.bpmn_ir_creator import (
    build_bpmn_ir_creator_system,
    build_bpmn_ir_creator_user,
)
from cpg_ingester.reference.bpmn_error_patterns import format_bpmn_error_pattern_hints

logger = logging.getLogger(__name__)


def _normalize_template_ir(raw_ir: dict, state: dict) -> dict:
    """Apply system-owned template fields and deterministic graph repairs."""
    process = raw_ir.get("process")
    if not isinstance(process, dict):
        return raw_ir

    item = state.get("item", {})
    if item.get("template_id"):
        process["id"] = item["template_id"]
    if item.get("name"):
        process["name"] = item["name"]
    process["kind"] = "template"

    acp = process.get("acp")
    if not isinstance(acp, dict):
        return raw_ir
    parameters = acp.setdefault("parameters", [])
    if not isinstance(parameters, list):
        return raw_ir
    if not any(
        isinstance(parameter, dict) and parameter.get("name") == "max_duration"
        for parameter in parameters
    ):
        parameters.append({
            "name": "max_duration",
            "type": "duration",
            "required": True,
            "description": "Maximum duration of the monitoring process, bound from the plan.",
            "reserved": True,
        })

    elements = process.get("flowElements")
    if not isinstance(elements, list):
        return raw_ir
    if not any(
        element.get("type") == "boundaryEvent" and element.get("attachedToRef") == "main"
        for element in elements
        if isinstance(element, dict)
    ):
        task_ids = [
            element["id"] for element in elements
            if isinstance(element, dict)
            and element.get("type") in {"task", "userTask", "businessRuleTask"}
            and element.get("id")
        ]
        elements.append({
            "id": "period_elapsed",
            "name": "Monitoring period elapsed",
            "type": "boundaryEvent",
            "attachedToRef": "main",
            "cancelActivity": True,
            "timerEventDefinition": {"timeDuration": {"param": "max_duration"}},
            "acp": {
                "provenance": {
                    "kind": "structural",
                    "derivation_rule": "plan-bound",
                    "supports": task_ids,
                }
            },
        })

    # Make every non-gateway merge explicit before the ProcessIR validator
    # runs. These are IR elements, so allocate around authored ID collisions.
    nodes = {
        element.get("id"): element
        for element in elements
        if isinstance(element, dict)
        and element.get("type") != "sequenceFlow"
        and isinstance(element.get("id"), str)
    }
    incoming: dict[str, list[dict]] = {}
    existing_ids = {
        element.get("id")
        for element in elements
        if isinstance(element, dict) and isinstance(element.get("id"), str)
    }
    for element in elements:
        if isinstance(element, dict) and element.get("type") == "sequenceFlow":
            target = element.get("targetRef")
            if isinstance(target, str):
                incoming.setdefault(target, []).append(element)

    joins: list[dict] = []
    for node_id, node in sorted(nodes.items()):
        flows = sorted(incoming.get(node_id, []), key=lambda flow: str(flow.get("id", "")))
        if len(flows) <= 1 or (
            node.get("type") == "exclusiveGateway"
            and node.get("gatewayDirection") == "Converging"
        ):
            continue

        gateway_base = f"{node_id}_join"
        gateway_id = gateway_base
        suffix = 2
        while gateway_id in existing_ids:
            gateway_id = f"{gateway_base}_{suffix}"
            suffix += 1

        flow_base = f"{node_id}_join_in"
        flow_id = flow_base
        suffix = 2
        while flow_id in existing_ids or flow_id == gateway_id:
            flow_id = f"{flow_base}_{suffix}"
            suffix += 1

        supports = sorted(flow["sourceRef"] for flow in flows if isinstance(flow.get("sourceRef"), str))
        for flow in flows:
            flow["targetRef"] = gateway_id
        provenance = {
            "kind": "structural",
            "derivation_rule": "exclusive-join",
            "supports": supports,
        }
        joins.extend([
            {
                "id": gateway_id,
                "name": f"Join before {node.get('name') or node_id}",
                "type": "exclusiveGateway",
                "gatewayDirection": "Converging",
                "acp": {"provenance": provenance},
            },
            {
                "id": flow_id,
                "name": "",
                "type": "sequenceFlow",
                "sourceRef": gateway_id,
                "targetRef": node_id,
            },
        ])
        existing_ids.update((gateway_id, flow_id))

    elements.extend(joins)
    return raw_ir


def _repair_feedback(state: dict) -> str:
    """Format prior mechanical and semantic findings for the next attempt."""
    findings = state.get("static_findings", []) or []
    engine_errors = state.get("engine_errors", []) or []
    discrepancies = state.get("semantic_discrepancies", []) or []
    if not findings and not engine_errors and not discrepancies:
        return ""

    parts = ["Previous ProcessIR attempt needs correction. Return a complete IR."]
    if state.get("process_ir"):
        parts.append(
            "## Previous ProcessIR\n"
            + json.dumps(state["process_ir"], indent=2, ensure_ascii=False, default=str)
        )
    if findings:
        parts.append("## Validation findings\n" + "\n".join(
            f"- {item.get('rung', '?')} {item.get('code', '')}: {item.get('message', item)}"
            for item in findings
        ))
    if engine_errors:
        parts.append("## Kogito validation findings\n" + "\n".join(f"- {item}" for item in engine_errors))
    if discrepancies:
        parts.append("## Critical semantic discrepancies\n" + "\n".join(f"- {item}" for item in discrepancies))
    hints = format_bpmn_error_pattern_hints(findings + engine_errors)
    if hints:
        parts.append("## Known error patterns\n" + hints)
    return "\n\n".join(parts)


def _format_abbreviations(abbreviations: dict) -> str:
    if not abbreviations:
        return "(none)"
    return "\n".join(f"- {name}: {value}" for name, value in abbreviations.items())


def _format_mermaid(figures: list[dict]) -> str:
    diagrams = []
    for figure in figures or []:
        interpretation = figure.get("interpretation") or {}
        mermaid = interpretation.get("mermaid")
        if interpretation.get("mermaid_valid") and mermaid:
            diagrams.append(
                f"Figure {figure.get('id', '?')} (page {figure.get('page', '?')}):\n{mermaid}"
            )
    return "\n\n".join(diagrams) if diagrams else "(none)"


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")[:50] or "process"


@mlflow.trace(name="bpmn_ir_creator")
def bpmn_ir_creator(state: dict) -> dict:
    """Generate ProcessIR JSON and prepare it for deterministic validation."""
    item = state.get("item", {})
    name = item.get("name", "unknown process")
    output_dir = state.get("output_dir", "output")
    repair_feedback = _repair_feedback(state)
    human_feedback = state.get("human_feedback", "") or ""
    dmn_summaries = state.get("dmn_summaries", {}) or {}
    if isinstance(dmn_summaries, dict):
        dmn_summaries = list(dmn_summaries.values())

    linked_recs = state.get("linked_recs", []) or []
    if not linked_recs:
        linked_recs = item.get("linked_recommendations", []) or []

    llm = get_llm(state)
    messages = [
        {"role": "system", "content": build_bpmn_ir_creator_system()},
        {"role": "user", "content": build_bpmn_ir_creator_user(
            item=item,
            source_pages=state.get("source_pages", ""),
            linked_dmn_summaries=dmn_summaries,
            linked_recs=linked_recs,
            mermaid_figures=_format_mermaid(state.get("figures", [])),
            abbreviations=_format_abbreviations(state.get("abbreviations", {})),
            human_feedback=human_feedback,
            repair_feedback=repair_feedback,
        )},
    ]

    logger.info("Creating ProcessIR for %s", name)
    started = time.time()
    response = llm.invoke(messages)
    logger.info("ProcessIR creator responded in %.1fs", time.time() - started)
    persist_llm_response(
        output_dir,
        process_name=name,
        role="creator",
        content=content_to_text(response.content),
    )

    join_normalizer_fired = False
    try:
        raw_ir = _parse_llm_json(content_to_text(response.content))
        if not isinstance(raw_ir, dict):
            raise ValueError("ProcessIR response must be a JSON object")
        original_process = raw_ir.get("process")
        original_elements = (
            original_process.get("flowElements", [])
            if isinstance(original_process, dict)
            else []
        )
        original_ids = {
            element.get("id")
            for element in original_elements
            if isinstance(element, dict)
        }
        raw_ir = _normalize_template_ir(raw_ir, state)
        normalized_process = raw_ir.get("process")
        normalized_elements = (
            normalized_process.get("flowElements", [])
            if isinstance(normalized_process, dict)
            else []
        )
        join_normalizer_fired = any(
            element.get("id") not in original_ids
            and element.get("type") == "exclusiveGateway"
            and isinstance(element.get("acp"), dict)
            and isinstance(element["acp"].get("provenance"), dict)
            and element["acp"]["provenance"].get("derivation_rule") == "exclusive-join"
            for element in normalized_elements
            if isinstance(element, dict)
        )
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("ProcessIR creator returned invalid JSON for %s: %s", name, exc)
        raw_ir = {}

    write_artifact(output_dir, f"bpmn/{_safe_name(name)}.ir.json", raw_ir)
    return {
        "process_ir": raw_ir,
        "join_normalizer_fired": join_normalizer_fired,
        "static_findings": [],
        "deferred_invariants": [],
        "static_rungs_passed": [],
        "semantic_discrepancies": [],
        "engine_errors": [],
        "engine_validation_warnings": [],
        "force_escalate": False,
    }
