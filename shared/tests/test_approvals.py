import hashlib
import hmac

import pytest
from cpg_contracts.approvals import (
    artifact_revision,
    canonical_json,
    normalize_ref,
    sign_record,
    verify_record,
)


def test_canonical_revision_and_hmac():
    assert canonical_json({'b': 2, 'a': 1}) == b'{"a":1,"b":2}'
    assert artifact_revision({'b': 2, 'a': 1}) == artifact_revision({'a': 1, 'b': 2})
    record = {'runId': 'synthetic'}
    signed = sign_record(record, 'test-signing-key')
    expected = hmac.new(b'test-signing-key', b'{"runId":"synthetic","signatureKeyId":"v1"}', hashlib.sha256).hexdigest()
    assert signed['signature'] == expected
    assert verify_record(signed, 'test-signing-key')
    assert not verify_record({**signed, 'runId': 'another'}, 'test-signing-key')
    assert not verify_record(signed, 'another-key')
    assert not verify_record(signed, 'test-signing-key', 'v2')
    assert not verify_record(record, 'test-signing-key')
    assert 'signature' not in record
    with pytest.raises(ValueError): sign_record(record, '')


@pytest.mark.parametrize('ref', ['run/bundle.json', 'cpg-phi:run/bundle.json', 's3://cpg-phi/run/bundle.json'])
def test_normalize_reference(ref):
    assert normalize_ref(ref) == 'cpg-phi:run/bundle.json'


@pytest.mark.parametrize('ref', ['', 'cpg-phi:run/../other', 's3://user:pass@bucket/run/file', 'cpg-phi:/absolute', 'cpg-phi:run/%2e%2e/secret', 'cpg-phi:automation-publications/file'])
def test_bad_references(ref):
    with pytest.raises(ValueError): normalize_ref(ref)
