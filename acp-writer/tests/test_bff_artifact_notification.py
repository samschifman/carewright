"""The artifact notification route only reads published guideline artifacts."""

import pytest
from fastapi.testclient import TestClient

from acp_writer.services import bff

ROUTE = "/api/v1/notifications/artifacts-available"


class _Response:
    def raise_for_status(self):
        pass


class _RecordingStore:
    """An artifact store double that records every reference it is asked for."""

    def __init__(self):
        self.reads = []

    def get(self, ref):
        self.reads.append(ref)
        return {"cpg_id": "CPG-A", "recommendations": [], "summary": {"id": "t", "version": "1"}}

    def get_raw(self, ref):
        self.reads.append(ref)
        return b"<definitions/>"


@pytest.fixture
def wired(monkeypatch):
    store, posts = _RecordingStore(), []
    monkeypatch.setattr(bff, "_artifacts_store", store)
    monkeypatch.setattr(
        bff.http_requests, "post",
        lambda url, **kwargs: posts.append((url, kwargs)) or _Response(),
    )
    return TestClient(bff.app), store, posts


@pytest.mark.parametrize("ref", [
    "cpg-phi:run-1/ips_bundle.json",
    "cpg-phi:published/CPG-A/metadata.json",
    "s3://cpg-phi/run-1/ips_bundle.json",
    "s3://cpg-artifacts/published/CPG-A/metadata.json",
    "run-1/ips_bundle.json",
    "uploads/u1/file.pdf",
    "cpg-artifacts:uploads/u1/file.pdf",
    "published/../run-1/ips_bundle.json",
    "cpg-artifacts:published/CPG-A/../../x",
    "cpg-artifacts:published//metadata.json",
    "cpg-artifacts:published/CPG-A/",
    "cpg-artifacts:published/CPG-A/a\\b",
    "cpg-artifacts:published/CPG-A/a\nb",
    "published",
])
@pytest.mark.parametrize("art_type", ["metadata", "dmn", "recommendations", "bpmn"])
def test_reference_outside_published_area_is_refused_before_any_read(wired, ref, art_type):
    client, store, posts = wired

    response = client.post(ROUTE, json={"cpg_id": "CPG-A", "artifacts": [{"type": art_type, "ref": ref, "name": "m"}]})

    assert response.status_code == 200
    body = response.json()
    assert body["registered"] == []
    assert body["errors"] == [f"invalid-ref: {art_type}"]
    assert store.reads == []
    assert posts == []


def test_refused_reference_is_not_echoed_and_does_not_stop_valid_artifacts(wired, caplog):
    client, store, posts = wired

    response = client.post(ROUTE, json={"cpg_id": "CPG-A", "artifacts": [
        {"type": "not-a-type\nforged", "ref": "cpg-phi:run-1/ips_bundle.json"},
        {"type": "metadata", "ref": "published/CPG-A/metadata.json"},
    ]})

    body = response.json()
    assert body["errors"] == ["invalid-ref: unknown"]
    assert body["registered"] == [{"type": "metadata", "ref": "published/CPG-A/metadata.json"}]
    # A bare key under published/ is read from the published bucket, by name.
    assert store.reads == ["cpg-artifacts:published/CPG-A/metadata.json"]
    assert len(posts) == 1
    assert "ips_bundle" not in caplog.text and "forged" not in caplog.text


@pytest.mark.parametrize("register, args", [
    (bff._register_metadata, ()),
    (bff._register_dmn_model, ("m", "CPG-A")),
    (bff._register_recommendations, ("CPG-A",)),
    (bff._register_automation_template, ("CPG-A",)),
])
def test_registration_functions_refuse_other_buckets_themselves(wired, register, args):
    _, store, posts = wired

    with pytest.raises(ValueError):
        register("cpg-phi:run-1/ips_bundle.json", *args)

    assert store.reads == [] and posts == []


def test_guideline_id_is_one_query_value_and_cannot_add_parameters(wired):
    client, _, posts = wired
    hostile = "x&replace=true&validate_only=true#frag"

    response = client.post(ROUTE, json={"cpg_id": hostile, "artifacts": [
        {"type": "dmn", "ref": "cpg-artifacts:published/x/dmn/m.dmn", "name": "m"},
    ]})

    assert response.json()["errors"] == []
    url, kwargs = posts[0]
    assert url.endswith("/api/v1/decisions/models") and "?" not in url
    assert kwargs["params"] == {"source_cpg": hostile}


def test_guideline_id_reaches_the_decision_engine_without_replace(wired, monkeypatch):
    """End to end through a real HTTP encoder: the engine sees one parameter."""
    import requests
    from urllib.parse import parse_qs, urlsplit

    client, _, _ = wired
    seen = []

    def post(url, **kwargs):
        prepared = requests.Request("POST", url, params=kwargs.get("params"), data=kwargs.get("data")).prepare()
        seen.append(parse_qs(urlsplit(prepared.url).query))
        return _Response()

    monkeypatch.setattr(bff.http_requests, "post", post)
    client.post(ROUTE, json={"cpg_id": "x&replace=true", "artifacts": [
        {"type": "dmn", "ref": "cpg-artifacts:published/x/dmn/m.dmn", "name": "m"},
    ]})

    assert seen == [{"source_cpg": ["x&replace=true"]}]


def test_reload_without_a_guideline_id_sends_no_source_parameter(wired):
    _, _, posts = wired

    bff._register_dmn_model("cpg-artifacts:published/CPG-A/dmn/m.dmn", "m")

    assert posts[0][1]["params"] is None
