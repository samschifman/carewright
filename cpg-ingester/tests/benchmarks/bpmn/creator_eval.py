"""Run and measure the production BPMN creator/reviewer loop."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import mlflow
from lxml import etree

from cpg_contracts import DecisionModelSummary, DecisionVariable, get_llm
from cpg_ingester import generation
from cpg_ingester.generation import (
    MAX_BPMN_SEMANTIC_RETRIES,
    MAX_BPMN_STATIC_RETRIES,
    _bpmn_accept,
    _bpmn_escalate,
    _extract_section_text,
    _route_after_bpmn_engine,
    _route_after_bpmn_semantic,
    _route_after_bpmn_validation,
    bpmn_engine_preflight,
)
from cpg_ingester.nodes import bpmn_ir_creator as creator_module
from cpg_ingester.nodes import bpmn_semantic_reviewer as reviewer_module
from cpg_ingester.nodes import bpmn_validator as validator_module

from ir_diff import diff_process_ir

logger = logging.getLogger(__name__)
RUNGS = ("L0", "L1", "L2", "L3", "L4", "L5a", "L5b")

_DMN_GOLDENS = {
    "treatment-recommendation": "data/golden/treatment-recommendation.dmn",
    "monitoring-plan": "data/golden/monitoring-plan.dmn",
}


class _UsageTrackingModel:
    """Small transparent proxy that accumulates provider-reported token use."""

    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def invoke(self, *args, **kwargs):
        response = self._wrapped.invoke(*args, **kwargs)
        usage = getattr(response, "usage_metadata", None) or {}
        if not usage:
            usage = getattr(response, "response_metadata", {}).get("token_usage", {})
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens) or 0
        self.usage["input_tokens"] += int(input_tokens)
        self.usage["output_tokens"] += int(output_tokens)
        self.usage["total_tokens"] += int(total_tokens)
        return response


def _source_pages(markdown: str, section_names: list[str]) -> str:
    section_map = [{"heading": name} for name in section_names]
    return "\n\n".join(
        text for name in section_names
        if (text := _extract_section_text(markdown, section_map, name))
    )


def _dmn_summaries(template: dict, ingester_root: Path) -> dict[str, dict]:
    result = {}
    for model_id in template.get("linked_decisions", []):
        relpath = _DMN_GOLDENS.get(model_id)
        if not relpath:
            continue
        root = etree.parse(str(ingester_root / relpath)).getroot()
        inputs = []
        outputs = []
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            local = etree.QName(element).localname
            if local == "inputData":
                variable = next(
                    (child for child in element if etree.QName(child).localname == "variable"),
                    None,
                )
                if variable is not None:
                    inputs.append(DecisionVariable(
                        name=variable.get("name", ""),
                        type=variable.get("typeRef", "string"),
                    ))
            elif local == "output":
                outputs.append(DecisionVariable(
                    name=element.get("name", ""),
                    type=element.get("typeRef", "string"),
                ))
        result[model_id] = DecisionModelSummary(
            id=model_id,
            name=model_id.replace("-", " ").title(),
            inputs=inputs,
            outputs=outputs,
            namespace=root.get("namespace"),
        ).model_dump(mode="json", exclude_none=True)
    return result


def _item(template: dict) -> dict:
    return {
        "id": template["id"],
        "template_id": template["id"],
        "type": "process",
        "name": template["name"],
        "description": template.get("description", ""),
        "section": template.get("section", ""),
        "tier_hint": template.get("tier_hint"),
        "pattern_family_hint": template.get("pattern_family_hint"),
        "trigger_hint": template.get("trigger_hint"),
        "actors": template.get("actors", []),
        "steps": template.get("steps", []),
        "timing_cues": template.get("timing_cues", []),
        "linked_recommendations": template.get("linked_recommendations", []),
        "linked_decisions": template.get("linked_decisions", []),
    }


def _record_first_attempt(record: dict, state: dict) -> None:
    findings = state.get("static_findings", []) or []
    passed = set(state.get("static_rungs_passed", []) or [])
    failed_rungs = {
        finding.get("rung") for finding in findings
        if finding.get("severity") == "ERROR"
    }
    record["first_attempt_rungs"] = {
        rung: (True if rung in passed else False if rung in failed_rungs else None)
        for rung in RUNGS
    }


def _drive_template(template: dict, markdown: str, llm_config: dict,
                    output_dir: Path, ingester_root: Path) -> dict:
    """Drive production node functions with production routers for one template."""
    item = _item(template)
    source = _source_pages(markdown, template.get("source_sections", []))
    token_tracker = _UsageTrackingModel(get_llm({
        "litellm_url": llm_config["litellm_url"],
        "llm_model": llm_config["llm_model"],
        "llm_api_key": llm_config["llm_api_key"],
    }))
    creator_get_llm = creator_module.get_llm
    reviewer_get_llm = reviewer_module.get_llm
    creator_module.get_llm = lambda _state: token_tracker
    reviewer_module.get_llm = lambda _state: token_tracker
    state: dict[str, Any] = {
        "item": item,
        "source_pages": source,
        "dmn_summaries": _dmn_summaries(template, ingester_root),
        "linked_recs": [
            {"name": name, "description": name}
            for name in template.get("linked_recommendations", [])
        ],
        "figures": [],
        "abbreviations": {},
        "human_feedback": "",
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "output_dir": str(output_dir),
        "litellm_url": llm_config["litellm_url"],
        "llm_model": llm_config["llm_model"],
        "llm_api_key": llm_config["llm_api_key"],
        "static_retry_count": 0,
        "semantic_retry_count": 0,
    }
    record = {
        "id": item["id"],
        "name": item["name"],
        "source_text_present": bool(source.strip()),
        "first_attempt_rungs": {rung: None for rung in RUNGS},
        "creator_attempts": 0,
        "accepted": False,
        "escalated": False,
        "escalation_reason": None,
        "preflight": "not-reached",
    }
    started = time.perf_counter()
    try:
        result = None
        for _ in range(MAX_BPMN_STATIC_RETRIES + MAX_BPMN_SEMANTIC_RETRIES + 3):
            state.update(creator_module.bpmn_ir_creator(state))
            record["creator_attempts"] += 1
            state.update(validator_module.bpmn_compile_and_validate(state))
            if record["creator_attempts"] == 1:
                _record_first_attempt(record, state)

            route = _route_after_bpmn_validation(state)
            if route == "bpmn_ir_creator":
                continue
            if route == "bpmn_escalate":
                result = {**state, **_bpmn_escalate(state)}
                break

            state.update(reviewer_module.bpmn_semantic_reviewer(state))
            route = _route_after_bpmn_semantic(state)
            if route == "bpmn_ir_creator":
                continue
            if route == "bpmn_escalate":
                result = {**state, **_bpmn_escalate(state)}
                break

            state.update(_bpmn_accept(state))
            state.update(bpmn_engine_preflight(state))
            route = _route_after_bpmn_engine(state)
            if route == "bpmn_complete":
                result = state
                break
            if route == "bpmn_escalate":
                result = {**state, **_bpmn_escalate(state)}
                break
        if result is None:
            result = {**state, **_bpmn_escalate(state)}
        record["accepted"] = not bool(result.get("escalated")) and bool(result.get("bpmn_xml"))
        record["escalated"] = bool(result.get("escalated"))
        record["escalation_reason"] = result.get("escalation_reason")
        record["static_rungs_passed"] = result.get("static_rungs_passed", [])
        record["static_findings"] = result.get("static_findings", [])
        record["semantic_discrepancies"] = result.get("semantic_discrepancies", [])
        record["engine_errors"] = result.get("engine_errors", [])
        record["engine_warnings"] = result.get("engine_validation_warnings", [])
        record["preflight"] = (
            "checked" if result.get("template_summary", {}).get("validation", {}).get("kogito_checked")
            else "skipped-or-unavailable"
        )
        record["generated_ir"] = result.get("process_ir", {})
        record["golden_diff"] = diff_process_ir(
            json_read(ingester_root / template["golden_ir"]),
            record["generated_ir"] if record["generated_ir"] else {},
        )
        record["template_summary"] = result.get("template_summary", {})
        record["bpmn_xml"] = result.get("bpmn_xml", "")
        record["tokens"] = token_tracker.usage
    finally:
        creator_module.get_llm = creator_get_llm
        reviewer_module.get_llm = reviewer_get_llm
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return record


def json_read(path: Path) -> dict:
    import json

    return json.loads(path.read_text())


@mlflow.trace(name="bpmn_benchmark_creator_suite")
def run_creator_suite(corpus: dict, markdown: str, llm_config: dict,
                      output_dir: str | Path, ingester_root: Path) -> dict:
    """Evaluate all corpus templates using the real generation subgraph."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    records = []
    for template in corpus.get("templates", []):
        logger.info("Benchmarking BPMN template %s", template["id"])
        records.append(_drive_template(
            template,
            markdown,
            llm_config,
            output_path / template["id"],
            ingester_root,
        ))
    valid = [record for record in records if record["accepted"]]
    rung_metrics = {}
    for rung in RUNGS:
        reached = [
            record["first_attempt_rungs"][rung]
            for record in records
            if record["first_attempt_rungs"][rung] is not None
        ]
        rung_metrics[rung] = {
            "pass_rate": round(sum(reached) / len(reached), 4) if reached else None,
            "reached": len(reached),
        }
    return {
        "templates": records,
        "summary": {
            "template_count": len(records),
            "first_attempt_validity_rate": round(
                sum(all(value is True for value in item["first_attempt_rungs"].values())
                    for item in records) / len(records), 4
            ) if records else 0.0,
            "first_attempt_rungs": rung_metrics,
            "mean_attempts": round(
                sum(item["creator_attempts"] for item in records) / len(records), 4
            ) if records else 0.0,
            "escalation_rate": round(
                sum(item["escalated"] for item in records) / len(records), 4
            ) if records else 0.0,
            "accepted_rate": round(len(valid) / len(records), 4) if records else 0.0,
            "mean_latency_seconds": round(
                sum(item["elapsed_seconds"] for item in records) / len(records), 3
            ) if records else 0.0,
            "total_tokens": sum(
                item.get("tokens", {}).get("total_tokens", 0) for item in records
            ),
        },
    }
