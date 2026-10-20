"""Assembly Agent — deterministic cross-reference resolution and integrity checks."""

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

import mlflow
import requests

from cpg_contracts import CONTRACT_VERSION
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR, SourceRef, Task, ir_version
from cpg_contracts.automation.templates import AutomationTemplate
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.decisions import DecisionModelSummary
from cpg_ingester.artifact_id import make_artifact_id
from cpg_ingester.output import write_artifact

logger = logging.getLogger(__name__)
_L7_ESCALATION_REASONS = frozenset({
    "semantic-budget-exhausted",
    "no-source-text",
    "reviewer-unparseable",
})


def _resolve_cross_references(recommendations: list[dict], dmn_results: list[dict]) -> list[dict]:
    """Resolve cross-references and remove any pointing to missing items."""
    all_ids = set()
    for rec in recommendations:
        if rec.get("id"):
            all_ids.add(rec["id"])
    for dmn in dmn_results:
        summary = dmn.get("decision_model_summary", {})
        if summary.get("id"):
            all_ids.add(summary["id"])

    for rec in recommendations:
        refs = rec.get("cross_references", []) or []
        resolved = []
        for ref in refs:
            if isinstance(ref, dict):
                target = ref.get("target_id", "")
                if target in all_ids:
                    resolved.append(ref)
                else:
                    logger.warning("Removed cross-ref to missing target %s from rec %s", target[:8], rec.get("id", "?")[:8])
            elif isinstance(ref, str):
                if ref in all_ids:
                    resolved.append({"target_id": ref, "relationship": "related"})
                else:
                    logger.warning("Removed cross-ref to missing target %s from rec %s", ref[:8], rec.get("id", "?")[:8])
        rec["cross_references"] = resolved if resolved else None

    return recommendations


def _collect_escalations(
    dmn_results: list[dict],
    recommendations: list[dict],
    recommendation_escalations: list[dict] | None = None,
    bpmn_results: list[dict] | None = None,
) -> list[dict]:
    """Gather every result flagged for human review, with its reason and errors."""
    escalated = []
    for dmn in dmn_results:
        if dmn.get("escalated"):
            escalated.append({
                "type": "decision",
                "name": dmn.get("item", {}).get("name", "?"),
                "escalation_reason": dmn.get("escalation_reason", ""),
                "escalation_errors": dmn.get("escalation_errors", []),
            })
    for rec in recommendations:
        if isinstance(rec, dict) and rec.get("escalated"):
            escalated.append({
                "type": "recommendation",
                "id": rec.get("id", "?"),
                "name": rec.get("title") or rec.get("id", "?"),
                "escalation_reason": rec.get("escalation_reason", ""),
                "escalation_errors": rec.get("escalation_errors", []),
            })
    for template in bpmn_results or []:
        if template.get("escalated"):
            item = template.get("item", {})
            escalated.append({
                "type": "automation",
                "id": item.get("id") or template.get("template_summary", {}).get("id", "?"),
                "name": item.get("name") or template.get("template_summary", {}).get("name", "?"),
                "section": item.get("section", ""),
                "escalation_reason": template.get("escalation_reason", ""),
                "escalation_errors": template.get("escalation_errors", []),
            })
    escalated.extend(recommendation_escalations or [])
    return escalated


def _alias_map(
    items: list[dict],
    *,
    id_field: str,
    aliases: tuple[str, ...],
    additional_aliases: list[tuple[str, str]] | None = None,
) -> dict[str, str]:
    """Map display names and source identifiers to canonical registry ids."""
    result = {}
    ambiguous = set()
    alias_pairs = []
    for item in items:
        canonical = item.get(id_field)
        if not canonical:
            continue
        for field in aliases:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                alias_pairs.append((value, canonical))
    alias_pairs.extend(additional_aliases or [])
    for value, canonical in alias_pairs:
        if not isinstance(value, str) or not value.strip():
            continue
        key = value.casefold().strip()
        if key in ambiguous:
            continue
        if key in result and result[key] != canonical:
            ambiguous.add(key)
        else:
            result[key] = canonical
    for key in ambiguous:
        result.pop(key, None)
    return result


def _template_escalation(result: dict, reason: str, errors: list[str]) -> dict:
    result["escalated"] = True
    result["escalation_reason"] = reason
    result["escalation_errors"] = errors
    summary = result.get("template_summary")
    if isinstance(summary, dict):
        validation = summary.setdefault("validation", {})
        validation["status"] = "escalated"
        validation["errors"] = errors
    return result


def _normalized_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _feel_string_literal(value: str | None) -> str | None:
    """Return a string for a FEEL string literal, skipping expressions."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, str) else None
    except json.JSONDecodeError:
        # FEEL escapes a quote by doubling it.
        return text[1:-1].replace('""', '"')


def _dmn_string_outputs(dmn_xml: str) -> dict[str, set[str]]:
    """Extract literal string values by DMN decision-table output name."""
    try:
        root = ET.fromstring(dmn_xml)
    except ET.ParseError:
        return {}

    outputs: dict[str, set[str]] = {}
    for table in root.iter():
        if table.tag.rsplit("}", 1)[-1] != "decisionTable":
            continue
        columns = [
            child for child in table
            if child.tag.rsplit("}", 1)[-1] == "output"
        ]
        for column in columns:
            name = column.attrib.get("name")
            if name:
                values = outputs.setdefault(_normalized_name(name), set())
                for child in column.iter():
                    if child.tag.rsplit("}", 1)[-1] == "text":
                        literal = _feel_string_literal(child.text)
                        if literal is not None:
                            values.add(literal)

        for row in table:
            if row.tag.rsplit("}", 1)[-1] != "rule":
                continue
            entries = [
                child for child in row
                if child.tag.rsplit("}", 1)[-1] == "outputEntry"
            ]
            for index, entry in enumerate(entries):
                if index >= len(columns):
                    continue
                name = columns[index].attrib.get("name")
                if not name:
                    continue
                text = next(
                    (child.text for child in entry
                     if child.tag.rsplit("}", 1)[-1] == "text"),
                    None,
                )
                literal = _feel_string_literal(text)
                if literal is not None:
                    outputs.setdefault(_normalized_name(name), set()).add(literal)
    return outputs


def _parameter_decision_output_mismatches(
    process: dict,
    linked_dmn_ids: set[str],
    dmn_results: list[dict],
) -> list[str]:
    """Check enum parameters used by linked rules against literal DMN outputs."""
    linked_outputs: dict[str, dict[str, set[str]]] = {}
    for result in dmn_results:
        summary = result.get("decision_model_summary") or {}
        model_id = summary.get("id")
        xml = result.get("dmn_xml")
        if model_id in linked_dmn_ids and isinstance(xml, str):
            linked_outputs[model_id] = _dmn_string_outputs(xml)

    parameter_models: dict[str, set[str]] = {}
    for element in process.get("flowElements", []):
        if not isinstance(element, dict) or element.get("type") != "businessRuleTask":
            continue
        model_id = element.get("dmnModel")
        if model_id not in linked_dmn_ids:
            continue
        inputs = element.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            parameter_name = value.get("param") if isinstance(value, dict) else None
            if not isinstance(parameter_name, str):
                parameter_name = str(input_name)
            parameter_models.setdefault(_normalized_name(parameter_name), set()).add(model_id)

    # A parameter named for a declared decision output is also checked, even
    # when it is not explicitly mapped as a rule-task input.
    for model_id, outputs in linked_outputs.items():
        for output_name in outputs:
            parameter_models.setdefault(output_name, set()).add(model_id)

    mismatches = []
    for parameter in (process.get("acp", {}).get("parameters", []) or []):
        if not isinstance(parameter, dict):
            continue
        constraints = parameter.get("constraints") or {}
        enum_values = constraints.get("enum") if isinstance(constraints, dict) else None
        if not isinstance(enum_values, list) or not enum_values:
            continue
        parameter_name = parameter.get("name")
        if not isinstance(parameter_name, str):
            continue
        normalized = _normalized_name(parameter_name)
        model_ids = parameter_models.get(normalized)
        if not model_ids:
            model_ids = {
                model_id for model_id, outputs in linked_outputs.items()
                if normalized in outputs
            }
        for model_id in sorted(model_ids or ()):
            model_outputs = linked_outputs.get(model_id, {})
            output_name = normalized if normalized in model_outputs else None
            declared = (
                model_outputs[output_name]
                if output_name
                else set().union(*model_outputs.values()) if model_outputs else set()
            )
            if declared and not set(enum_values).issubset(declared):
                mismatches.append(
                    f"Parameter {parameter_name!r} enum values {sorted(set(enum_values))!r} "
                    f"are not a subset of linked decision model {model_id!r} output values "
                    f"{sorted(declared)!r}."
                )
    return mismatches


def _review_items(review: dict, key: str, template_id: str) -> list[dict]:
    values = review.get(key) or []
    if not isinstance(values, list):
        return []
    return [
        item for item in values
        if isinstance(item, dict) and item.get("templateId") == template_id
    ]


@mlflow.trace(name="assembly.apply_parameter_edits")
def _apply_parameter_edits(ir: ProcessIR, edits: list[dict], review: dict) -> None:
    """Apply reviewer defaults while retaining the original parameter source."""
    for edit in edits:
        name = str(edit.get("parameter", ""))
        parameter = next(
            (item for item in ir.process.acp.parameters if item.name == name),
            None,
        )
        if parameter is None:
            raise ValueError(f"parameter edit references unknown parameter {name!r}")
        if "value" not in edit or edit["value"] is None:
            raise ValueError(f"parameter edit for {name!r} requires a value")

        previous = parameter.source
        if previous is not None and previous.kind == "reviewer":
            previous = previous.previous
        if previous is None:
            raise ValueError(f"parameter {name!r} has no source provenance to preserve")

        note = str(edit.get("note") or review.get("overallComment") or "").strip()
        if not note:
            note = "Edited during pre-delivery review."
        parameter.default = edit["value"]
        parameter.source = SourceRef(
            kind="reviewer",
            reviewer=(str(review["reviewer"]) if review.get("reviewer") else None),
            note=note,
            previous=previous,
        )


@mlflow.trace(name="assembly.template_preflight")
def _preflight_template(
    template: AutomationTemplate,
    *,
    source_cpg: str,
) -> tuple[list[str], list[str], bool]:
    """Optionally validate a reviewer-edited BPMN artifact with the engine."""
    url = os.environ.get("BPMN_PREFLIGHT_URL", "").strip()
    if not url:
        return [], [], False
    query = urlencode({
        "validate_only": "true",
        **({"source_cpg": source_cpg} if source_cpg else {}),
    })
    separator = "&" if "?" in url else "?"
    try:
        response = requests.post(
            f"{url}{separator}{query}",
            json=template.model_dump(mode="json", exclude_none=True),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        is_validation = isinstance(payload, dict) and any(
            key in payload for key in ("valid", "messages", "errors")
        )
        messages = payload.get("messages", payload.get("errors", [])) if is_validation else []
        if isinstance(messages, (str, dict)):
            messages = [messages]
        errors: list[str] = []
        warnings: list[str] = []
        for message in messages or []:
            if isinstance(message, dict):
                text = message.get("text", message.get("message", str(message)))
                target = errors if message.get("severity", "ERROR").upper() == "ERROR" else warnings
                target.append(str(text))
            else:
                errors.append(str(message))
        invalid = response.status_code == 422 or (
            is_validation and payload.get("valid") is False
        )
        if invalid and not errors:
            errors.append(str(payload.get("detail") or "Kogito rejected the BPMN template"))
        if invalid:
            return errors, warnings, True
        if response.ok and is_validation:
            return errors, warnings, True
        warning = (
            f"BPMN preflight unavailable (HTTP {response.status_code}; no validation result)"
        )
        return [], [warning, *warnings], False
    except requests.RequestException as exc:
        return [], [f"BPMN preflight unavailable: {exc}"], False


def _resolve_template_links(
    bpmn_results: list[dict],
    recommendations: list[dict],
    dmn_results: list[dict],
    manifest: list[dict],
    cpg_id: str,
    artifact_review: dict | None = None,
) -> list[dict]:
    """Resolve template links, normalize IR, and compile publishable versions."""
    for recommendation in recommendations:
        recommendation["automation_template_ids"] = []
    rec_by_id = {
        item["id"]: item for item in recommendations if isinstance(item.get("id"), str)
    }
    manifest_rec_aliases = []
    for item in manifest:
        manifest_id = item.get("id")
        if (
            item.get("type") == "recommendation"
            and isinstance(manifest_id, str)
            and manifest_id in rec_by_id
        ):
            for field in ("title", "name"):
                title = item.get(field)
                if isinstance(title, str) and title.strip():
                    manifest_rec_aliases.append((title, manifest_id))
    rec_aliases = _alias_map(
        recommendations,
        id_field="id",
        aliases=("id", "title", "name"),
        additional_aliases=manifest_rec_aliases,
    )

    dmn_models = []
    delivered_dmn_ids = set()
    dmn_display_names = {}
    for result in dmn_results:
        summary = result.get("decision_model_summary") or {}
        if summary.get("id"):
            dmn_display_names[summary["id"]] = summary.get("name") or summary["id"]
            dmn_models.append({
                "id": summary["id"],
                "name": summary.get("name", ""),
                "model_id": summary["id"],
            })
            # Delivery publishes every decision model that has XML, escalated
            # or not (escalations travel alongside in escalated-items.json), and
            # acp-writer registers what is published. A template reference is
            # therefore "delivered" on the same condition; anything stricter
            # here escalates templates for decisions the consumer actually has.
            if result.get("dmn_xml"):
                delivered_dmn_ids.add(summary["id"])
    for item in manifest:
        if item.get("type") == "decision" and item.get("model_id"):
            dmn_display_names.setdefault(item["model_id"], item.get("name") or item["model_id"])
            dmn_models.append({
                "id": item["model_id"],
                "model_id": item["model_id"],
                "name": item.get("name", ""),
                "item_id": item.get("id", ""),
            })
    dmn_aliases = _alias_map(
        dmn_models, id_field="model_id",
        aliases=("id", "model_id", "name", "item_id"),
    )
    dmn_summary_values = {
        item["id"]: item for item in (
            result.get("decision_model_summary") or {} for result in dmn_results
        )
        if item.get("id")
    }

    id_counts = Counter(
        result.get("template_summary", {}).get("id")
        or result.get("item", {}).get("template_id")
        for result in bpmn_results
        if (
            result.get("template_summary", {}).get("id")
            or result.get("item", {}).get("template_id")
        )
    )
    resolved_results = []
    catalog = load_catalog()
    review = artifact_review if isinstance(artifact_review, dict) else {}
    for original in bpmn_results:
        result = deepcopy(original)
        item = result.get("item", {})
        summary = result.get("template_summary", {})
        template_id = summary.get("id") or item.get("template_id")
        l7_acknowledgements = _review_items(review, "acknowledgements", template_id)
        acknowledgement_note = (
            str(l7_acknowledgements[0].get("note", "")).strip()
            if l7_acknowledgements else ""
        )
        acknowledged_l7 = False
        if result.get("escalated"):
            if result.get("escalation_reason") != "undelivered-decision-reference":
                if (
                    result.get("escalation_reason") in _L7_ESCALATION_REASONS
                    and acknowledgement_note
                ):
                    acknowledged_l7 = True
                    validation = summary.setdefault("validation", {})
                    validation["status"] = "escalated"
                    validation["errors"] = list(result.get("escalation_errors", []))
                    validation["acknowledgement"] = acknowledgement_note
                    result["escalated"] = False
                else:
                    resolved_results.append(result)
                    continue
            else:
                # This is dependency-state, not a permanent template defect. Run
                # the same link resolution and ladder again against this assembly's
                # delivered decisions; it will re-escalate if the reference remains
                # unavailable.
                result.pop("escalated", None)
                result.pop("escalation_reason", None)
                result.pop("escalation_errors", None)
                validation = summary.setdefault("validation", {})
                validation["status"] = "valid"
                validation["errors"] = []
        if not template_id or id_counts[template_id] > 1:
            resolved_results.append(_template_escalation(
                result,
                "duplicate-template-id" if template_id else "invalid-template",
                [f"Template id {template_id!r} is missing or duplicated in this run."],
            ))
            continue
        if not isinstance(result.get("process_ir"), dict) or not summary:
            resolved_results.append(_template_escalation(
                result,
                "invalid-template",
                ["Generated template is missing its ProcessIR or summary."],
            ))
            continue

        requested_recommendations = item.get("linked_recommendations", []) or []
        if not requested_recommendations:
            resolved_results.append(_template_escalation(
                result,
                "no-recommendation-link",
                ["Automation templates must link to at least one recommendation."],
            ))
            continue
        linked_rec_ids = []
        unresolved_recommendations = []
        for value in requested_recommendations:
            key = str(value).casefold().strip()
            resolved_id = rec_aliases.get(key)
            if resolved_id:
                linked_rec_ids.append(resolved_id)
            else:
                unresolved_recommendations.append(str(value))
        if unresolved_recommendations:
            resolved_results.append(_template_escalation(
                result,
                "unresolved-recommendation-link",
                ["Could not resolve linked recommendation(s): "
                 + ", ".join(unresolved_recommendations)],
            ))
            continue

        requested_decisions = item.get("linked_decisions", []) or []
        linked_dmn_ids = []
        unresolved_decisions = []
        for value in requested_decisions:
            key = str(value).casefold().strip()
            model_id = dmn_aliases.get(key)
            if model_id:
                linked_dmn_ids.append(model_id)
            else:
                unresolved_decisions.append(str(value))
        if unresolved_decisions:
            resolved_results.append(_template_escalation(
                result,
                "unresolved-decision-link",
                ["Could not resolve linked decision model(s): "
                 + ", ".join(unresolved_decisions)],
            ))
            continue

        raw_ir = deepcopy(result["process_ir"])
        process = raw_ir.get("process")
        if not isinstance(process, dict) or not isinstance(process.get("acp"), dict):
            resolved_results.append(_template_escalation(
                result, "invalid-template", ["Generated ProcessIR has no process/acp object."]
            ))
            continue
        acp = process["acp"]
        if not isinstance(acp.get("triggers"), list):
            resolved_results.append(_template_escalation(
                result, "invalid-template", ["Generated ProcessIR has no trigger list."]
            ))
            continue
        link_errors = []
        undelivered_decision_errors = []
        for trigger in acp.get("triggers", []):
            if trigger.get("kind") == "recommendation":
                value = str(trigger.get("recommendation_id", ""))
                candidates = [value]
                if value.startswith("<rec-guid:") and value.endswith(">"):
                    candidates.insert(0, value[len("<rec-guid:"):-1].strip())
                resolved_id = next(
                    (rec_aliases[key.casefold().strip()] for key in candidates
                     if key.casefold().strip() in rec_aliases),
                    None,
                )
                if resolved_id is None and len(set(linked_rec_ids)) == 1:
                    resolved_id = linked_rec_ids[0]
                if resolved_id is None:
                    link_errors.append(f"Could not resolve recommendation trigger {value!r}.")
                else:
                    trigger["recommendation_id"] = resolved_id
                    if resolved_id not in linked_rec_ids:
                        linked_rec_ids.append(resolved_id)
            elif trigger.get("kind") == "dmn-output":
                value = str(trigger.get("model_id", ""))
                model_id = dmn_aliases.get(value.casefold().strip())
                if model_id is None:
                    link_errors.append(f"Could not resolve DMN trigger model {value!r}.")
                else:
                    trigger["model_id"] = model_id
                    linked_dmn_ids.append(model_id)

        for element in process.get("flowElements", []):
            if not isinstance(element, dict):
                continue
            if element.get("type") == "businessRuleTask":
                value = str(element.get("dmnModel", ""))
                model_id = dmn_aliases.get(value.casefold().strip())
                task_id = str(element.get("id", "?"))
                task_name = str(element.get("name", "")).strip()
                task_label = f"{task_id!r} ({task_name!r})" if task_name else repr(task_id)
                if model_id is None:
                    undelivered_decision_errors.append(
                        f"businessRuleTask {task_label} references decision {value!r}, "
                        "which was not delivered in this run."
                    )
                else:
                    element["dmnModel"] = model_id
                    linked_dmn_ids.append(model_id)
                    if model_id not in delivered_dmn_ids:
                        decision_name = dmn_display_names.get(model_id, value)
                        undelivered_decision_errors.append(
                            f"businessRuleTask {task_label} references decision {decision_name!r}, "
                            "which was not delivered in this run."
                        )
        if undelivered_decision_errors:
            resolved_results.append(_template_escalation(
                result,
                "undelivered-decision-reference",
                undelivered_decision_errors,
            ))
            continue
        if link_errors:
            resolved_results.append(_template_escalation(
                result, "unresolved-template-link", link_errors
            ))
            continue

        decision_output_errors = _parameter_decision_output_mismatches(
            process, set(linked_dmn_ids), dmn_results
        )
        if decision_output_errors:
            resolved_results.append(_template_escalation(
                result, "decision-output-mismatch", decision_output_errors
            ))
            continue

        try:
            ir = ProcessIR.model_validate(raw_ir)
            parameter_edits = _review_items(review, "parameterEdits", template_id)
            _apply_parameter_edits(ir, parameter_edits, review)
            summaries = {
                model_id: DecisionModelSummary.model_validate(summary_value)
                for model_id, summary_value in dmn_summary_values.items()
            }
            ladder = run_static_ladder(
                ir, catalog=catalog, stage="template", dmn_summaries=summaries,
            )
            if not ladder.ok:
                raise ValueError("; ".join(finding.message for finding in ladder.findings))
            namespaces = {
                model_id: value.namespace
                for model_id, value in summaries.items()
                if value.namespace
            }
            bpmn_xml = compile(ir, catalog=catalog, dmn_namespaces=namespaces)
            version = ir_version(ir)
            summary["id"] = ir.process.id
            summary["version"] = version
            summary["name"] = ir.process.name
            summary["description"] = ir.process.description
            summary["triggers"] = [trigger.model_dump(mode="json", exclude_none=True)
                                   for trigger in ir.process.acp.triggers]
            summary["parameters"] = [
                parameter.model_dump(mode="json", exclude_none=True)
                for parameter in ir.process.acp.parameters
            ]
            summary["linked_recommendation_ids"] = sorted(set(linked_rec_ids))
            summary["linked_decision_model_ids"] = sorted(set(linked_dmn_ids))
            summary["capabilities_used"] = sorted({
                element.taskName for element in ir.process.flowElements
                if isinstance(element, Task)
            })
            summary["ir_version"] = ir.ir_version
            summary["artifact_id"] = make_artifact_id(
                cpg_id, "bpmn", ir.process.name, summary.get("section", "")
            )
            validation = summary.setdefault("validation", {})
            rung_order = ["L0", "L1", "L2", "L3", "L4", "L5a", "L5b"]
            passed = set(validation.get("rungs_passed", []) or [])
            passed.update(ladder.rungs_passed)
            validation["rungs_passed"] = [
                rung for rung in rung_order if rung in passed
            ]
            if ladder.findings:
                validation["warnings"] = list(dict.fromkeys([
                    *(validation.get("warnings", []) or []),
                    *[
                        finding.message for finding in ladder.findings
                        if finding.severity == "WARNING"
                    ],
                ]))
            if acknowledged_l7:
                validation["status"] = "escalated"
                validation["errors"] = list(result.get("escalation_errors", []))
                validation["acknowledgement"] = acknowledgement_note
            else:
                validation["status"] = "valid"
                validation["errors"] = []
            linked_summaries = {
                model_id: value
                for model_id, value in summaries.items()
                if model_id in set(linked_dmn_ids)
            }
            linked_namespaces = {
                model_id: namespace
                for model_id, namespace in namespaces.items()
                if model_id in linked_summaries
            }
            template = AutomationTemplate.model_validate({
                "summary": summary,
                "ir": ir,
                "bpmn_xml": bpmn_xml,
                "dmn_namespaces": linked_namespaces,
                "dmn_summaries": linked_summaries,
            })
            engine_errors, engine_warnings, engine_checked = _preflight_template(
                template,
                source_cpg=cpg_id,
            )
            validation["kogito_checked"] = engine_checked
            template.summary.validation.kogito_checked = engine_checked
            if engine_warnings:
                validation["warnings"] = list(dict.fromkeys([
                    *(validation.get("warnings", []) or []),
                    *engine_warnings,
                ]))
                template.summary.validation.warnings = list(validation["warnings"])
            if engine_errors:
                validation["status"] = "escalated"
                validation["errors"] = engine_errors
                result["process_ir"] = ir.model_dump(mode="json", exclude_none=True)
                result["template_summary"] = summary
                resolved_results.append(_template_escalation(
                    result,
                    "engine-validation-failed",
                    engine_errors,
                ))
                continue
        except Exception as exc:
            resolved_results.append(_template_escalation(
                result, "invalid-template", [str(exc)]
            ))
            continue

        normalized = template.model_dump(mode="json", exclude_none=True)
        result["process_ir"] = ir.model_dump(mode="json", exclude_none=True)
        result["template_summary"] = normalized["summary"]
        result["bpmn_xml"] = bpmn_xml
        result["automation_template"] = normalized
        resolved_results.append(result)
        for rec_id in linked_rec_ids:
            if rec_id in rec_by_id:
                rec_templates = rec_by_id[rec_id].get("automation_template_ids") or []
                if template_id not in rec_templates:
                    rec_templates.append(template_id)
                rec_by_id[rec_id]["automation_template_ids"] = rec_templates

    for recommendation in recommendations:
        if recommendation.get("automation_template_ids"):
            recommendation["automation_template_ids"] = sorted(
                set(recommendation["automation_template_ids"])
            )
        else:
            recommendation["automation_template_ids"] = None
    return resolved_results


def _check_integrity(recommendations: list[dict], dmn_results: list[dict], cpg_metadata: dict) -> list[str]:
    """Run integrity checks on assembled output."""
    errors = []
    cpg_id = cpg_metadata.get("cpg_id", "")

    rec_ids = [r.get("id") for r in recommendations if r.get("id") is not None]
    if len(rec_ids) != len(set(rec_ids)):
        errors.append("Duplicate recommendation IDs found")

    dmn_ids = [d.get("decision_model_summary", {}).get("id") for d in dmn_results
               if d.get("decision_model_summary", {}).get("id") is not None]
    if len(dmn_ids) != len(set(dmn_ids)):
        errors.append("Duplicate decision model IDs found")

    for rec in recommendations:
        if rec.get("source_cpg") and rec["source_cpg"] not in ("TBD", cpg_id):
            errors.append(f"Rec {rec.get('id', '?')[:8]}: source_cpg '{rec['source_cpg']}' doesn't match '{cpg_id}'")

    if not recommendations and not dmn_results:
        errors.append("No recommendations or decision models produced")

    return errors


def _collect_from_output_dir(output_dir: str) -> tuple[list[dict], list[dict]]:
    """Collect DMN and recommendation results from the output directory."""
    out = Path(output_dir)
    dmn_results = []
    for dmn_file in sorted(out.glob("dmn/*.dmn")):
        dmn_results.append({
            "dmn_xml": dmn_file.read_text(),
            "item": {"name": dmn_file.stem},
        })

    all_recs = []
    for rec_file in sorted(out.glob("recommendations-*.json")):
        try:
            data = json.loads(rec_file.read_text())
            if isinstance(data, list):
                all_recs.extend(data)
            elif isinstance(data, dict) and "recommendations" in data:
                all_recs.extend(data["recommendations"])
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse %s", rec_file)

    return dmn_results, all_recs


@mlflow.trace(name="assembly")
def assembly(state: dict) -> dict:
    """Assemble all validated outputs from DMN and Rec tracks."""
    logger.info("── Assembly ──")
    cpg_metadata = state.get("cpg_metadata", {})
    output_dir = state.get("output_dir", "output")

    dmn_results = state.get("dmn_results") or []
    all_recs = state.get("recommendation_results") or []
    bpmn_results = state.get("bpmn_results") or []
    recommendation_escalations = state.get("recommendation_escalations") or []
    if not dmn_results and not all_recs:
        dmn_results, all_recs = _collect_from_output_dir(output_dir)

    cpg_id = cpg_metadata.get("cpg_id", "UNKNOWN")
    for rec in all_recs:
        if not rec.get("source_cpg") or rec["source_cpg"] == "TBD":
            rec["source_cpg"] = cpg_id

    all_recs = _resolve_cross_references(all_recs, dmn_results)
    bpmn_results = _resolve_template_links(
        bpmn_results,
        all_recs,
        dmn_results,
        state.get("item_manifest", []) or [],
        cpg_id,
        state.get("artifact_review"),
    )

    # Per-recommendation escalations travel on recommendation objects. Empty or
    # crashed sections use the separate section-level list so the bundle stays
    # a valid list of recommendations.
    escalated = _collect_escalations(
        dmn_results, all_recs, recommendation_escalations, bpmn_results,
    )

    integrity_errors = _check_integrity(all_recs, dmn_results, cpg_metadata)
    if integrity_errors:
        for err in integrity_errors:
            logger.warning("Integrity check: %s", err)

    recommendation_bundle = {
        "contract_version": CONTRACT_VERSION,
        "source_cpg": cpg_id,
        "recommendations": all_recs,
    }

    assembly_report = {
        "cpg_id": cpg_id,
        "recommendations_count": len(all_recs),
        "dmn_models_count": len(dmn_results),
        "automation_templates_count": sum(
            bool(item.get("automation_template")) and not item.get("escalated")
            for item in bpmn_results
        ),
        "escalated_count": len(escalated),
        "integrity_errors": integrity_errors,
    }

    write_artifact(output_dir, "recommendation-bundle.json", recommendation_bundle)
    write_artifact(output_dir, "assembly-report.json", assembly_report)
    for result in bpmn_results:
        if result.get("escalated") or not result.get("automation_template"):
            continue
        template = result["automation_template"]
        template_id = template["summary"]["id"]
        write_artifact(output_dir, f"bpmn/{template_id}.template.json", template)
        write_artifact(output_dir, f"bpmn/{template_id}.bpmn", template["bpmn_xml"])
    if escalated:
        write_artifact(output_dir, "escalated-items.json", escalated)

    logger.info(
        "Assembly complete: %d recs, %d DMN models, %d escalated, %d integrity errors",
        len(all_recs), len(dmn_results), len(escalated), len(integrity_errors),
    )

    return {
        "dmn_results": dmn_results,
        "recommendation_results": all_recs,
        "bpmn_results": bpmn_results,
        "escalated_items": escalated,
        "assembly_report": assembly_report,
    }
