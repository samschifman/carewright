#!/usr/bin/env python3
"""Compile the hand-authored hypertension automation IR goldens."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lxml import etree

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import BusinessRuleTask, ProcessIR, Task, ir_version
from cpg_contracts.automation.templates import (
    AutomationTemplate,
    AutomationTemplateSummary,
    Automatability,
    ValidationRecord,
)
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.decisions import DecisionModelSummary, DecisionVariable


AUTOMATION_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = AUTOMATION_DIR.parent
REPO_ROOT = AUTOMATION_DIR.parents[3]
CPG_PATH = REPO_ROOT / "cpg-ingester" / "data" / "synthetic-hypertension-cpg-v2.md"
DMN_DIR = GOLDEN_DIR
DMN_NS = "https://www.omg.org/spec/DMN/20211108/MODEL/"

PATTERN_FAMILY = {
    "home-bp-monitoring": "remind-until-done",
    "acei-lab-follow-up": "wait-for-result-then-decide",
    "follow-up-visit": "schedule-and-check",
    "lifestyle-reassessment": "schedule-and-check",
}
AUTOMATABILITY_RATIONALE = {
    "home-bp-monitoring": "The guideline gives a repeatable monitoring cadence, thresholds, reminder limit, and escalation path.",
    "acei-lab-follow-up": "The monitoring decision and timing come from the linked decision model; the remaining steps are result lookup and notification.",
    "follow-up-visit": "The treatment decision routes medication patients with a two- or four-week interval to visit scheduling and an immediate order check.",
    "lifestyle-reassessment": "The treatment decision supplies an eight- or twelve-week interval; the process sends lifestyle education, requests the visit, then has a clinician review readings.",
}
DMN_TYPE_MAP = {"number": "number", "string": "string", "boolean": "boolean"}


def load_dmn_summaries() -> dict[str, DecisionModelSummary]:
    """Read model ids, namespaces, and variable names from golden DMN files."""
    namespace = {"dmn": DMN_NS}
    summaries: dict[str, DecisionModelSummary] = {}
    for path in sorted(DMN_DIR.glob("*.dmn")):
        document = etree.parse(str(path))
        root = document.getroot()
        inputs = [
            DecisionVariable(
                name=variable.get("name"),
                type=DMN_TYPE_MAP.get(variable.get("typeRef"), variable.get("typeRef", "Any")),
            )
            for variable in document.xpath("//dmn:inputData/dmn:variable", namespaces=namespace)
        ]
        outputs = [
            DecisionVariable(
                name=variable.get("name"),
                type=DMN_TYPE_MAP.get(variable.get("typeRef"), variable.get("typeRef", "Any")),
            )
            for variable in document.xpath(
                "//dmn:decision/dmn:decisionTable/dmn:output", namespaces=namespace
            )
        ]
        model_id = root.get("id")
        if model_id is None:
            raise ValueError(f"DMN file {path} has no definitions id")
        summaries[model_id] = DecisionModelSummary(
            id=model_id,
            name=root.get("name") or model_id,
            inputs=inputs,
            outputs=outputs,
            namespace=root.get("namespace"),
        )
    return summaries


def _linked_decision_ids(ir: ProcessIR) -> list[str]:
    process = ir.process
    model_ids = {
        element.dmnModel
        for element in process.flowElements
        if isinstance(element, BusinessRuleTask)
    }
    model_ids.update(
        trigger.model_id
        for trigger in process.acp.triggers
        if trigger.kind == "dmn-output" and trigger.model_id is not None
    )
    return sorted(model_ids)


def _build_template(ir: ProcessIR, *, xml: str, result, catalog) -> AutomationTemplate:
    process = ir.process
    warning_messages = [
        f"{finding.rung}/{finding.code}: {finding.message}"
        for finding in result.warnings()
    ]
    validation = ValidationRecord(
        status="valid",
        rungs_passed=result.rungs_passed,
        deferred_invariants=result.deferred,
        kogito_checked=False,
        warnings=warning_messages,
        errors=[],
    )
    summary = AutomationTemplateSummary(
        id=process.id,
        version=ir_version(ir),
        name=process.name,
        description=process.description,
        source_cpg=process.acp.provenance.source_cpg,
        section=process.acp.provenance.section,
        source_location=process.acp.provenance.source_location,
        triggers=process.acp.triggers,
        linked_recommendation_ids=["<placeholder-rec-id>"],
        linked_decision_model_ids=_linked_decision_ids(ir),
        parameters=process.acp.parameters,
        capabilities_used=sorted(
            {
                element.taskName
                for element in process.flowElements
                if isinstance(element, Task)
            }
        ),
        catalog_version=catalog.version,
        ir_version=ir.ir_version,
        pattern_family=PATTERN_FAMILY[process.id],
        automatability=Automatability(
            tier="A",
            rationale=AUTOMATABILITY_RATIONALE[process.id],
        ),
        validation=validation,
        artifact_id=f"golden-automation-{process.id}",
    )
    return AutomationTemplate(summary=summary, ir=ir, bpmn_xml=xml)


def render_outputs() -> dict[str, str]:
    """Return deterministic BPMN and template JSON for every authored IR."""
    catalog = load_catalog()
    dmn_summaries = load_dmn_summaries()
    dmn_namespaces = {
        model_id: summary.namespace
        for model_id, summary in dmn_summaries.items()
        if summary.namespace is not None
    }
    section_text = CPG_PATH.read_text(encoding="utf-8")
    outputs: dict[str, str] = {}

    for ir_path in sorted(AUTOMATION_DIR.glob("*.ir.json")):
        ir = ProcessIR.model_validate_json(ir_path.read_text(encoding="utf-8"))
        result = run_static_ladder(
            ir,
            catalog=catalog,
            stage="template",
            section_text=section_text,
            dmn_summaries=dmn_summaries,
        )
        if not result.ok:
            details = "; ".join(
                f"{finding.rung}/{finding.code}: {finding.message}"
                for finding in result.findings
            )
            raise ValueError(f"{ir_path.name} failed the validation ladder: {details}")
        if "L5b" not in result.rungs_passed or not result.deferred:
            raise ValueError(f"{ir_path.name} lacks text grounding or a deferred invariant")

        xml = compile(ir, catalog=catalog, dmn_namespaces=dmn_namespaces)
        template = _build_template(ir, xml=xml, result=result, catalog=catalog)
        outputs[f"{ir.process.id}.bpmn"] = xml
        outputs[f"{ir.process.id}.template.json"] = json.dumps(
            template.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ) + "\n"
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed generated BPMN or template JSON is stale",
    )
    args = parser.parse_args()
    outputs = render_outputs()
    if args.check:
        stale = [
            filename
            for filename, content in outputs.items()
            if not (AUTOMATION_DIR / filename).is_file()
            or (AUTOMATION_DIR / filename).read_text(encoding="utf-8") != content
        ]
        if stale:
            raise SystemExit(f"stale generated golden(s): {', '.join(stale)}")
        print(f"{len(outputs) // 2} automation goldens are up to date")
        return
    for filename, content in outputs.items():
        (AUTOMATION_DIR / filename).write_text(content, encoding="utf-8")
        print(f"wrote {filename}")


if __name__ == "__main__":
    main()
