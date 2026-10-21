from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from cpg_ingester.services import bff


class _Store:
    def __init__(self, data=b"artifact", error=None):
        self.data = data
        self.error = error
        self.calls = []

    def get_raw_in_bucket(self, bucket, key):
        self.calls.append((bucket, key))
        if self.error:
            raise self.error
        return self.data


def _artifact_url(run_id, path):
    return (
        f"/api/v1/runs/{quote(run_id, safe='')}/artifacts/"
        f"{quote(path, safe='')}"
    )


def test_artifact_route_reads_only_from_upload_store(monkeypatch):
    store = _Store(data=b"analysis result")
    monkeypatch.setattr(bff, "_artifact_store", store)

    response = TestClient(bff.app).get(
        "/api/v1/runs/01234567-89ab-cdef-0123-456789abcdef/artifacts/analysis_result.json"
    )

    assert response.status_code == 200
    assert response.content == b"analysis result"
    assert store.calls == [("cpg-uploads", "01234567-89ab-cdef-0123-456789abcdef/analysis_result.json")]


@pytest.mark.parametrize(
    ("run_id", "path"),
    [
        ("run-123", "cpg-uploads:other/key"),
        ("run-123", "../secret.json"),
        ("run-123", "/leading.json"),
        ("run-123", "nested/%2e%2e/secret.json"),
        ("run-123", "nested/%252e%252e/secret.json"),
        ("automation-publications", "careplan/jobs/job.json"),
        ("run-123", "automation-publications/careplan/jobs/job.json"),
    ],
)
def test_invalid_artifact_paths_are_rejected_without_store_access(monkeypatch, run_id, path):
    store = _Store()
    monkeypatch.setattr(bff, "_artifact_store", store)

    response = TestClient(bff.app).get(_artifact_url(run_id, path))

    assert response.status_code == 400
    assert store.calls == []


def test_artifact_route_returns_503_without_upload_store(monkeypatch):
    monkeypatch.setattr(bff, "_artifact_store", None)

    response = TestClient(bff.app).get("/api/v1/runs/run-123/artifacts/analysis_result.json")

    assert response.status_code == 503


def test_artifact_route_returns_404_when_object_is_missing(monkeypatch):
    store = _Store(error=KeyError("missing"))
    monkeypatch.setattr(bff, "_artifact_store", store)

    response = TestClient(bff.app).get("/api/v1/runs/run-123/artifacts/analysis_result.json")

    assert response.status_code == 404
    assert store.calls == [("cpg-uploads", "run-123/analysis_result.json")]
