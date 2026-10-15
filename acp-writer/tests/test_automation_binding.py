"""Plan-specific automation binding behavior."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cpg_contracts.automation.instances import ParameterBinding
from cpg_contracts.automation.ir import (
    ConceptRef,
    Parameter,
    ProcessIR,
    SourceRef,
    TemplateRef,
    ir_version,
)
from acp_writer.nodes.automation_binding import bind_instance, known_phi_values


GOLDEN = (
    Path(__file__).parent / "fixtures/home-bp-monitoring.ir.json"
)


@pytest.fixture(autouse=True)
def mock_terminology_lookup(monkeypatch):
    def lookup(system, term):
        if term == "blood pressure":
            return SimpleNamespace(found=True, system=system, code="lookup-code")
        return SimpleNamespace(found=False, system=system, code=None)

    monkeypatch.setattr("acp_writer.nodes.automation_binding.find_terminology_concept", lookup)


def _template_and_instance(*, monitoring_period: str | None = None):
    template = ProcessIR.model_validate_json(GOLDEN.read_text())
    instance = template.model_copy(deep=True)
    template_id = template.process.id
    version = ir_version(template)
    instance.process.kind = "instance"
    instance.process.acp.template_refs = [TemplateRef(template_id=template_id, version=version)]
    if monitoring_period is not None:
        parameter = Parameter(
            name="monitoring_period",
            type="duration",
            default=monitoring_period,
            required=True,
            source=SourceRef(kind="cpg", source_text="monitoring continues for 30 days"),
        )
        template.process.acp.parameters.append(parameter)
        instance.process.acp.parameters.append(parameter.model_copy(deep=True))
        version = ir_version(template)
        instance.process.acp.template_refs = [TemplateRef(template_id=template_id, version=version)]
    for element in instance.process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=template_id,
            version=version,
            element_id=element.id,
        )
    return template, instance


def test_resolves_concepts_to_codes_and_plan_derived_bindings():
    template, instance = _template_and_instance()

    result, bindings, evidence, findings = bind_instance(
        instance,
        [template],
        activity={"id": "activity-1"},
        brief={},
        policy={"environment_default": "P90D"},
    )

    query = next(element for element in result.process.flowElements if element.id == "query_systolic")
    source_query = next(element for element in template.process.flowElements if element.id == "query_systolic")
    assert query.inputs == source_query.inputs
    request = next(element for element in result.process.flowElements if element.id == "request_readings")
    source_request = next(element for element in template.process.flowElements if element.id == "request_readings")
    assert request.inputs == source_request.inputs
    code_binding = next(binding for binding in bindings if binding.name == "query_systolic__code")
    assert code_binding.source == "plan-derived"
    assert code_binding.value == "http://loinc.org|8480-6"
    assert next(binding for binding in bindings if binding.name == "max_duration").value == "P90D"
    assert evidence["ev-monitoring-period"].payload["value"] == "P90D"
    assert not [finding for finding in findings if finding.code == "invalid-binding"]


def test_unresolved_concept_produces_incomplete_binding_finding():
    template, instance = _template_and_instance()
    request = next(element for element in instance.process.flowElements if element.id == "request_readings")
    request.inputs["code"] = ConceptRef(concept="unmapped clinical phrase")

    _, _, _, findings = bind_instance(
        instance,
        [template],
        activity={"id": "activity-1"},
        brief={},
        policy={"environment_default": "P90D"},
    )

    finding = next(item for item in findings if item.code == "incomplete-binding")
    assert "unmapped clinical phrase" in finding.message
    assert finding.element_id == "request_readings"


def test_monitoring_period_precedence_uses_separate_cpg_parameter_then_plan_then_policy(monkeypatch):
    template, instance = _template_and_instance(monitoring_period="P30D")
    _, bindings, _, _ = bind_instance(
        instance,
        [template],
        activity={"id": "activity-1"},
        brief={"careplan_period": "P60D"},
        policy={"environment_default": "P90D"},
    )
    assert next(binding for binding in bindings if binding.name == "max_duration").value == "P30D"
    assert next(binding for binding in bindings if binding.name == "max_duration").source == "cpg-default"

    template, instance = _template_and_instance()
    _, bindings, _, _ = bind_instance(
        instance,
        [template],
        activity={"id": "activity-1"},
        brief={"careplan_period": "P60D"},
        policy={"environment_default": "P90D"},
    )
    assert next(binding for binding in bindings if binding.name == "max_duration").value == "P60D"
    assert next(binding for binding in bindings if binding.name == "max_duration").source == "plan-derived"

    monkeypatch.setenv("ACP_AUTOMATION_DEFAULT_MONITORING_PERIOD", "P45D")
    _, bindings, _, _ = bind_instance(
        instance,
        [template],
        activity={"id": "activity-1"},
        brief={},
        policy={},
    )
    assert next(binding for binding in bindings if binding.name == "max_duration").value == "P45D"


def test_prior_clinician_parameter_binding_is_preserved():
    template, instance = _template_and_instance()
    prior = ParameterBinding(name="reporting_interval", value="P14D", source="clinician")

    _, bindings, _, _ = bind_instance(
        instance,
        [template],
        activity={"id": "activity-1"},
        brief={
            "prior_planning_brief": {
                "activities": [{
                    "id": "activity-1",
                    "automations": [{"bindings": [prior.model_dump()]}],
                }],
            },
        },
        policy={"environment_default": "P90D"},
    )

    assert next(binding for binding in bindings if binding.name == "reporting_interval") == prior


def test_known_phi_values_collect_names_identifiers_and_birth_date():
    assert known_phi_values({
        "name": "Ada Patient",
        "identifiers": [{"value": "patient-123"}],
        "birth_date": "1970-01-02",
        "gender": "female",
    }) == ["Ada Patient", "patient-123", "1970-01-02"]
