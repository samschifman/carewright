"""BFF registration and startup reload of published BPMN templates."""

from fastapi.testclient import TestClient

from acp_writer.services import bff


class _Response:
    def raise_for_status(self):
        pass


class _ArtifactStore:
    def __init__(self, value=None):
        self.value = value or {"summary": {"id": "bp-monitoring", "version": "a" * 12}}

    def get(self, _ref):
        return self.value


def test_notification_registers_bpmn_with_source_cpg(monkeypatch):
    calls = []
    monkeypatch.setattr(bff, "_artifacts_store", _ArtifactStore())
    monkeypatch.setattr(bff.http_requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)) or _Response())
    client = TestClient(bff.app)

    response = client.post(
        "/api/v1/notifications/artifacts-available",
        json={"cpg_id": "CPG-A", "artifacts": [{"type": "bpmn", "ref": "cpg-artifacts:published/CPG-A/bpmn/x/y.template.json"}]},
    )

    assert response.status_code == 200
    assert response.json()["registered"] == [{
        "type": "bpmn",
        "ref": "cpg-artifacts:published/CPG-A/bpmn/x/y.template.json",
    }]
    assert calls[0][0][0].endswith("/api/v1/automation/templates")
    assert calls[0][1]["params"] == {"source_cpg": "CPG-A"}


def test_startup_reload_registers_each_versioned_template(monkeypatch):
    class Client:
        def list_objects_v2(self, **kwargs):
            prefix = kwargs["Prefix"]
            if kwargs.get("Delimiter"):
                return {"CommonPrefixes": [{"Prefix": "published/CPG-A/"}]}
            if prefix.endswith("bpmn/"):
                return {"Contents": [
                    {"Key": "published/CPG-A/bpmn/tpl/111111111111.template.json"},
                    {"Key": "published/CPG-A/bpmn/tpl/222222222222.template.json"},
                    {"Key": "published/CPG-A/bpmn/tpl/latest.json"},
                ]}
            return {"Contents": []}

    class Store(_ArtifactStore):
        def _get_client(self):
            return Client()

        def get(self, ref):
            if ref.endswith("/latest.json"):
                return {"version": "111111111111"}
            return super().get(ref)

    registered = []
    monkeypatch.setattr(bff, "_artifacts_store", Store())
    monkeypatch.setattr(bff, "_register_metadata", lambda ref: None)
    monkeypatch.setattr(bff, "_register_dmn_model", lambda *args: None)
    monkeypatch.setattr(bff, "_register_recommendations", lambda *args: None)
    monkeypatch.setattr(bff, "_register_automation_template", lambda ref, cpg_id: registered.append((ref, cpg_id)))

    bff._reload_cpg_artifacts()

    assert registered == [
        ("cpg-artifacts:published/CPG-A/bpmn/tpl/222222222222.template.json", "CPG-A"),
        ("cpg-artifacts:published/CPG-A/bpmn/tpl/111111111111.template.json", "CPG-A"),
    ]
