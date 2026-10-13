"""FHIR Bundle Builder — deterministic FHIR R4 resource construction.

Produces Patient, CarePlan, Goal, MedicationRequest, ServiceRequest, and the
AI-transparency resources (AI-Device, AI-Provenance, AI-InputPrompt /
AI-ModelCard DocumentReferences) from a PlanningBrief. All AI-produced
resources carry AIAST ``meta.security`` tags.

AI-transparency resource construction lives in
``acp_writer.services.ai_transparency`` (the HL7 AI Transparency IG is the
source of truth); this builder wires those resources into the bundle.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import mlflow

from acp_writer.planning_brief import ActivityType, PlanningBrief
from acp_writer.services import ai_transparency as ait
from acp_writer.services.ai_transparency import ACP_EXT_BASE, aiast_security

# Retained for import back-compat; sourced from the IG-conformant helper.
AIAST_SECURITY = aiast_security()


def _uuid() -> str:
    return str(uuid.uuid4())


def _urn(uid: str) -> str:
    return f"urn:uuid:{uid}"


def _meta() -> dict:
    return {"security": [AIAST_SECURITY]}


def _entry(resource: dict, uid: str) -> dict:
    return {
        "fullUrl": _urn(uid),
        "resource": resource,
        "request": {
            "method": "POST",
            "url": resource["resourceType"],
        },
    }


def _codeable_concept(code_dict: dict | None, text: str | None = None) -> dict:
    if code_dict:
        cc: dict[str, Any] = {
            "coding": [{
                "system": code_dict.get("system", ""),
                "code": code_dict.get("code", ""),
            }],
        }
        if code_dict.get("display"):
            cc["coding"][0]["display"] = code_dict["display"]
        if text:
            cc["text"] = text
        elif code_dict.get("display"):
            cc["text"] = code_dict["display"]
        return cc
    if text:
        return {"text": text}
    return {"text": "Unknown"}


@mlflow.trace(name="fhir.build_bundle")
def build_fhir_bundle(
    brief: PlanningBrief,
    patient_demographics: dict[str, Any] | None = None,
    model_id: str | None = None,
    prompts: dict[str, str] | None = None,
    careplan_id: str | None = None,
) -> dict:
    """Build a complete FHIR R4 transaction Bundle from a PlanningBrief.

    ``model_id`` names the LLM behind the AI-Device (from state ``llm_model``).
    ``prompts`` maps a human label → rendered prompt text; each becomes an
    AI-InputPrompt DocumentReference (when ``ACP_CAPTURE_PROMPTS`` is enabled).
    ``careplan_id`` is the stable business identifier used for idempotent
    transaction creation; it is distinct from the FHIR resource's UUID id.
    """
    entries: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    prompts = prompts or {}

    bundle_id = _uuid()

    patient_uid = _uuid()
    patient_urn = _urn(patient_uid)
    patient_resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": patient_uid,
        "meta": _meta(),
    }
    if_none_exist = None
    if patient_demographics:
        identifiers = patient_demographics.get("identifiers", [])
        if identifiers:
            patient_resource["identifier"] = [
                {"system": ident["system"], "value": ident["value"]}
                for ident in identifiers
            ]
            first = identifiers[0]
            if_none_exist = f"identifier={first['system']}|{first['value']}"
        if patient_demographics.get("name"):
            patient_resource["name"] = [{"text": patient_demographics["name"]}]
        if patient_demographics.get("gender"):
            patient_resource["gender"] = patient_demographics["gender"]
        if patient_demographics.get("birth_date"):
            patient_resource["birthDate"] = patient_demographics["birth_date"]

    patient_entry: dict[str, Any] = {
        "fullUrl": patient_urn,
        "resource": patient_resource,
        "request": {
            "method": "POST",
            "url": "Patient",
        },
    }
    if if_none_exist:
        patient_entry["request"]["ifNoneExist"] = if_none_exist
    entries.append(patient_entry)

    patient_ref = patient_urn

    goal_uids: list[str] = []
    activity_refs: list[dict] = []
    activity_uid_map: dict[int, str] = {}
    careplan_uid = _uuid()
    careplan_urn = _urn(careplan_uid)
    careplan_business_id = careplan_id or _uuid().replace("-", "")
    automation_resources: dict[tuple[str, str], dict[str, Any]] = {}

    for goal in brief.goals:
        uid = _uuid()
        goal_uids.append(uid)
        goal_resource: dict[str, Any] = {
            "resourceType": "Goal",
            "id": uid,
            "meta": _meta(),
            "lifecycleStatus": "proposed",
            "description": {"text": goal.description},
            "subject": {"reference": patient_ref},
        }
        if goal.target_measure_code and goal.target_value:
            target: dict[str, Any] = {
                "measure": _codeable_concept(
                    goal.target_measure_code.model_dump() if goal.target_measure_code else None,
                ),
            }
            detail: dict[str, Any] = {}
            if goal.target_value.high is not None:
                detail["high"] = {
                    "value": goal.target_value.high,
                    "unit": goal.target_value.unit,
                    "system": "http://unitsofmeasure.org",
                }
            if goal.target_value.low is not None:
                detail["low"] = {
                    "value": goal.target_value.low,
                    "unit": goal.target_value.unit,
                    "system": "http://unitsofmeasure.org",
                }
            if detail:
                target["detailRange"] = detail
            goal_resource["target"] = [target]
        entries.append(_entry(goal_resource, uid))

    for i, activity in enumerate(brief.activities):
        uid = _uuid()
        activity_ref: dict[str, Any] = {"id": activity.id}

        if activity.type == ActivityType.MEDICATION:
            activity_uid_map[i] = uid
            resource: dict[str, Any] = {
                "resourceType": "MedicationRequest",
                "id": uid,
                "meta": _meta(),
                "status": "draft",
                "intent": "proposal",
                "subject": {"reference": patient_ref},
                "medicationCodeableConcept": _codeable_concept(
                    activity.code.model_dump() if activity.code else None,
                    activity.description,
                ),
            }
            if activity.dose:
                resource["dosageInstruction"] = [{
                    "text": f"{activity.dose} {activity.route or ''} {activity.frequency or ''}".strip(),
                }]
            entries.append(_entry(resource, uid))
            activity_ref["reference"] = {"reference": _urn(uid)}

        elif activity.type in (ActivityType.MONITORING, ActivityType.REFERRAL):
            activity_uid_map[i] = uid
            resource = {
                "resourceType": "ServiceRequest",
                "id": uid,
                "meta": _meta(),
                "status": "draft",
                "intent": "proposal",
                "subject": {"reference": patient_ref},
                "code": _codeable_concept(
                    activity.code.model_dump() if activity.code else None,
                    activity.description,
                ),
            }
            if activity.frequency:
                resource["note"] = [{"text": f"Frequency: {activity.frequency}"}]
            entries.append(_entry(resource, uid))
            activity_ref["reference"] = {"reference": _urn(uid)}

        else:
            detail_entry: dict[str, Any] = {
                "detail": {
                    "status": "not-started",
                    "description": activity.description,
                },
            }
            code = _codeable_concept(
                activity.code.model_dump() if activity.code else None,
                activity.description,
            )
            if code:
                detail_entry["detail"]["code"] = code
            activity_ref.update(detail_entry)

        for automation in activity.automations:
            if automation.enabled is not True or automation.validation.status != "valid":
                continue
            key = (automation.id, automation.revision or "")
            record = automation_resources.get(key)
            if record is None:
                template_ref = automation.template_refs[0]
                snapshot = next(
                    (
                        item for item in automation.template_snapshots
                        if item.process.id == template_ref.template_id
                    ),
                    None,
                )
                if snapshot is None:
                    raise ValueError(
                        f"automation {automation.id} has no snapshot for template {template_ref.template_id}"
                    )
                name = snapshot.process.name
                docref_uid = _uuid()
                task_uid = _uuid()
                docref_ref = _urn(docref_uid)
                task_ref = _urn(task_uid)
                docref = ait.build_automation_docref(
                    docref_id=docref_uid,
                    task_id=task_uid,
                    patient_ref=patient_ref,
                    careplan_ref=careplan_urn,
                    revision=automation.revision or "",
                    name=name,
                    template_id=template_ref.template_id,
                    bpmn_xml=automation.bpmn_xml,
                    now=now,
                )
                focus_ref = activity_ref.get("reference", {}).get("reference")
                task = ait.build_automation_task(
                    task_id=task_uid,
                    automation=automation,
                    name=name,
                    patient_ref=patient_ref,
                    careplan_ref=careplan_urn,
                    focus_ref=focus_ref,
                    docref_ref=docref_ref,
                    now=now,
                )
                entries.append(_entry(docref, docref_uid))
                entries.append(_entry(task, task_uid))
                record = {
                    "docref_ref": docref_ref,
                    "task_ref": task_ref,
                    "task": task,
                    "automation": automation,
                    "template_refs": automation.template_refs,
                    "activity_indices": [],
                    "recommendation_ids": set(),
                    "name": name,
                }
                automation_resources[key] = record
            elif not record["task"].get("focus"):
                focus_ref = activity_ref.get("reference", {}).get("reference")
                if focus_ref:
                    record["task"]["focus"] = {"reference": focus_ref}
            record["activity_indices"].append(i)
            record["recommendation_ids"].update(
                activity.source_recommendation_ids
                or ([activity.source_recommendation_id] if activity.source_recommendation_id else [])
            )
            activity_ref.setdefault("extension", []).append(
                ait.activity_automation_extension(
                    record["docref_ref"], record["task_ref"], status="enabled"
                )
            )
        activity_refs.append(activity_ref)

    careplan: dict[str, Any] = {
        "resourceType": "CarePlan",
        "id": careplan_uid,
        "meta": _meta(),
        "identifier": [{
            "system": f"{ait.ACP_CS_BASE}/careplan-id",
            "value": careplan_business_id,
        }],
        "status": "draft",
        "intent": "proposal",
        "title": "Care Plan",
        "category": [{
            "coding": [{
                "system": "http://hl7.org/fhir/us/core/CodeSystem/careplan-category",
                "code": "assess-plan",
            }],
        }],
        "subject": {"reference": patient_ref},
        "created": now,
        "goal": [{"reference": _urn(uid)} for uid in goal_uids],
        "activity": activity_refs,
    }
    # WS4 marker: exactly one boolean extension when the plan has conflicts.
    if brief.conflicts:
        careplan["extension"] = [{
            "url": f"{ACP_EXT_BASE}/careplan-conflict-detected",
            "valueBoolean": True,
        }]
    careplan_entry = _entry(careplan, careplan_uid)
    careplan_entry["request"]["ifNoneExist"] = (
        f"identifier={ait.ACP_CS_BASE}/careplan-id|{careplan_business_id}"
    )
    entries.append(careplan_entry)

    # ── AI-Device ───────────────────────────────────────────────────────────
    device_uid = _uuid()
    device_urn = _urn(device_uid)
    ai_device = ait.build_ai_device(device_uid, model_id)
    entries.append(_entry(ai_device, device_uid))

    # ── AI-InputPrompt DocumentReferences (captured prompts) ─────────────────
    input_prompt_urns: list[str] = []
    input_prompt_by_label: dict[str, str] = {}
    if ait.prompts_capture_enabled():
        for label, text in prompts.items():
            if not text:
                continue
            docref_uid = _uuid()
            docref = ait.build_input_prompt_docref(
                docref_uid, text, f"Rendered prompt: {label}"
            )
            entries.append(_entry(docref, docref_uid))
            prompt_ref = _urn(docref_uid)
            input_prompt_urns.append(prompt_ref)
            input_prompt_by_label[label] = prompt_ref

    # Each instance gets one provenance record, even when a composed instance
    # is referenced from several CarePlan.activity entries.
    for (automation_id, _revision), record in automation_resources.items():
        automation = record["automation"]
        activity_ids = {
            brief.activities[index].id
            for index in record["activity_indices"]
        }
        template_ids = {ref.template_id for ref in record["template_refs"]}
        prompt_refs = [
            prompt_ref
            for label, prompt_ref in input_prompt_by_label.items()
            if any(
                label.startswith(f"{activity_id}:{template_id}:")
                for activity_id in activity_ids
                for template_id in template_ids
            )
        ]
        provenance = ait.build_automation_provenance(
            prov_uid=_uuid(),
            careplan_ref=careplan_urn,
            activity_indices=record["activity_indices"],
            docref_ref=record["docref_ref"],
            task_ref=record["task_ref"],
            device_ref=device_urn,
            recorded=now,
            source_recommendation_ids=sorted(record["recommendation_ids"]),
            template_refs=record["template_refs"],
            input_prompt_refs=prompt_refs,
        )
        entries.append(_entry(provenance, provenance["id"]))

    # ── AI-ModelCard DocumentReference (optional, env-gated) ─────────────────
    model_card_urn: str | None = None
    mc_url = ait.model_card_url()
    if mc_url:
        mc_uid = _uuid()
        entries.append(_entry(ait.build_model_card_docref(mc_uid, mc_url), mc_uid))
        model_card_urn = _urn(mc_uid)

    # ── Bundle-level AI-Provenance ───────────────────────────────────────────
    # The Patient and captured prompt DocumentReferences carry AIAST labels too,
    # so include them in the aggregate provenance rather than leaving those
    # labels without a traceable attribution target.
    all_targets = [{"reference": careplan_urn}, {"reference": patient_urn}]
    all_targets.extend({"reference": _urn(uid)} for uid in goal_uids)
    for _idx, uid in activity_uid_map.items():
        all_targets.append({"reference": _urn(uid)})
    all_targets.extend({"reference": ref} for ref in input_prompt_urns)

    prov_entities: list[dict] = []
    for cpg_id in brief.applicable_cpgs:
        prov_entities.append({
            "role": "derivation",
            "what": {"display": f"CPG: {cpg_id}"},
        })
    for ip_urn in input_prompt_urns:
        prov_entities.append({"role": "derivation", "what": {"reference": ip_urn}})
    if model_card_urn:
        prov_entities.append({"role": "derivation", "what": {"reference": model_card_urn}})

    prov_uid = _uuid()
    ai_provenance = ait.build_ai_provenance(
        prov_uid=prov_uid,
        targets=all_targets,
        device_urn=device_urn,
        recorded=now,
        occurred=now,
        entities=prov_entities or None,
    )
    entries.append(_entry(ai_provenance, prov_uid))

    # ── Per-activity source Provenances ──────────────────────────────────────
    for i, activity in enumerate(brief.activities):
        uid = activity_uid_map.get(i)
        if uid:
            prov_target: dict[str, Any] = {"reference": _urn(uid)}
        else:
            prov_target = {
                "reference": careplan_urn,
                "extension": [ait.targetPath_ext(f"CarePlan.activity[{i}].detail")],
            }

        recommendation_ids = list(dict.fromkeys(
            activity.source_recommendation_ids
            or ([activity.source_recommendation_id] if activity.source_recommendation_id else [])
        ))
        for recommendation_id in recommendation_ids:
            act_prov_uid = _uuid()
            act_provenance = ait.build_ai_provenance(
                prov_uid=act_prov_uid,
                targets=[prov_target],
                device_urn=device_urn,
                recorded=now,
                occurred=now,
                entities=[{
                    "role": "source",
                    "what": {
                        "display": f"Recommendation: {recommendation_id}",
                        "identifier": {"value": recommendation_id},
                    },
                }],
            )
            entries.append(_entry(act_provenance, act_prov_uid))

    # ── Per-goal source Provenances ──────────────────────────────────────────
    for index, goal in enumerate(brief.goals):
        recommendation_ids = list(dict.fromkeys(
            goal.source_recommendation_ids
            or ([goal.source_recommendation_id] if goal.source_recommendation_id else [])
        ))
        for recommendation_id in recommendation_ids:
            goal_prov_uid = _uuid()
            goal_provenance = ait.build_ai_provenance(
                prov_uid=goal_prov_uid,
                targets=[{"reference": _urn(goal_uids[index])}],
                device_urn=device_urn,
                recorded=now,
                occurred=now,
                entities=[{
                    "role": "source",
                    "what": {
                        "display": f"Recommendation: {recommendation_id}",
                        "identifier": {"value": recommendation_id},
                    },
                }],
            )
            entries.append(_entry(goal_provenance, goal_prov_uid))

    # ── Conflict Provenances (WS4) ───────────────────────────────────────────
    goal_urns = [_urn(uid) for uid in goal_uids]
    activity_urn_map = {idx: _urn(uid) for idx, uid in activity_uid_map.items()}
    for conflict in brief.conflicts:
        conf_prov_uid = _uuid()
        conf_prov = ait.build_conflict_provenance(
            prov_uid=conf_prov_uid,
            conflict=conflict,
            careplan_urn=careplan_urn,
            goal_urns=goal_urns,
            activity_urn_map=activity_urn_map,
            device_urn=device_urn,
            recorded=now,
            occurred=now,
        )
        entries.append(_entry(conf_prov, conf_prov_uid))

    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "id": bundle_id,
        "type": "transaction",
        "timestamp": now,
        "entry": entries,
    }
    return bundle
