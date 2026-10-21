from io import BytesIO

import pytest

from cpg_contracts.artifact_paths import validated_run_artifact_key
from cpg_contracts.artifact_store import ArtifactStore


class _Client:
    def __init__(self):
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"Body": BytesIO(b"artifact")}


def test_get_raw_in_bucket_uses_configured_bucket_and_literal_key():
    store = ArtifactStore(endpoint_url="http://minio", bucket="cpg-phi")
    client = _Client()
    store._client = client

    data = store.get_raw_in_bucket("cpg-phi", "cpg-uploads:run-123/fhir_bundle.json")

    assert data == b"artifact"
    assert client.calls == [{
        "Bucket": "cpg-phi",
        "Key": "cpg-uploads:run-123/fhir_bundle.json",
    }]


def test_get_raw_in_bucket_rejects_a_different_bucket_before_client_access():
    store = ArtifactStore(endpoint_url="http://minio", bucket="cpg-phi")
    client = _Client()
    store._client = client

    with pytest.raises(ValueError):
        store.get_raw_in_bucket("cpg-uploads", "run-123/analysis_result.json")

    assert client.calls == []


def test_trusted_get_raw_still_reads_qualified_refs():
    store = ArtifactStore(endpoint_url="http://minio", bucket="cpg-phi")
    client = _Client()
    store._client = client

    data = store.get_raw("cpg-uploads:run-123/analysis_result.json")

    assert data == b"artifact"
    assert client.calls == [{
        "Bucket": "cpg-uploads",
        "Key": "run-123/analysis_result.json",
    }]


@pytest.mark.parametrize(
    ("run_id", "path"),
    [
        ("01234567-89ab-cdef-0123-456789abcdef", "fhir_bundle.json"),
        ("run-123", "nested/analysis_result.json"),
    ],
)
def test_validated_run_artifact_key_preserves_valid_components(run_id, path):
    assert validated_run_artifact_key(run_id, path) == f"{run_id}/{path}"


@pytest.mark.parametrize(
    ("run_id", "path"),
    [
        ("automation-publications", "careplan/jobs/job.json"),
        ("run-123", "automation-publications/careplan/jobs/job.json"),
        ("run:123", "artifact.json"),
        ("run/123", "artifact.json"),
        ("run-123", ""),
        ("run-123", "/leading.json"),
        ("run-123", "nested//artifact.json"),
        ("run-123", "./artifact.json"),
        ("run-123", "nested/../artifact.json"),
        ("run-123", "cpg-phi:other/key"),
        ("run-123", "nested/%2e%2e/artifact.json"),
        ("run-123", "nested\\artifact.json"),
        ("run-123", "nested/%252e%252e/artifact.json"),
        ("run-123", "line\nbreak.json"),
        ("r" * 129, "artifact.json"),
        ("é", "artifact.json"),
    ],
)
def test_validated_run_artifact_key_rejects_untrusted_components(run_id, path):
    with pytest.raises(ValueError):
        validated_run_artifact_key(run_id, path)
