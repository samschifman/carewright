"""Template registry validation, collision and version-retention tests."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp_writer import api
from acp_writer.services import decision_engine
from acp_writer.services.automation_registry import AutomationRegistry, RegistryError, router as registry_router
from acp_writer.services.template_client import TemplateClient
from cpg_contracts.automation.compiler import compile
from cpg_contracts.automation.ir import ProcessIR, ir_version
from cpg_contracts.automation.templates import AutomationTemplate
from cpg_contracts.decisions import DecisionModelSummary, DecisionVariable


GOLDEN = (
    Path(__file__).parent / "fixtures/home-bp-monitoring.template.json"
)


def _template(source_cpg: str = "SYN-HTN-2026-002") -> AutomationTemplate:
    data = AutomationTemplate.model_validate_json(GOLDEN.read_text()).model_dump(mode="python")
    data["summary"]["source_cpg"] = source_cpg
    return AutomationTemplate.model_validate(data)


def _next_version(template: AutomationTemplate, source_cpg: str | None = None) -> AutomationTemplate:
    data = template.model_dump(mode="python")
    data["ir"]["process"]["description"] += " Revised follow-up wording."
    data["summary"]["description"] = data["ir"]["process"]["description"]
    if source_cpg is not None:
        data["summary"]["source_cpg"] = source_cpg
    process_ir = ProcessIR.model_validate(data["ir"])
    data["summary"]["version"] = ir_version(process_ir)
    data["bpmn_xml"] = compile(process_ir)
    return AutomationTemplate.model_validate(data)


def _valid_kogito(_xml: str) -> dict:
    return {"valid": True, "messages": []}


def _register(registry: AutomationRegistry, template: AutomationTemplate, **kwargs):
    return registry.register(
        template,
        source_cpg=template.summary.source_cpg,
        bpmn_validator=_valid_kogito,
        **kwargs,
    )


def test_registers_valid_template_and_lists_latest_by_links():
    registry = AutomationRegistry()
    template = _template()

    result = _register(registry, template)

    assert result.status_code == 201
    assert registry.get(template.summary.id) == template
    assert registry.list(recommendation_id="<placeholder-rec-id>") == [template]
    assert registry.list(recommendation_id="unlinked") == []
    assert registry.list(dmn_model_id="unlinked") == []


def test_registration_captures_linked_dmn_metadata_and_template_client_exposes_it(monkeypatch):
    template = _template()
    template.summary.linked_decision_model_ids = ["monitoring-plan"]
    namespaces = {
        "monitoring-plan": "https://example.test/dmn/monitoring-plan",
        "unlinked-model": "https://example.test/dmn/unlinked",
    }
    summaries = {
        "monitoring-plan": DecisionModelSummary(
            id="monitoring-plan",
            name="Monitoring Plan",
            inputs=[DecisionVariable(name="has_kidney_disease", type="boolean")],
            outputs=[DecisionVariable(name="lab_timing_weeks", type="number")],
            namespace=namespaces["monitoring-plan"],
        ),
        "unlinked-model": DecisionModelSummary(
            id="unlinked-model",
            name="Unlinked",
            inputs=[],
            outputs=[],
            namespace=namespaces["unlinked-model"],
        ),
    }
    registered = _register(
        AutomationRegistry(),
        template,
        dmn_namespaces=namespaces,
        dmn_summaries=summaries,
    )

    assert registered.template.dmn_namespaces == {
        "monitoring-plan": namespaces["monitoring-plan"]
    }
    assert registered.template.dmn_summaries == {
        "monitoring-plan": summaries["monitoring-plan"]
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return registered.template.model_dump(mode="json")

    monkeypatch.setattr(
        "acp_writer.services.template_client.requests.get",
        lambda *_args, **_kwargs: Response(),
    )
    fetched = TemplateClient("http://registry").get(template.summary.id)

    assert fetched.dmn_namespaces == registered.template.dmn_namespaces
    assert fetched.dmn_summaries == registered.template.dmn_summaries


def test_ir_xml_mismatch_is_rejected_before_engine_validation():
    registry = AutomationRegistry()
    template = _template().model_copy(update={"bpmn_xml": "<bpmn/>"})
    called = False

    def validator(_xml):
        nonlocal called
        called = True
        return _valid_kogito(_xml)

    with pytest.raises(RegistryError) as exc:
        registry.register(template, source_cpg=template.summary.source_cpg, bpmn_validator=validator)

    assert exc.value.status_code == 422
    assert exc.value.code == "ir-xml-mismatch"
    assert not called


def test_missing_l5b_result_is_rejected():
    template = _template()
    data = template.model_dump(mode="python")
    data["summary"]["validation"]["rungs_passed"].remove("L5b")
    template = AutomationTemplate.model_validate(data)

    with pytest.raises(RegistryError) as exc:
        _register(AutomationRegistry(), template)

    assert exc.value.status_code == 422
    assert exc.value.code == "text-validation-missing"


def test_cross_cpg_collision_requires_replace_and_keeps_old_versions():
    registry = AutomationRegistry()
    first = _template("CPG-A")
    _register(registry, first)
    second = _next_version(first, "CPG-B")

    validation = _register(registry, second, validate_only=True)
    assert validation.validate_only
    assert validation.status_code == 200
    assert registry.get(first.summary.id) == first

    with pytest.raises(RegistryError) as exc:
        _register(registry, second)
    assert exc.value.status_code == 409
    assert exc.value.code == "template-id-collision"

    result = _register(registry, second, replace=True)

    assert result.status_code == 201
    assert registry.get(first.summary.id) == second
    assert registry.get(first.summary.id, first.summary.version) == first
    assert registry.get(first.summary.id, second.summary.version) == second


def test_validate_only_does_not_store_template():
    registry = AutomationRegistry()
    template = _template()

    result = _register(registry, template, validate_only=True)

    assert result.status_code == 200
    assert result.validate_only
    assert registry.get(template.summary.id) is None


def test_kogito_invalid_and_unavailable_results_fail_closed():
    template = _template()
    registry = AutomationRegistry()

    with pytest.raises(RegistryError) as failed:
        registry.register(
            template,
            source_cpg=template.summary.source_cpg,
            bpmn_validator=lambda _xml: {"valid": False, "messages": ["invalid"]},
        )
    assert failed.value.code == "kogito-validation-failed"

    with pytest.raises(RegistryError) as exc:
        registry.register(
            template,
            source_cpg=template.summary.source_cpg,
            bpmn_validator=lambda _xml: None,
        )
    assert exc.value.status_code == 503


def test_api_registers_lists_and_retrieves_template_versions(monkeypatch):
    registry = AutomationRegistry()
    monkeypatch.setattr(api, "_automation_registry", registry)
    monkeypatch.setattr(api, "validate_bpmn", _valid_kogito)
    monkeypatch.setattr(api.app.state, "automation_registry", registry)
    monkeypatch.setattr(api.app.state, "bpmn_validator", _valid_kogito)
    monkeypatch.setattr(
        api.app.state,
        "dmn_namespaces",
        lambda: {"monitoring-plan": "https://example.test/dmn/monitoring-plan"},
    )
    registered_summary = DecisionModelSummary(
        id="monitoring-plan",
        name="Monitoring Plan",
        inputs=[],
        outputs=[],
        namespace="https://example.test/dmn/monitoring-plan",
    )
    monkeypatch.setattr(api.app.state, "dmn_summaries", lambda: {"monitoring-plan": registered_summary})
    client = TestClient(api.app)
    template = _template()
    template.summary.linked_decision_model_ids = ["monitoring-plan"]

    registered = client.post(
        "/api/v1/automation/templates",
        params={"source_cpg": template.summary.source_cpg},
        json=template.model_dump(mode="json"),
    )
    assert registered.status_code == 201
    assert registered.json()["summary"]["id"] == template.summary.id

    listed = client.get(
        "/api/v1/automation/templates",
        params={"recommendation_id": "<placeholder-rec-id>"},
    )
    fetched = client.get(f"/api/v1/automation/templates/{template.summary.id}")

    assert [item["summary"]["version"] for item in listed.json()] == [template.summary.version]
    assert fetched.json()["summary"]["version"] == template.summary.version
    assert fetched.json()["dmn_namespaces"] == {
        "monitoring-plan": "https://example.test/dmn/monitoring-plan"
    }
    assert fetched.json()["dmn_summaries"] == {
        "monitoring-plan": registered_summary.model_dump(mode="json")
    }


def test_registry_routes_are_defined_once_and_mounted_by_both_services():
    expected = {
        ("POST", "/api/v1/automation/templates"),
        ("GET", "/api/v1/automation/templates"),
        ("GET", "/api/v1/automation/templates/{template_id}"),
        ("POST", "/api/v1/automation/validate"),
    }
    router_routes = {
        (method, route.path)
        for route in registry_router.routes
        for method in route.methods
        if method in {"GET", "POST"}
    }
    assert router_routes == expected
    for service_app in (api.app, decision_engine.app):
        mounted = {
            (method.upper(), path)
            for path, operations in service_app.openapi()["paths"].items()
            if path.startswith("/api/v1/automation/")
            for method in operations
            if method in {"get", "post"}
        }
        assert mounted == expected
