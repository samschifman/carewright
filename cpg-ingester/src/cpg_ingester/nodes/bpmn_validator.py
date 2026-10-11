"""Compile Process IR and run the shared template validation ladder."""

import logging

import mlflow
from pydantic import ValidationError

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.decisions import DecisionModelSummary
from cpg_ingester.output import write_artifact

logger = logging.getLogger(__name__)


def _decision_summaries(values) -> dict[str, DecisionModelSummary]:
    """Validate linked DMN summaries and key them by model id."""
    if isinstance(values, list):
        values = {value.get("id", ""): value for value in values if isinstance(value, dict)}
    result = {}
    for model_id, value in (values or {}).items():
        if isinstance(value, DecisionModelSummary):
            result[model_id] = value
        elif isinstance(value, dict):
            result[model_id] = DecisionModelSummary.model_validate(value)
    return result


@mlflow.trace(name="bpmn_compile_and_validate")
def bpmn_compile_and_validate(state: dict) -> dict:
    """Run L0–L5b for a template, compile it, and retain findings for repair."""
    raw_ir = state.get("process_ir", {})
    output_dir = state.get("output_dir", "output")
    findings = []
    deferred = []
    rungs_passed = []
    bpmn_xml = ""

    try:
        process_ir = ProcessIR.model_validate(raw_ir)
        dmn_summaries = _decision_summaries(state.get("dmn_summaries", {}))
        catalog = load_catalog()
        ladder = run_static_ladder(
            process_ir,
            catalog=catalog,
            stage="template",
            section_text=state.get("source_pages", ""),
            dmn_summaries=dmn_summaries,
        )
        findings = [finding.__dict__.copy() for finding in ladder.findings]
        deferred = list(ladder.deferred)
        rungs_passed = list(ladder.rungs_passed)
        if ladder.ok:
            namespaces = {
                model_id: summary.namespace
                for model_id, summary in dmn_summaries.items()
                if summary.namespace
            }
            bpmn_xml = compile(process_ir, catalog=catalog, dmn_namespaces=namespaces)
        normalized_ir = process_ir.model_dump(mode="json", exclude_none=True)
    except (ValidationError, TypeError, ValueError, KeyError) as exc:
        logger.warning("ProcessIR validation failed: %s", exc)
        findings = [{
            "rung": "L0",
            "severity": "ERROR",
            "code": "ir-schema",
            "message": str(exc),
            "element_id": None,
        }]
        normalized_ir = raw_ir if isinstance(raw_ir, dict) else {}

    has_errors = any(finding.get("severity") == "ERROR" for finding in findings)
    retry_count = state.get("static_retry_count", 0) + int(has_errors)
    if bpmn_xml:
        write_artifact(output_dir, "bpmn/process.bpmn", bpmn_xml)

    return {
        "process_ir": normalized_ir,
        "bpmn_xml": bpmn_xml,
        "static_findings": findings,
        "static_rungs_passed": rungs_passed,
        "deferred_invariants": deferred,
        "static_retry_count": retry_count,
    }
