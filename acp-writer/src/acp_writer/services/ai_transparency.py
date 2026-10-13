"""AI Transparency on FHIR — resource construction conforming to the HL7 IG.

Pure functions that build the AI-transparency FHIR resources used across the
acp-writer bundle: AI-Device, AI-Provenance, AI-InputPrompt / AI-ModelCard
DocumentReferences, AIAST security labels, the stock ``targetPath`` extension,
and the ``AIconfidence`` extension.

Conformance target: the HL7 *AI Transparency on FHIR* IG (local copy in
``working/AI Transparency/``). Canonical base is
``http://hl7.org/fhir/uv/aitransparency`` — note the un-hyphenated ``aitransparency``.

All functions return plain dicts and take timestamps as arguments (never
generating ``now`` themselves) so that a single bundle shares one timestamp.
The project's own (non-IG) extensions hang off ``ACP_EXT_BASE``.
"""

import base64
import os
from importlib import metadata
from typing import TYPE_CHECKING, Any

import mlflow
from cpg_contracts.automation.expressions import ExpressionError, parse_duration

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids any import ordering risk
    from acp_writer.services.reviewer import ReviewerContext

# ── Canonical URLs (verbatim from the IG local copy) ────────────────────────
IG_BASE = "http://hl7.org/fhir/uv/aitransparency"
AI_DEVICE_PROFILE = f"{IG_BASE}/StructureDefinition/AI-Device"
AI_PROVENANCE_PROFILE = f"{IG_BASE}/StructureDefinition/AI-Provenance"
AI_INPUT_PROMPT_PROFILE = f"{IG_BASE}/StructureDefinition/AI-InputPrompt"
AI_MODEL_CARD_PROFILE = f"{IG_BASE}/StructureDefinition/AI-ModelCard"

AIKIND_EXT = f"{IG_BASE}/StructureDefinition/aitransparency.AIKind"
AICONFIDENCE_EXT = f"{IG_BASE}/StructureDefinition/AIconfidence"

DEVICE_TYPE_CS = f"{IG_BASE}/CodeSystem/AIdeviceTypeCS"
AI_INPUTS_CS = f"{IG_BASE}/CodeSystem/AIinputsCS"

# Device-type / AI-kind / AI-agent-role codes (AIdeviceTypeCS).
AI_ROOT_CODE = "Artificial-Intelligence"          # Device.type (fixed by profile)
LLM_KIND_CODE = "Large-Language-Models"           # AIKind for an LLM

# DocumentReference type/category codes (AIinputsCS). Casing is inconsistent in
# the IG — reproduce exactly (AIInputPrompt / AIModelCard).
AI_INPUT_PROMPT_CODE = "AIInputPrompt"
AI_MODEL_CARD_CODE = "AIModelCard"

# Stock FHIR extension for narrowing a Provenance.target to a FHIRPath.
TARGET_PATH_EXT = "http://hl7.org/fhir/StructureDefinition/targetPath"

# AIAST security label (Provenance.reason AIReason slice + meta.security tag).
AIAST_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationValue"
AIAST_CODE = "AIAST"
AIAST_DISPLAY = "Artificial Intelligence asserted"

# CLINAST_AIRPT security label — replaces AIAST once a clinician approves the
# plan (clinician asserted, from an AI report). Same v3-ObservationValue system.
CLINAST_AIRPT_CODE = "CLINAST_AIRPT"
CLINAST_AIRPT_SECURITY = {
    "system": AIAST_SYSTEM,
    "code": CLINAST_AIRPT_CODE,
    "display": "clinician asserted from AI reported",
}

# AIconfidence categorical slice is bound (required) to certainty-rating.
CERTAINTY_RATING_CS = "http://terminology.hl7.org/CodeSystem/certainty-rating"

# Provenance agent participation-type system.
PROVENANCE_PARTICIPANT_TYPE = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"

# The project's own (non-IG) extension base — §4.6 of the dev plan. Sam may
# change this value later; it is defined once here.
ACP_EXT_BASE = "https://github.com/samschifman/cpg-to-acp/fhir/StructureDefinition"
ACP_CS_BASE = "https://github.com/samschifman/cpg-to-acp/fhir/CodeSystem"

ACTIVITY_AUTOMATION_EXT = f"{ACP_EXT_BASE}/activity-automation"
AUTOMATION_TEMPLATE_EXT = f"{ACP_EXT_BASE}/automation-template"
AUTOMATION_PARAMETER_SOURCE_EXT = f"{ACP_EXT_BASE}/automation-parameter-source"
AUTOMATION_REVISION_SYSTEM = f"{ACP_CS_BASE}/automation-revision"
AUTOMATION_ID_SYSTEM = f"{ACP_CS_BASE}/automation-id"
AUTOMATION_DOCUMENT_TYPE_SYSTEM = f"{ACP_CS_BASE}/automation-document-type"
AUTOMATION_DOCUMENT_CATEGORY_SYSTEM = f"{ACP_CS_BASE}/automation-document-category"
AUTOMATION_TASK_CODE_SYSTEM = f"{ACP_CS_BASE}/automation-task-code"
AUTOMATION_INPUT_SYSTEM = f"{ACP_CS_BASE}/automation-input"
DOCUMENT_FORMAT_SYSTEM = f"{ACP_CS_BASE}/document-format"


def _app_version() -> str:
    """Installed acp-writer version, falling back to the pyproject value."""
    try:
        return metadata.version("acp-writer")
    except metadata.PackageNotFoundError:  # pragma: no cover - dev-only path
        return "0.2.0"


def aiast_security() -> dict:
    """The AIAST ``meta.security`` coding for AI-produced resources."""
    return {"system": AIAST_SYSTEM, "code": AIAST_CODE, "display": AIAST_DISPLAY}


def targetPath_ext(fhirpath: str) -> dict:
    """Stock FHIR ``targetPath`` extension narrowing a target to a FHIRPath."""
    return {"url": TARGET_PATH_EXT, "valueString": fhirpath}


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _binding_value(parameter: Any, binding: Any) -> tuple[str, Any]:
    """Return the FHIR value[x] element and its value for one plan binding."""
    name = str(_value(binding, "name", ""))
    value = _value(binding, "value")
    if name.endswith("__code"):
        if not isinstance(value, str) or "|" not in value:
            raise ValueError(f"resolved code binding {name!r} must be system|code")
        system, code = value.split("|", 1)
        return "valueCodeableConcept", {"coding": [{"system": system, "code": code}]}
    kind = _value(parameter, "type")
    if kind == "duration":
        if not isinstance(value, str):
            raise ValueError(f"duration binding {name!r} must be an ISO duration")
        try:
            duration = parse_duration(value)
        except ExpressionError as exc:
            raise ValueError(f"invalid duration binding {name!r}: {exc}") from exc
        seconds = int(duration.total_seconds())
        if seconds % 86400 == 0:
            amount, unit, code = seconds // 86400, "day", "d"
        elif seconds % 3600 == 0:
            amount, unit, code = seconds // 3600, "hour", "h"
        elif seconds % 60 == 0:
            amount, unit, code = seconds // 60, "minute", "min"
        else:
            amount, unit, code = seconds, "second", "s"
        return "valueDuration", {
            "value": amount,
            "unit": unit,
            "system": "http://unitsofmeasure.org",
            "code": code,
        }
    if kind == "quantity":
        unit_code = _value(binding, "unit") or _value(parameter, "unit")
        if unit_code is None:
            raise ValueError(f"quantity binding {name!r} has no UCUM unit")
        unit_display = {"mm[Hg]": "mmHg"}.get(unit_code, unit_code)
        return "valueQuantity", {
            "value": value,
            "unit": unit_display,
            "system": "http://unitsofmeasure.org",
            "code": unit_code,
        }
    value_key = {
        "integer": "valueInteger",
        "decimal": "valueDecimal",
        "boolean": "valueBoolean",
        "string": "valueString",
    }.get(kind)
    if value_key is None:
        raise ValueError(f"unsupported bound parameter type {kind!r} for {name!r}")
    return value_key, value


@mlflow.trace(name="fhir.build_automation_document_reference")
def build_automation_docref(
    *, docref_id: str, task_id: str, patient_ref: str, careplan_ref: str,
    revision: str, name: str, template_id: str, bpmn_xml: str, now: str,
) -> dict:
    """Build the patient-free BPMN DocumentReference for one instance revision."""
    data = base64.b64encode(bpmn_xml.encode("utf-8")).decode("ascii")
    return {
        "resourceType": "DocumentReference",
        "id": docref_id,
        "meta": {"security": [aiast_security()]},
        "identifier": [{"system": AUTOMATION_REVISION_SYSTEM, "value": revision}],
        "status": "current",
        "type": {"coding": [{
            "system": AUTOMATION_DOCUMENT_TYPE_SYSTEM,
            "code": "bpmn-process-definition",
            "display": "BPMN 2.0 process definition",
        }]},
        "category": [{"coding": [{
            "system": AUTOMATION_DOCUMENT_CATEGORY_SYSTEM,
            "code": "care-plan-automation",
        }]}],
        "subject": {"reference": patient_ref},
        "date": now,
        "description": name,
        "content": [{
            "attachment": {
                "contentType": "application/xml",
                "data": data,
                "title": template_id,
                "creation": now,
            },
            "format": {
                "system": DOCUMENT_FORMAT_SYSTEM,
                "code": "bpmn-2.0-xml",
                "display": "BPMN 2.0 XML (Kogito profile)",
            },
        }],
        "context": {"related": [
            {"reference": careplan_ref},
            {"reference": f"urn:uuid:{task_id}"},
        ]},
    }


@mlflow.trace(name="fhir.build_automation_task")
def build_automation_task(
    *, task_id: str, automation: Any, name: str, patient_ref: str,
    careplan_ref: str, focus_ref: str | None, docref_ref: str, now: str,
) -> dict:
    """Build the Task containing all plan-specific automation values."""
    ir = _value(automation, "ir")
    acp = _value(_value(ir, "process"), "acp")
    parameters = {parameter.name: parameter for parameter in _value(acp, "parameters", [])}
    refs = _value(automation, "template_refs", []) or []
    template_extensions = []
    for ref in refs:
        template_fields = [
            {"url": "templateId", "valueString": _value(ref, "template_id")},
            {"url": "version", "valueString": _value(ref, "version")},
        ]
        source_cpg = _value(ref, "source_cpg")
        if source_cpg:
            template_fields.append({"url": "sourceCpg", "valueString": source_cpg})
        template_extensions.append({"url": AUTOMATION_TEMPLATE_EXT, "extension": template_fields})
    inputs = [{
        "type": {"coding": [{"system": AUTOMATION_INPUT_SYSTEM, "code": "process-definition"}]},
        "valueReference": {"reference": docref_ref},
    }]
    for binding in _value(automation, "bindings", []) or []:
        binding_name = _value(binding, "name")
        parameter = parameters.get(binding_name)
        if parameter is None and not str(binding_name).endswith("__code"):
            raise ValueError(f"automation binding {binding_name!r} has no matching IR parameter")
        value_key, value = _binding_value(parameter, binding)
        inputs.append({
            "type": {
                "coding": [{"system": AUTOMATION_INPUT_SYSTEM, "code": "parameter"}],
                "text": binding_name,
            },
            "extension": [{
                "url": AUTOMATION_PARAMETER_SOURCE_EXT,
                "valueCode": _value(binding, "source"),
            }],
            value_key: value,
        })
    task = {
        "resourceType": "Task",
        "id": task_id,
        "meta": {"security": [aiast_security()]},
        "identifier": [
            {"system": AUTOMATION_ID_SYSTEM, "value": _value(automation, "id")},
            {"system": AUTOMATION_REVISION_SYSTEM, "value": _value(automation, "revision")},
        ],
        "status": "draft",
        "intent": "plan",
        "code": {
            "coding": [{"system": AUTOMATION_TASK_CODE_SYSTEM, "code": "care-plan-automation"}],
            "text": name,
        },
        "for": {"reference": patient_ref},
        "basedOn": [{"reference": careplan_ref}],
        "authoredOn": now,
        "input": inputs,
    }
    if template_extensions:
        task["extension"] = template_extensions
    if focus_ref:
        task["focus"] = {"reference": focus_ref}
    return task


def activity_automation_extension(docref_ref: str, task_ref: str, status: str = "enabled") -> dict:
    """Return the project extension linking a CarePlan activity to its automation."""
    return {
        "url": ACTIVITY_AUTOMATION_EXT,
        "extension": [
            {"url": "definition", "valueReference": {"reference": docref_ref}},
            {"url": "instance", "valueReference": {"reference": task_ref}},
            {"url": "status", "valueCode": status},
        ],
    }


@mlflow.trace(name="fhir.build_automation_provenance")
def build_automation_provenance(
    *, prov_uid: str, careplan_ref: str, activity_indices: list[int], docref_ref: str,
    task_ref: str, device_ref: str, recorded: str, source_recommendation_ids: list[str],
    template_refs: list[Any], input_prompt_refs: list[str] | None = None,
) -> dict:
    entities: list[dict] = []
    for recommendation_id in sorted(set(source_recommendation_ids)):
        entities.append({
            "role": "source",
            "what": {"identifier": {"value": recommendation_id}},
        })
    for ref in template_refs:
        entities.append({
            "role": "derivation",
            "what": {"identifier": {
                "system": AUTOMATION_ID_SYSTEM,
                "value": f"{_value(ref, 'template_id')}@{_value(ref, 'version')}",
            }},
        })
    for prompt_ref in input_prompt_refs or []:
        entities.append({"role": "derivation", "what": {"reference": prompt_ref}})
    activity_targets = [
        {
            "reference": careplan_ref,
            "extension": [targetPath_ext(f"CarePlan.activity[{activity_index}]")],
        }
        for activity_index in activity_indices
    ]
    return build_ai_provenance(
        prov_uid=prov_uid,
        targets=[{"reference": docref_ref}, {"reference": task_ref}, *activity_targets],
        device_urn=device_ref,
        recorded=recorded,
        occurred=recorded,
        entities=entities or None,
    )


# Map the analyst's free-form confidence ("low"/"medium"/"high") onto the
# certainty-rating value set the AIconfidence categorical slice is bound to.
# "medium" has no certainty-rating code — it maps to "moderate".
_CONFIDENCE_TO_CERTAINTY = {
    "low": "low",
    "medium": "moderate",
    "moderate": "moderate",
    "high": "high",
    "very-low": "very-low",
}

# Inverse of the forward map above, used when reading conflicts back off a
# stored Provenance: the certainty-rating code is what got persisted, so
# "moderate" must resolve back to the analyst's "medium" (its only forward
# source). Codes that already match an analyst level pass through unchanged.
_CERTAINTY_TO_CONFIDENCE = {
    "very-low": "very-low",
    "low": "low",
    "moderate": "medium",
    "high": "high",
}


def ai_confidence_ext(level: str | None) -> dict | None:
    """AIconfidence extension (categorical) for a confidence level string.

    Returns ``None`` when ``level`` is empty/unmappable so callers can simply
    filter falsy values out of an extension list.
    """
    if not level:
        return None
    code = _CONFIDENCE_TO_CERTAINTY.get(level.strip().lower())
    if not code:
        return None
    return {
        "url": AICONFIDENCE_EXT,
        "valueCodeableConcept": {
            "coding": [{"system": CERTAINTY_RATING_CS, "code": code}],
        },
    }


def build_ai_device(device_uid: str, model_id: str | None, app_version: str | None = None) -> dict:
    """AI-Device resource for the acp-writer LLM.

    ``AIKind`` = Large-Language-Models; ``type`` = Artificial-Intelligence
    (fixed by the profile). ``version`` records both the app version and the
    model id so the specific model is traceable.
    """
    app_version = app_version or _app_version()
    model_id = model_id or "unknown"
    return {
        "resourceType": "Device",
        "id": device_uid,
        "meta": {
            "profile": [AI_DEVICE_PROFILE],
            "security": [aiast_security()],
        },
        "extension": [{
            "url": AIKIND_EXT,
            "valueCodeableConcept": {
                "coding": [{"system": DEVICE_TYPE_CS, "code": LLM_KIND_CODE}],
            },
        }],
        "manufacturer": "cpg-to-acp acp-writer",
        "type": {
            "coding": [{"system": DEVICE_TYPE_CS, "code": AI_ROOT_CODE}],
        },
        "deviceName": [{"name": "acp-writer", "type": "user-friendly-name"}],
        "version": [{"value": app_version}, {"value": model_id}],
    }


def prompts_capture_enabled() -> bool:
    """Whether rendered prompts should be captured as AI-InputPrompt DocRefs.

    Gated by ``ACP_CAPTURE_PROMPTS`` (default ``"true"`` everywhere). The system
    currently runs on synthetic data and full input-prompt traceability is the
    point of adopting the IG; rendered prompts embed patient data, so this flag
    MUST be revisited before the system ever touches real PHI.
    """
    return os.environ.get("ACP_CAPTURE_PROMPTS", "true").strip().lower() not in ("false", "0", "no")


def build_input_prompt_docref(docref_uid: str, prompt_text: str, description: str) -> dict:
    """AI-InputPrompt DocumentReference wrapping a rendered prompt (base64)."""
    data = base64.b64encode(prompt_text.encode("utf-8")).decode("ascii")
    return {
        "resourceType": "DocumentReference",
        "id": docref_uid,
        "meta": {
            "profile": [AI_INPUT_PROMPT_PROFILE],
            "security": [aiast_security()],
        },
        "status": "current",
        "type": {
            "coding": [{"system": AI_INPUTS_CS, "code": AI_INPUT_PROMPT_CODE}],
        },
        "description": description,
        "content": [{
            "attachment": {"contentType": "text/markdown", "data": data},
        }],
    }


def model_card_url() -> str | None:
    """The configured model-card URL, if any (``LLM_MODEL_CARD_URL``)."""
    url = os.environ.get("LLM_MODEL_CARD_URL", "").strip()
    return url or None


def build_model_card_docref(docref_uid: str, url: str) -> dict:
    """AI-ModelCard DocumentReference ("web" variant — attachment.url)."""
    return {
        "resourceType": "DocumentReference",
        "id": docref_uid,
        "meta": {
            "profile": [AI_MODEL_CARD_PROFILE],
            "security": [aiast_security()],
        },
        "status": "current",
        "type": {
            "coding": [{"system": AI_INPUTS_CS, "code": AI_MODEL_CARD_CODE}],
        },
        "content": [{
            "attachment": {"contentType": "text/html", "url": url},
        }],
    }


def ai_agent(device_urn: str) -> dict:
    """AI agent block for a Provenance: author + fixed AI role → the Device."""
    return {
        "type": {
            "coding": [{
                "system": PROVENANCE_PARTICIPANT_TYPE,
                "code": "author",
                "display": "Author",
            }],
        },
        "role": [{
            "coding": [{"system": DEVICE_TYPE_CS, "code": AI_ROOT_CODE}],
        }],
        "who": {"reference": device_urn},
    }


def build_ai_provenance(
    prov_uid: str,
    targets: list[dict],
    device_urn: str,
    recorded: str,
    occurred: str,
    entities: list[dict] | None = None,
    human_agents: list[dict] | None = None,
    extra_reasons: list[dict] | None = None,
) -> dict:
    """AI-Provenance resource.

    ``targets`` are fully-formed Provenance.target objects (each a Reference,
    optionally carrying a ``targetPath`` extension). ``entities`` are
    Provenance.entity objects. ``recorded``/``occurred`` are ISO timestamps.
    The AIAST reason slice and the AI agent (with fixed role) are always present.
    """
    reason: list[dict] = [{
        "coding": [{"system": AIAST_SYSTEM, "code": AIAST_CODE}],
    }]
    if extra_reasons:
        reason.extend(extra_reasons)

    agents: list[dict] = [ai_agent(device_urn)]
    if human_agents:
        agents.extend(human_agents)

    provenance: dict[str, Any] = {
        "resourceType": "Provenance",
        "id": prov_uid,
        "meta": {
            "profile": [AI_PROVENANCE_PROFILE],
            "security": [aiast_security()],
        },
        "target": targets,
        "occurredDateTime": occurred,
        "recorded": recorded,
        "reason": reason,
        "agent": agents,
    }
    if entities:
        provenance["entity"] = entities
    return provenance


def _conflict_extensions(conflict: Any) -> list[dict]:
    """Machine-readable conflict metadata as extensions on the Provenance.

    Read-back (WS6) uses these exclusively — never the note text.
    """
    exts: list[dict] = [
        {"url": f"{ACP_EXT_BASE}/conflict-id", "valueString": conflict.id},
        {"url": f"{ACP_EXT_BASE}/conflict-description", "valueString": conflict.description},
        {"url": f"{ACP_EXT_BASE}/conflict-severity", "valueCode": conflict.severity.value},
        {"url": f"{ACP_EXT_BASE}/conflict-category", "valueCode": conflict.category.value},
        {"url": f"{ACP_EXT_BASE}/conflict-status", "valueCode": conflict.status.value},
    ]
    suggested = getattr(conflict, "suggested_resolution", None)
    if suggested:
        exts.append({
            "url": f"{ACP_EXT_BASE}/conflict-suggested-resolution",
            "valueString": suggested,
        })
    # The clinician's applied resolution on a resolved conflict (F17c) — the
    # note that says what was done. Persisted so a resolved conflict reads back
    # with its resolution, not just a "resolved" status.
    resolution = getattr(conflict, "resolution", None)
    if resolution:
        exts.append({
            "url": f"{ACP_EXT_BASE}/conflict-resolution",
            "valueString": resolution,
        })
    confidence_ext = ai_confidence_ext(conflict.confidence)
    if confidence_ext:
        exts.append(confidence_ext)
    return exts


def _source_display(cpg_id: str, rec_id: str | None, excerpt: str | None) -> str:
    """Human-readable ConflictSource label. Round-trips via ``parse_source_display``."""
    display = f"CPG {cpg_id}"
    if rec_id:
        display += f" · rec {rec_id}"
    if excerpt:
        display += f" — {excerpt}"
    return display


def parse_source_display(display: str, rec_from_identifier: str | None = None) -> dict:
    """Inverse of ``_source_display`` → ``{cpgId, recommendationId?, excerpt?}``.

    ``rec_from_identifier`` (the entity's ``what.identifier.value``) is
    authoritative for the recommendation id when present.
    """
    excerpt: str | None = None
    base = display
    if " — " in base:
        base, excerpt = base.split(" — ", 1)
    rec = rec_from_identifier
    if " · rec " in base:
        cpg_part, rec_part = base.split(" · rec ", 1)
        cpg = cpg_part[4:] if cpg_part.startswith("CPG ") else cpg_part
        rec = rec or rec_part
    else:
        cpg = base[4:] if base.startswith("CPG ") else base
    src: dict = {"cpgId": cpg}
    if rec:
        src["recommendationId"] = rec
    if excerpt:
        src["excerpt"] = excerpt
    return src


def apply_approval_transition(bundle: dict, reviewer: "ReviewerContext") -> list[str]:
    """Apply the full clinician-approval transition to a care-plan bundle.

    This is the single source of truth for what "approved" means to the FHIR
    bundle. Both the monolith approve endpoint (``approve_care_plan``) and the
    deployed SonataFlow ``WriteFHIR`` path (``fhir_server_writer`` with
    ``approved=True``) call it, so the two paths can never drift — the split
    path previously only swapped security tags, silently dropping the human
    verifier and conflict acknowledgment (issue #169 F1).

    It performs the approval transitions in one pass:

    1. Swap every ``AIAST`` ``meta.security`` label → ``CLINAST_AIRPT``.
    2. Set the CarePlan ``status`` → ``active``.
    3. Append ``reviewer`` as a ``verifier`` Humanagent on every AI-Provenance.
    4. Flip automation Tasks from ``draft`` → ``requested``.
    5. Flip every conflict Provenance's ``conflict-status`` → ``acknowledged``.

    Returns the ``fullUrl`` of every entry it modified, so a future server-side
    persistence step (the phase-runner fast-follow, issue #174 F5) can PUT back
    exactly the resources that changed rather than the whole bundle.
    """
    conflict_status_url = f"{ACP_EXT_BASE}/conflict-status"
    verifier_who = reviewer.as_agent_who()
    modified: list[str] = []

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        touched = False

        security = resource.get("meta", {}).get("security", [])
        for i, sec in enumerate(security):
            if sec.get("code") == AIAST_CODE:
                security[i] = dict(CLINAST_AIRPT_SECURITY)
                touched = True

        if resource.get("resourceType") == "CarePlan":
            resource["status"] = "active"
            touched = True
        elif resource.get("resourceType") == "Task" and resource.get("status") == "draft":
            resource["status"] = "requested"
            touched = True

        if resource.get("resourceType") == "Provenance":
            profiles = resource.get("meta", {}).get("profile", [])
            if any("AI-Provenance" in p for p in profiles):
                resource.setdefault("agent", []).append({
                    "type": {
                        "coding": [{
                            "system": PROVENANCE_PARTICIPANT_TYPE,
                            "code": "verifier",
                            "display": "Verifier",
                        }],
                    },
                    "who": dict(verifier_who),
                })
                touched = True
            for ext in resource.get("extension", []):
                if ext.get("url") == conflict_status_url:
                    ext["valueCode"] = "acknowledged"
                    touched = True

        if touched and entry.get("fullUrl"):
            modified.append(entry["fullUrl"])

    return modified


def is_conflict_provenance(prov: dict) -> bool:
    """True if a Provenance carries the conflict-id extension (WS4 record)."""
    return any(
        e.get("url") == f"{ACP_EXT_BASE}/conflict-id"
        for e in prov.get("extension", [])
    )


@mlflow.trace(name="plan_conflict_from_provenance")
def plan_conflict_from_provenance(prov: dict) -> dict | None:
    """Reconstruct a BFF ``PlanConflict`` from a conflict Provenance.

    Uses the Provenance extensions exclusively for scalar metadata (never the
    note text) and the ``entity[]`` for sources.
    """
    exts = {e.get("url"): e for e in prov.get("extension", [])}
    id_ext = exts.get(f"{ACP_EXT_BASE}/conflict-id")
    if not id_ext:
        return None

    pc: dict = {
        "id": id_ext.get("valueString", ""),
        "description": exts.get(f"{ACP_EXT_BASE}/conflict-description", {}).get("valueString", ""),
    }
    sev = exts.get(f"{ACP_EXT_BASE}/conflict-severity", {}).get("valueCode")
    if sev:
        pc["severity"] = sev
    cat = exts.get(f"{ACP_EXT_BASE}/conflict-category", {}).get("valueCode")
    if cat:
        pc["category"] = cat
    status = exts.get(f"{ACP_EXT_BASE}/conflict-status", {}).get("valueCode")
    if status:
        pc["status"] = status
    suggested = exts.get(f"{ACP_EXT_BASE}/conflict-suggested-resolution", {}).get("valueString")
    if suggested:
        pc["suggestedResolution"] = suggested
    resolution = exts.get(f"{ACP_EXT_BASE}/conflict-resolution", {}).get("valueString")
    if resolution:
        pc["resolution"] = resolution
    conf = exts.get(AICONFIDENCE_EXT)
    if conf:
        coding = (conf.get("valueCodeableConcept", {}).get("coding") or [{}])[0]
        code = coding.get("code")
        if code:
            # The certainty-rating code is what got persisted; map it back to
            # the analyst's confidence vocabulary so a "medium" that was stored
            # as "moderate" reads back as "medium" (F8). Unknown codes pass
            # through unchanged.
            pc["confidence"] = _CERTAINTY_TO_CONFIDENCE.get(code, code)

    sources: list[dict] = []
    for e in prov.get("entity", []):
        if e.get("role") != "source":
            continue
        what = e.get("what", {})
        src_exts = {
            x.get("url"): x.get("valueString") for x in e.get("extension", [])
        }
        cpg_id = src_exts.get(f"{ACP_EXT_BASE}/conflict-source-cpg-id")
        if cpg_id:
            # Structural source fields (F9) are authoritative when present —
            # no display-string parsing, so a cpg id or excerpt containing the
            # display delimiters can't corrupt the round-trip.
            src: dict = {"cpgId": cpg_id}
            rec = src_exts.get(
                f"{ACP_EXT_BASE}/conflict-source-recommendation-id"
            ) or what.get("identifier", {}).get("value")
            if rec:
                src["recommendationId"] = rec
            excerpt = src_exts.get(f"{ACP_EXT_BASE}/conflict-source-excerpt")
            if excerpt:
                src["excerpt"] = excerpt
            sources.append(src)
        else:
            # Legacy Provenance without structural source extensions — fall
            # back to parsing the human-readable display string.
            rec = what.get("identifier", {}).get("value")
            sources.append(parse_source_display(what.get("display", ""), rec))
    if sources:
        pc["sources"] = sources
    return pc


@mlflow.trace(name="build_conflict_provenance")
def build_conflict_provenance(
    prov_uid: str,
    conflict: Any,
    careplan_urn: str,
    goal_urns: list[str],
    activity_urn_map: dict[int, str],
    device_urn: str,
    recorded: str,
    occurred: str,
) -> dict:
    """One AI-Provenance recording a single detected plan conflict.

    ``conflict`` is a ``ConflictEntry``. Affected activities that produced a
    standalone resource are targeted directly; inline (detail-only) activities
    are targeted via the CarePlan urn + a ``targetPath`` into
    ``CarePlan.activity[<index>].detail``. Affected goals are targeted directly.
    """
    targets: list[dict] = []
    for idx in conflict.activity_indices:
        uid_urn = activity_urn_map.get(idx)
        if uid_urn:
            targets.append({"reference": uid_urn})
        else:
            targets.append({
                "reference": careplan_urn,
                "extension": [targetPath_ext(f"CarePlan.activity[{idx}].detail")],
            })
    for idx in conflict.goal_indices:
        if 0 <= idx < len(goal_urns):
            targets.append({"reference": goal_urns[idx]})

    # Fall back to targeting the CarePlan as a whole if nothing resolved.
    if not targets:
        targets.append({"reference": careplan_urn})

    entities: list[dict] = []
    for src in conflict.sources:
        what: dict[str, Any] = {
            "display": _source_display(src.cpg_id, src.recommendation_id, src.excerpt),
        }
        if src.recommendation_id:
            what["identifier"] = {"value": src.recommendation_id}
        # Structural source fields (F9) so read-back never parses the display
        # string. The display is retained for human readers only.
        source_exts: list[dict] = [
            {"url": f"{ACP_EXT_BASE}/conflict-source-cpg-id", "valueString": src.cpg_id},
        ]
        if src.recommendation_id:
            source_exts.append({
                "url": f"{ACP_EXT_BASE}/conflict-source-recommendation-id",
                "valueString": src.recommendation_id,
            })
        if src.excerpt:
            source_exts.append({
                "url": f"{ACP_EXT_BASE}/conflict-source-excerpt",
                "valueString": src.excerpt,
            })
        entities.append({"role": "source", "what": what, "extension": source_exts})

    provenance = build_ai_provenance(
        prov_uid=prov_uid,
        targets=targets,
        device_urn=device_urn,
        recorded=recorded,
        occurred=occurred,
        entities=entities or None,
    )
    provenance["extension"] = _conflict_extensions(conflict)

    # Device-authored rationale note — human-readable only, never parsed on
    # read-back. WS5 appends the clinician note here later.
    if conflict.rationale:
        provenance["note"] = [{
            "authorReference": {"reference": device_urn},
            "text": conflict.rationale,
        }]
    return provenance
