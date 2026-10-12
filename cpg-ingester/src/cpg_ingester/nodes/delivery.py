"""Delivery Agent — publishes assembled artifacts to the artifact store.

The ingester does not communicate with downstream consumers directly.
It publishes artifacts to a well-known location in MinIO/S3 and returns
a manifest.  The SonataFlow orchestrator owns downstream notification.
"""

import logging

import mlflow
from cpg_contracts import get_artifact_store

from cpg_contracts.automation.templates import AutomationTemplate
from cpg_ingester.artifact_id import make_artifact_id
from cpg_ingester.output import write_artifact

logger = logging.getLogger(__name__)


@mlflow.trace(name="delivery")
def delivery(state: dict) -> dict:
    """Publish assembled artifacts to the artifact store."""
    logger.info("── Delivery ──")
    cpg_metadata = state.get("cpg_metadata", {})
    dmn_results = state.get("dmn_results", []) or []
    recommendation_results = state.get("recommendation_results", [])
    bpmn_results = state.get("bpmn_results", []) or []
    escalated_items = state.get("escalated_items", [])
    assembly_report = state.get("assembly_report", {})
    output_dir = state.get("output_dir", "output")
    # ArtifactStore owns a boto3 client and cannot be saved by LangGraph's
    # msgpack checkpointer. Resolve the configured client only at delivery.
    store = state.get("artifact_store")
    if store is None:
        store = get_artifact_store()

    cpg_id = cpg_metadata.get("cpg_id", "UNKNOWN")
    artifacts: list[dict] = []
    errors: list[str] = []

    if not store:
        logger.info("No artifact store available — writing to output_dir only")
        status = {
            "published": False,
            "reason": "no artifact store configured",
            "cpg_id": cpg_id,
            "artifacts": [],
            "errors": [],
        }
        write_artifact(output_dir, "delivery-status.json", status)
        return {"delivery_status": status}

    base_key = f"published/{cpg_id}"

    if cpg_metadata:
        try:
            ref = store.put(f"{base_key}/metadata.json", cpg_metadata)
            artifacts.append({"type": "metadata", "ref": ref, "cpg_id": cpg_id})
            logger.info("Published metadata: %s", ref)
        except Exception as e:
            errors.append(f"Failed to publish metadata: {e}")
            logger.error("Failed to publish metadata: %s", e)

    for dmn in dmn_results:
        dmn_xml = dmn.get("dmn_xml", "") if isinstance(dmn, dict) else ""
        if not dmn_xml:
            continue
        name = dmn.get("item", {}).get("name", "unknown") if isinstance(dmn, dict) else "unknown"
        section = dmn.get("item", {}).get("section", "") if isinstance(dmn, dict) else ""
        artifact_id = make_artifact_id(cpg_id, "dmn", name, section)
        try:
            ref = store.put_raw(
                f"{base_key}/dmn/{name}.dmn",
                dmn_xml.encode("utf-8"),
                "application/xml",
            )
            artifacts.append({"type": "dmn", "ref": ref, "name": name, "artifact_id": artifact_id})
            logger.info("Published DMN model: %s (%s)", ref, artifact_id)
        except Exception as e:
            errors.append(f"Failed to publish DMN '{name}': {e}")
            logger.error("Failed to publish DMN '%s': %s", name, e)

    if recommendation_results:
        recs_list = recommendation_results if isinstance(recommendation_results, list) else []
        for rec in recs_list:
            if isinstance(rec, dict) and "artifact_id" not in rec:
                rec_name = rec.get("title", "")
                rec_section = rec.get("section", "")
                rec["artifact_id"] = make_artifact_id(cpg_id, "rec", rec_name, rec_section)
        bundle = {
            "contract_version": cpg_metadata.get("contract_version", "1.0"),
            "source_cpg": cpg_id,
            "recommendations": recs_list,
        }
        try:
            ref = store.put(f"{base_key}/recommendations.json", bundle)
            artifacts.append({"type": "recommendations", "ref": ref, "count": len(recs_list)})
            logger.info("Published %d recommendations: %s", len(recs_list), ref)
        except Exception as e:
            errors.append(f"Failed to publish recommendations: {e}")
            logger.error("Failed to publish recommendations: %s", e)

    for result in bpmn_results:
        if not isinstance(result, dict) or result.get("escalated"):
            continue
        raw_template = result.get("automation_template")
        if not raw_template:
            continue
        try:
            template = AutomationTemplate.model_validate(raw_template)
            summary = template.summary
            base = f"{base_key}/bpmn/{summary.id}"
            template_key = f"{base}/{summary.version}.template.json"
            bpmn_key = f"{base}/{summary.version}.bpmn"
            latest_key = f"{base}/latest.json"
            template_payload = template.model_dump(mode="json", exclude_none=True)
            template_ref = store.put(template_key, template_payload)
            bpmn_ref = store.put_raw(
                bpmn_key,
                template.bpmn_xml.encode("utf-8"),
                "application/xml",
            )
            store.put(latest_key, {
                "template_id": summary.id,
                "version": summary.version,
                "template_ref": template_ref,
                "bpmn_ref": bpmn_ref,
                "artifact_id": summary.artifact_id,
                "name": summary.name,
            })
            artifacts.append({
                "type": "bpmn",
                "ref": template_ref,
                "name": summary.name,
                "artifact_id": summary.artifact_id,
                "template_id": summary.id,
                "version": summary.version,
            })
            logger.info(
                "Published BPMN template %s version %s: %s",
                summary.id, summary.version, template_ref,
            )
        except Exception as exc:
            message = (
                "Failed to publish BPMN template "
                f"{result.get('template_summary', {}).get('id', 'unknown')}: {exc}"
            )
            errors.append(message)
            logger.error("%s", message)

    if assembly_report:
        try:
            ref = store.put(f"{base_key}/assembly-report.json", assembly_report)
            artifacts.append({"type": "assembly_report", "ref": ref})
        except Exception as e:
            errors.append(f"Failed to publish assembly report: {e}")

    if escalated_items:
        try:
            ref = store.put(f"{base_key}/escalated-items.json", escalated_items)
            artifacts.append({"type": "escalated_items", "ref": ref, "count": len(escalated_items)})
        except Exception as e:
            errors.append(f"Failed to publish escalated items: {e}")

    status = {
        "published": len(artifacts) > 0 and len(errors) == 0,
        "cpg_id": cpg_id,
        "artifact_location": f"{store.bucket}:{base_key}",
        "artifacts": artifacts,
        "errors": errors,
        "escalated_items_count": len(escalated_items),
    }
    write_artifact(output_dir, "delivery-status.json", status)

    if errors:
        logger.error("Delivery completed with %d errors", len(errors))
    else:
        logger.info("Delivery complete — %d artifacts published to %s", len(artifacts), status["artifact_location"])

    return {"delivery_status": status}
