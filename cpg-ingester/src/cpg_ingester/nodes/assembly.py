"""Assembly Agent — deterministic cross-reference resolution and integrity checks."""

import json
import logging
from collections import Counter
from copy import deepcopy
from pathlib import Path

import mlflow

from cpg_contracts import CONTRACT_VERSION
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR, Task, ir_version
from cpg_contracts.automation.templates import AutomationTemplate
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.decisions import DecisionModelSummary
from cpg_ingester.artifact_id import make_artifact_id
from cpg_ingester.output import write_artifact

logger = logging.getLogger(__name__)


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


def _resolve_template_links(
    bpmn_results: list[dict],
    recommendations: list[dict],
    dmn_results: list[dict],
    manifest: list[dict],
    cpg_id: str,
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
    for result in dmn_results:
        summary = result.get("decision_model_summary") or {}
        if summary.get("id"):
            dmn_models.append({
                "id": summary["id"],
                "name": summary.get("name", ""),
                "model_id": summary["id"],
            })
    for item in manifest:
        if item.get("type") == "decision" and item.get("model_id"):
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
    for original in bpmn_results:
        result = deepcopy(original)
        item = result.get("item", {})
        summary = result.get("template_summary", {})
        template_id = summary.get("id") or item.get("template_id")
        if result.get("escalated"):
            resolved_results.append(result)
            continue
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
                if model_id is None:
                    link_errors.append(
                        f"Could not resolve businessRuleTask {element.get('id')!r} model {value!r}."
                    )
                else:
                    element["dmnModel"] = model_id
                    linked_dmn_ids.append(model_id)
        if link_errors:
            resolved_results.append(_template_escalation(
                result, "unresolved-template-link", link_errors
            ))
            continue

        try:
            ir = ProcessIR.model_validate(raw_ir)
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
            template = AutomationTemplate.model_validate({
                "summary": summary,
                "ir": ir,
                "bpmn_xml": bpmn_xml,
            })
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
