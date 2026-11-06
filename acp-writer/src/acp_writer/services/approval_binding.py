"""The signed, artifact-bound entry point for publishing approved FHIR bundles."""
import json
import os
import re

from acp_writer.services.reviewer import ReviewerContext
from cpg_contracts.approvals import artifact_revision, normalize_ref, verify_record
from cpg_contracts.tracing import trace_safe
from fastapi import HTTPException


def _missing(exc):
    return isinstance(exc, KeyError) or getattr(exc, 'response', {}).get('Error', {}).get('Code') in {'NoSuchKey', 'NotFound', '404'}


@trace_safe
def write_approved_bundle(data: dict, store, writer) -> dict:
    key = os.getenv('APPROVAL_SIGNING_KEY', '')
    key_id = os.getenv('APPROVAL_SIGNING_KEY_ID', 'v1')
    if not key:
        raise HTTPException(503, detail={'error': 'signing-key-unconfigured'})
    approval_ref = data.get('approval_ref')
    if not approval_ref:
        raise HTTPException(403, detail={'error': 'approval-required'})
    try:
        normalized = normalize_ref(approval_ref)
        bucket, record_key = normalized.split(':', 1)
        if bucket != 'cpg-phi' or not re.fullmatch(r'[^/]+/approvals/(0|[1-9][0-9]*)\.json', record_key):
            raise ValueError('Invalid approval reference')
    except (TypeError, ValueError):
        raise HTTPException(400, detail={'error': 'approval-reference'}) from None
    if store is None:
        raise HTTPException(503, detail={'error': 'approval-store-unavailable'})
    try:
        record = json.loads(store.get_raw_in_bucket('cpg-phi', record_key))
    except Exception as exc:
        if _missing(exc) or isinstance(exc, (ValueError, TypeError)):
            raise HTTPException(403, detail={'error': 'approval-required'}) from None
        raise HTTPException(503, detail={'error': 'approval-store-unavailable'}) from None
    # Verify authenticity before using any record field to select a resource.
    if not verify_record(record, key, key_id):
        raise HTTPException(403, detail={'error': 'approval-required'})
    if record.get('decision') != 'approve':
        raise HTTPException(403, detail={'error': 'approval-required'})
    try:
        bundle_ref = normalize_ref(data.get('fhir_bundle_ref'))
        recorded_ref = normalize_ref(record.get('fhirBundleRef'))
        bundle_bucket, bundle_key = bundle_ref.split(':', 1)
    except (TypeError, ValueError):
        raise HTTPException(409, detail={'error': 'approval-mismatch'}) from None
    if (bundle_bucket != 'cpg-phi' or bundle_ref != recorded_ref
            or record.get('workflowInstanceId') != data.get('careplan_id')
            or record.get('runId') != record_key.split('/')[0]
            or str(record.get('reviewRound')) != record_key.split('/')[-1].removesuffix('.json')):
        raise HTTPException(409, detail={'error': 'approval-mismatch'})
    try:
        bundle = json.loads(store.get_raw_in_bucket('cpg-phi', bundle_key))
    except Exception as exc:
        if _missing(exc) or isinstance(exc, (ValueError, TypeError)):
            raise HTTPException(409, detail={'error': 'approval-mismatch'}) from None
        raise HTTPException(503, detail={'error': 'approval-store-unavailable'}) from None
    if not isinstance(bundle, dict) or artifact_revision(bundle) != record.get('artifactRevision'):
        raise HTTPException(409, detail={'error': 'approval-mismatch'})
    try:
        reviewer = ReviewerContext.model_validate(record.get('reviewer')).model_dump()
    except Exception:
        raise HTTPException(403, detail={'error': 'approval-required'}) from None
    plans = [entry.get('resource', {}) for entry in bundle.get('entry', []) if entry.get('resource', {}).get('resourceType') == 'CarePlan']
    patient_reference = plans[0].get('subject', {}).get('reference', '') if len(plans) == 1 else ''
    result = writer({'fhir_bundle': bundle, 'careplan_id': record['workflowInstanceId'],
                     'patient_reference': patient_reference, 'approved': True, 'reviewer': reviewer})
    return {**result, 'reviewRound': record['reviewRound'], 'artifactRevision': record['artifactRevision']}
