"""Receive, validate and store approved automation publications."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac
import inspect
import json
import logging
import os
import threading
from typing import Any

import mlflow
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.compiler import ParseError, parse
from cpg_contracts.automation.instances import PublicationPayload, PublishedAutomation
from cpg_contracts.automation.validators.catalog_conformance import (
    validate_catalog,
    validate_catalog_xml,
)
from cpg_contracts.automation.validators.lint import validate_structure
from cpg_contracts.automation.validators.phi import scan_phi
from cpg_contracts.automation.validators.preflight import validate_preflight
from cpg_contracts.automation.validators.xsd import validate_xsd

from automation_service.store import AutomationStore, safe_component

logger = logging.getLogger(__name__)
app = FastAPI(title="automation-service", version="0.1.0")
_store = AutomationStore()
_idempotency_lock = threading.Lock()
MAX_REQUEST_BYTES = int(os.getenv("AUTOMATION_MAX_REQUEST_BYTES", str(10 * 1024 * 1024)))
app.state.max_request_bytes = MAX_REQUEST_BYTES


class RequestSizeLimitMiddleware:
    """Enforce a byte limit on caller-supplied automation publications."""

    def __init__(self, downstream):
        self.downstream = downstream

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path") != "/api/v1/automations"):
            await self.downstream(scope, receive, send)
            return

        limit = int(getattr(app.state, "max_request_bytes", MAX_REQUEST_BYTES))
        content_length = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    await self._too_large(send, limit)
                    return
            except ValueError:
                await self._too_large(send, limit)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > limit:
                await self._too_large(send, limit)
                return
            if not message.get("more_body", False):
                break

        first_message = {"type": "http.request", "body": bytes(body), "more_body": False}
        body_consumed = False

        async def replay_body():
            nonlocal body_consumed
            if body_consumed:
                return {"type": "http.request", "body": b"", "more_body": False}
            body_consumed = True
            return first_message

        await self.downstream(scope, replay_body, send)

    @staticmethod
    async def _too_large(send, limit: int):
        body = json.dumps({"detail": f"request body exceeds {limit} bytes"}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(RequestSizeLimitMiddleware)


def _require_automation_token(
    provided_token: str | None = Header(default=None, alias="X-Automation-Token"),
) -> None:
    configured_token = os.getenv("AUTOMATION_SERVICE_TOKEN", "")
    if not configured_token.strip():
        raise HTTPException(
            status_code=503,
            detail="automation authentication is not configured",
        )
    if provided_token is None or not hmac.compare_digest(
        provided_token.encode("utf-8"), configured_token.strip().encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid automation token")


def _trace_handler(name: str, *identifier_fields: str):
    """Trace only selected identifiers; do not capture request or response bodies."""
    def decorate(handler):
        signature = inspect.signature(handler)

        @wraps(handler)
        def traced(*args, **kwargs):
            bound = signature.bind_partial(*args, **kwargs)
            values = bound.arguments
            payload = values.get("payload")
            attributes = {}
            for field in identifier_fields:
                value = getattr(payload, field, None) if payload is not None else values.get(field)
                if isinstance(value, str):
                    attributes[field] = value
            with mlflow.start_span(name=name, attributes=attributes):
                return handler(*args, **kwargs)

        return traced

    return decorate


def _utc_datetime(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mark_records_superseded(careplan_id: str, records: list[dict[str, Any]], *, replaced_by: str | None = None):
    for record in records:
        if record.get("status") == "superseded":
            continue
        updated = {**record, "status": "superseded", "superseded_at": datetime.now(timezone.utc).isoformat()}
        if replaced_by:
            updated["replaced_by_careplan_id"] = replaced_by
        _store.put_record(
            careplan_id,
            record["automation_id"],
            record["revision"],
            updated,
            record.get("bpmn_xml", ""),
        )


def _decode_document_bpmn(document_reference: dict[str, Any]) -> str:
    if document_reference.get("resourceType") != "DocumentReference":
        raise ValueError("document_reference.resourceType must be DocumentReference")
    if document_reference.get("status") not in {"current", "superseded"}:
        raise ValueError("DocumentReference.status must be current")
    content = document_reference.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("DocumentReference.content must contain a BPMN attachment")
    for item in content:
        attachment = item.get("attachment") if isinstance(item, dict) else None
        if not isinstance(attachment, dict):
            continue
        if attachment.get("contentType") not in {"application/xml", "application/bpmn+xml"}:
            continue
        encoded = attachment.get("data")
        if not isinstance(encoded, str):
            continue
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("BPMN attachment data must be valid base64 UTF-8") from exc
    raise ValueError("DocumentReference must contain a base64 BPMN XML attachment")


def _schema_errors(publication: PublishedAutomation) -> list[str]:
    errors = []
    task = publication.task
    document = publication.document_reference
    if task.get("resourceType") != "Task":
        errors.append("task.resourceType must be Task")
    if task.get("status") not in {"requested", "draft"}:
        errors.append("task.status must be requested or draft")
    if task.get("intent") != "plan":
        errors.append("task.intent must be plan")
    if not isinstance(task.get("for"), dict) or not task["for"].get("reference"):
        errors.append("task.for.reference is required")
    inputs = task.get("input")
    if not isinstance(inputs, list):
        errors.append("task.input must be an array")
    elif not any(_is_process_definition_input(item) for item in inputs):
        errors.append("task.input must include a process-definition reference")
    if document.get("resourceType") != "DocumentReference":
        errors.append("document_reference.resourceType must be DocumentReference")
    if document.get("status") not in {"current", "superseded"}:
        errors.append("document_reference.status must be current")
    if not isinstance(document.get("content"), list) or not document["content"]:
        errors.append("document_reference.content is required")
    return errors


def _is_process_definition_input(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    type_data = item.get("type")
    type_data = type_data if isinstance(type_data, dict) else {}
    codings = type_data.get("coding")
    codings = codings if isinstance(codings, list) else []
    has_type = any(
        isinstance(coding, dict) and coding.get("code") == "process-definition"
        for coding in codings
    )
    reference = item.get("valueReference")
    return has_type and isinstance(reference, dict) and bool(reference.get("reference"))


def _validate_l1_l4(bpmn_xml: str) -> list[dict[str, Any]]:
    findings = validate_preflight(bpmn_xml)
    if any(item.severity == "ERROR" for item in findings):
        return [item.__dict__ for item in findings]
    findings.extend(validate_xsd(bpmn_xml))
    if any(item.severity == "ERROR" for item in findings):
        return [item.__dict__ for item in findings]
    findings.extend(validate_structure(bpmn_xml))
    findings.extend(scan_phi(bpmn_xml))
    try:
        process_ir = parse(bpmn_xml)
    except (ParseError, ValueError) as exc:
        return [*([item.__dict__ for item in findings]), {
            "rung": "L0", "severity": "ERROR", "code": "xml-ir-parse",
            "message": str(exc), "element_id": None,
        }]
    catalog = load_catalog()
    findings.extend(validate_catalog(process_ir, catalog))
    findings.extend(validate_catalog_xml(bpmn_xml, catalog))
    return [item.__dict__ for item in findings]


def _publication_record(
    payload: PublicationPayload,
    item: PublishedAutomation,
    bpmn_xml: str,
) -> dict[str, Any]:
    return {
        "careplan_id": payload.careplan_id,
        "careplan_server_id": payload.careplan_server_id,
        "patient_server_id": payload.patient_server_id,
        "automation_id": item.automation_id,
        "activity_ids": item.activity_ids,
        "revision": item.revision,
        "template_refs": [ref.model_dump(mode="json") for ref in item.template_refs],
        "task": item.task,
        "document_reference": item.document_reference,
        "bpmn_xml": bpmn_xml,
        "approved_at": payload.approved_at.isoformat(),
        "reviewer": payload.reviewer,
        "status": "active",
    }


@app.get("/health")
def health():
    return {"status": "UP", "service": "automation"}


@app.post("/api/v1/automations", dependencies=[Depends(_require_automation_token)])
@_trace_handler("automation.publish", "job_id", "careplan_id")
def publish_automations(
    payload: PublicationPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Validate a complete approved payload before storing its automations."""
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if idempotency_key != payload.job_id:
        raise HTTPException(status_code=400, detail="Idempotency-Key must equal job_id")
    try:
        safe_component(payload.careplan_id)
        safe_component(payload.job_id)
        if payload.replaces_careplan_id:
            safe_component(payload.replaces_careplan_id)
        for item in payload.automations:
            safe_component(item.automation_id)
            safe_component(item.revision)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with _idempotency_lock:
        return _publish_automations_locked(payload)


def _publish_automations_locked(payload: PublicationPayload):
    payload_hash = hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if _store.get_withdrawal(payload.careplan_id) is not None:
        return JSONResponse(status_code=409, content={
            "error": "careplan-withdrawn",
            "detail": "the CarePlan has a withdrawal tombstone",
        })

    previous = _store.get_idempotency(payload.job_id)
    if previous is not None:
        if previous.get("payload_hash") != payload_hash:
            return JSONResponse(status_code=409, content={
                "error": "idempotency-conflict",
                "detail": "job_id was already used with a different publication payload",
            })
        return JSONResponse(status_code=200, content=previous["response"])

    existing_records = _store.list_records(payload.careplan_id)
    approved_at = _utc_datetime(payload.approved_at)
    if any(_utc_datetime(record["approved_at"]) > approved_at for record in existing_records if record.get("approved_at")):
        return JSONResponse(status_code=409, content={
            "error": "stale-approval",
            "detail": "approved_at is older than a publication already stored for this CarePlan",
        })

    validated: list[tuple[PublishedAutomation, str]] = []
    errors = []
    for item in payload.automations:
        item_errors = _schema_errors(item)
        try:
            bpmn_xml = _decode_document_bpmn(item.document_reference)
        except ValueError as exc:
            bpmn_xml = ""
            item_errors.append(str(exc))
        if bpmn_xml:
            findings = _validate_l1_l4(bpmn_xml)
            item_errors.extend(
                f"{finding['rung']} {finding['code']}: {finding['message']}"
                for finding in findings
                if finding["severity"] == "ERROR"
            )
        if item_errors:
            errors.append({"automation_id": item.automation_id, "errors": item_errors})
        else:
            validated.append((item, bpmn_xml))
    if errors:
        raise HTTPException(status_code=422, detail={"automations": errors})

    now = datetime.now(timezone.utc).isoformat()
    for item, bpmn_xml in validated:
        record = _publication_record(payload, item, bpmn_xml)
        record["stored_at"] = now
        _store.put_record(
            payload.careplan_id, item.automation_id, item.revision, record, bpmn_xml
        )

    published_ids = {item.automation_id for item, _ in validated}
    absent = [record for record in existing_records if record.get("automation_id") not in published_ids]
    _mark_records_superseded(payload.careplan_id, absent)
    if payload.replaces_careplan_id:
        replaced_records = _store.list_records(payload.replaces_careplan_id)
        _mark_records_superseded(
            payload.replaces_careplan_id,
            replaced_records,
            replaced_by=payload.careplan_id,
        )
    response = {
        "job_id": payload.job_id,
        "careplan_id": payload.careplan_id,
        "status": "accepted",
        "automations": [
            {"automation_id": item.automation_id, "revision": item.revision}
            for item, _ in validated
        ],
    }
    _store.put_idempotency(payload.job_id, {"payload_hash": payload_hash, "response": response})
    return JSONResponse(status_code=201, content=response)


@app.get("/api/v1/automations")
@_trace_handler("automation.list", "careplan_id")
def list_automations(careplan_id: str):
    try:
        records = _store.list_records(careplan_id)
        marker = _store.get_withdrawal(careplan_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if marker is not None:
        records = [{**record, "status": "withdrawn"} for record in records]
    return records


@app.get("/api/v1/automations/{careplan_id}/{automation_id}")
@_trace_handler("automation.get", "careplan_id")
def get_automation(careplan_id: str, automation_id: str, revision: str | None = None):
    try:
        record = _store.get_record(careplan_id, automation_id, revision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    if _store.get_withdrawal(careplan_id) is not None:
        record = {**record, "status": "withdrawn"}
    return record


@app.post(
    "/api/v1/automations/{careplan_id}/withdraw",
    dependencies=[Depends(_require_automation_token)],
)
@_trace_handler("automation.withdraw", "careplan_id")
def withdraw_automations(careplan_id: str):
    try:
        safe_component(careplan_id)
        marker = _store.get_withdrawal(careplan_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _idempotency_lock:
        if marker is not None:
            return JSONResponse(status_code=200, content=marker)
        marker = {
            "careplan_id": careplan_id,
            "status": "withdrawn",
            "withdrawn_at": datetime.now(timezone.utc).isoformat(),
        }
        _store.put_withdrawal(careplan_id, marker)
        return JSONResponse(status_code=201, content=marker)
