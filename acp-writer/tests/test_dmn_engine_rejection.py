"""Unsafe-DMN engine responses must survive both deployment entry points."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from acp_writer import api
from acp_writer.services.decision_engine import app as decision_engine_app

DMN = '''<definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
    id="unsafe" name="Unsafe" namespace="urn:test:unsafe">
  <decision id="decision" name="Result"><variable name="Result"/>
    <literalExpression><text>1</text></literalExpression>
  </decision>
</definitions>'''
REJECTION = {
    "error": "unsupported-dmn-feature",
    "messages": ["functionDefinition kind=JAVA at unsafe_function"],
}


@pytest.fixture
def rejected_engine(monkeypatch):
    response = Mock(status_code=400, text="unused")
    response.json.return_value = REJECTION
    post = Mock(return_value=response)
    monkeypatch.setattr(api.requests, "post", post)
    monkeypatch.setattr(api, "_dynamic_models", {})
    # Both services share the same registry object.
    from acp_writer.services import decision_engine
    monkeypatch.setattr(decision_engine, "_dynamic_models", api._dynamic_models)
    return post


def test_engine_feature_rejection_preserves_messages(rejected_engine):
    assert api._validate_dmn_with_engine(DMN) == {"valid": False, **REJECTION}
    rejected_engine.assert_called_once()


@pytest.mark.parametrize("app", [api.app, decision_engine_app])
@pytest.mark.parametrize("query", ["", "?validate_only=true", "?replace=true"])
def test_deployment_refuses_unsafe_model_without_storing(app, query, rejected_engine):
    response = TestClient(app).post("/api/v1/decisions/models" + query, content=DMN)
    assert response.status_code == 400
    assert response.json() == REJECTION
    assert api._dynamic_models == {}


def test_feature_rejection_does_not_replace_an_existing_model(rejected_engine):
    previous = {"dmn_xml": "previous model"}
    api._dynamic_models["unsafe"] = previous
    response = TestClient(api.app).post("/api/v1/decisions/models?replace=true", content=DMN)
    assert response.status_code == 400
    assert api._dynamic_models["unsafe"] is previous
