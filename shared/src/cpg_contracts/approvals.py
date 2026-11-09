"""Canonical artifact revisions and tamper-evident reviewer approval records."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import urlsplit

from cpg_contracts.artifact_paths import validated_run_artifact_key
from cpg_contracts.tracing import trace_safe


@trace_safe
def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@trace_safe
def artifact_revision(bundle) -> str:
    return hashlib.sha256(canonical_json(bundle)).hexdigest()


@trace_safe
def normalize_ref(ref: str, default_bucket='cpg-phi') -> str:
    if not isinstance(ref, str) or not ref:
        raise ValueError('Artifact reference required')
    if ref.startswith('s3://'):
        url = urlsplit(ref)
        if url.query or url.fragment or url.username or url.password or url.port:
            raise ValueError('Invalid artifact reference')
        bucket, key = url.netloc, url.path.removeprefix('/')
    elif ':' in ref:
        bucket, key = ref.split(':', 1)
    else:
        bucket, key = default_bucket, ref
    if not bucket or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in bucket):
        raise ValueError('Invalid artifact bucket')
    run, separator, path = key.partition('/')
    if not separator:
        raise ValueError('Run-scoped artifact required')
    key = validated_run_artifact_key(run, path)
    return bucket + ':' + key


@trace_safe
def sign_record(record: dict, key: str, key_id='v1') -> dict:
    if not key:
        raise ValueError('Signing key unconfigured')
    signed = {k: v for k, v in record.items() if k != 'signature'}
    signed['signatureKeyId'] = key_id
    signed['signature'] = hmac.new(key.encode(), canonical_json(signed), hashlib.sha256).hexdigest()
    return signed


@trace_safe
def verify_record(record, key: str, key_id='v1') -> bool:
    if not key or not isinstance(record, dict) or record.get('signatureKeyId') != key_id:
        return False
    signature = record.get('signature')
    if not isinstance(signature, str):
        return False
    try:
        expected = sign_record(record, key, key_id)['signature']
        return hmac.compare_digest(signature.encode(), expected.encode())
    except (TypeError, ValueError):
        return False
