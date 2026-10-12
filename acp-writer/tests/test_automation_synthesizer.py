"""The slice synthesizer keeps template behavior and gates enablement."""

from pathlib import Path
from types import SimpleNamespace
import json

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import Mock

from cpg_contracts.automation.instances import ActivityAutomation
from cpg_contracts.automation.ir import Parameter, ProcessIR, TemplateRef, ir_version
from cpg_contracts.automation.templates import AutomationTemplate
from acp_writer.nodes.automation_synthesizer import _validation_payload, synthesize_for_activity
from cpg_contracts.automation.validators.grounding import InstanceContext, validate_instance
from cpg_contracts.decisions import DecisionVariable
from acp_writer import api


GOLDEN = (
    Path(__file__).parent / "fixtures/home-bp-monitoring.template.json"
)


class Registry:
    def __init__(self, templates):
        self.templates = templates

    def list(self, *, recommendation_id=None, dmn_model_id=None):
        return [
            template for template in self.templates
            if recommendation_id in template.summary.linked_recommendation_ids
        ]


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        response = self.responses.pop(0)
        return SimpleNamespace(content=json.dumps(response) if isinstance(response, dict) else response)


def _instance(template):
    ir = template.ir.model_copy(deep=True)
    process = ir.process
    process.id = "instance_home_bp"
    process.kind = "instance"
    process.acp.template_refs = [TemplateRef(
        template_id=template.summary.id,
        version=template.summary.version,
        source_cpg=template.summary.source_cpg,
    )]
    return ir


def _template_with_parameter(name: str, *, required: bool, default=None):
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    template_ir = template.ir.model_copy(deep=True)
    template_ir.process.acp.parameters.append(Parameter(
        name=name,
        type="duration",
        required=required,
        default=default,
    ))
    version = ir_version(template_ir)
    template.summary.version = version
    template.summary.parameters = list(template_ir.process.acp.parameters)
    template.ir = template_ir
    return template


def _output(
    template,
    *,
    rename: bool = False,
    add_second_ref: bool = False,
    wrong_reference_versions: bool = False,
):
    ir = _instance(template)
    for element in ir.process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=template.summary.id,
            version=template.summary.version,
            element_id=element.id,
        )
    if rename:
        next(item for item in ir.process.flowElements if item.id == "query_systolic").name = "Invented node name"
    if add_second_ref:
        ir.process.acp.template_refs.append(TemplateRef(template_id="other", version="0" * 12))
    result = ir.model_dump(mode="json", exclude_none=True)
    process = result["process"]
    process.pop("id", None)
    process.pop("kind", None)
    process["acp"].pop("template_refs", None)
    process["acp"]["provenance"].pop("source_cpg", None)
    for element in process["flowElements"]:
        ref = element.get("acp", {}).get("template_ref")
        if ref:
            ref.pop("version", None)
            ref.pop("source_cpg", None)
    if wrong_reference_versions:
        process["acp"]["template_refs"] = [{
            "template_id": template.summary.id,
            "version": "1.0",
        }]
        query = next(item for item in process["flowElements"] if item["id"] == "query_systolic")
        query["acp"]["template_ref"]["version"] = "1.0"
    elif add_second_ref:
        process["acp"]["template_refs"] = [
            {"template_id": template.summary.id, "version": template.summary.version},
            {"template_id": "other", "version": "0" * 12},
        ]
    return {"ir": result, "bindings": []}


def _activity():
    return {
        "id": "activity-1",
        "type": "monitoring",
        "description": "Monitor home blood pressure readings for Ada Patient.",
        "source_recommendation_id": "<placeholder-rec-id>",
        "source_cpg": "SYN-HTN-2026-002",
    }


def _synthesize(
    template,
    llm,
    *,
    reviewer=None,
    use_llm_reviewer=False,
    state=None,
    validator=None,
):
    return synthesize_for_activity(
        _activity(),
        Registry([template]),
        [],
        brief={"dmn_audit_trail": []},
        state=state or {"patient_demographics": {"name": "Ada Patient"}},
        llm=llm,
        validator=validator or (lambda payload: {"valid": True, "rungs_passed": ["L6"]}),
        reviewer=reviewer if use_llm_reviewer else (reviewer or (lambda **_: (True, ""))),
    )


def test_golden_derived_instance_passes_static_and_semantic_checks():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template), {"verdict": "APPROVE"}])

    automations = _synthesize(template, llm, use_llm_reviewer=True)

    assert len(automations) == 1
    automation = automations[0]
    assert isinstance(automation, ActivityAutomation)
    assert automation.enabled is True
    assert automation.validation.status == "valid"
    assert automation.validation.rungs_passed == ["L0", "L1", "L2", "L3", "L4", "L5a", "L6", "L7"]
    assert len(llm.prompts) == 2
    assert "Ada Patient" not in str(llm.prompts)


def test_engine_rejection_with_messages_is_a_repairable_l6_finding():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template), _output(template)])
    results = iter([
        {"valid": False, "messages": [{"severity": "ERROR", "text": "engine rejected BPMN"}]},
        {"valid": True, "messages": []},
    ])

    automations = _synthesize(template, llm, validator=lambda _payload: next(results))

    assert automations[0].validation.status == "valid"
    assert len(llm.prompts) == 2
    assert "kogito-validation" in str(llm.prompts[1])


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("engine connection failed"),
        requests.HTTPError(
            "engine returned HTTP 503",
            response=Mock(status_code=503),
        ),
        HTTPException(status_code=503, detail="engine returned HTTP 503"),
    ],
)
def test_engine_transport_failure_escalates_without_another_model_call(error):
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template)])

    def unavailable(_payload):
        raise error

    automations = _synthesize(template, llm, validator=unavailable)

    assert automations[0].validation.status == "escalated"
    assert automations[0].enabled is False
    assert any("engine" in warning for warning in automations[0].validation.warnings)
    assert not automations[0].validation.errors
    assert len(llm.prompts) == 1


def test_unexpected_validator_exception_fails_loudly():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template)])

    def broken(_payload):
        raise AttributeError("validator implementation bug")

    with pytest.raises(AttributeError, match="validator implementation bug"):
        _synthesize(template, llm, validator=broken)

    assert len(llm.prompts) == 1


def test_replay_dmn_registration_normalizes_nested_variables_before_instance_validation(monkeypatch):
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    models = {}
    monkeypatch.setattr(api, "_dynamic_models", models)
    parse_dmn = api._parse_dmn_metadata

    def replay_shaped_summary(dmn_xml):
        summary = parse_dmn(dmn_xml)
        # Reproduce a deserialized DecisionModelSummary whose nested variables
        # were left as dictionaries by the artifact/replay path.
        summary.inputs = [item.model_dump(mode="python") for item in summary.inputs]
        summary.outputs = [item.model_dump(mode="python") for item in summary.outputs]
        return summary

    monkeypatch.setattr(api, "_parse_dmn_metadata", replay_shaped_summary)
    monkeypatch.setattr(api, "_validate_dmn_with_engine", lambda _xml: None)
    engine_result = Mock(return_value={"valid": True, "messages": []})
    monkeypatch.setattr(api, "validate_bpmn", engine_result)
    client = TestClient(api.app)

    dmn_fixtures = Path(__file__).parent / "fixtures/dmn"
    for name in (
        "home-bp-blood-pressure-classification.dmn",
        "home-bp-treatment-recommendation.dmn",
        "home-bp-monitoring-plan.dmn",
    ):
        response = client.post(
            "/api/v1/decisions/models?source_cpg=SYN-HTN-2026-002",
            content=(dmn_fixtures / name).read_text(),
        )
        assert response.status_code == 201

    summaries = api._automation_dmn_summaries()
    assert len(summaries) == 3
    assert all(
        isinstance(variable, DecisionVariable)
        for summary in summaries.values()
        for variable in (*summary.inputs, *summary.outputs)
    )
    assert len(api._automation_dmn_namespaces()) == 3

    llm = ScriptedLLM([_output(template), {"verdict": "APPROVE"}])
    automations = _synthesize(
        template,
        llm,
        use_llm_reviewer=True,
        validator=api._automation_validation_result,
    )

    assert automations[0].validation.status == "valid"
    assert "L6" in automations[0].validation.rungs_passed
    assert engine_result.call_count == 1
    assert len(llm.prompts) == 2


def test_wrong_model_template_versions_are_replaced_from_selected_snapshot():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template, wrong_reference_versions=True), {"verdict": "APPROVE"}])

    automations = _synthesize(template, llm, use_llm_reviewer=True)

    assert len(automations) == 1
    automation = automations[0]
    expected_version = ir_version(template.ir)
    assert automation.validation.status == "valid"
    assert automation.ir.process.id == automation.id
    assert automation.ir.process.kind == "instance"
    assert automation.ir.process.acp.provenance.source_cpg == template.summary.source_cpg
    assert [(ref.template_id, ref.version) for ref in automation.template_refs] == [
        (template.summary.id, expected_version),
    ]
    assert [(ref.template_id, ref.version) for ref in automation.ir.process.acp.template_refs] == [
        (template.summary.id, expected_version),
    ]
    query = next(item for item in automation.ir.process.flowElements if item.id == "query_systolic")
    assert query.acp.template_ref.version == expected_version
    assert all(
        element.acp.template_ref is None
        or (
            element.acp.template_ref.template_id == template.summary.id
            and element.acp.template_ref.version == expected_version
            and element.acp.template_ref.source_cpg == template.summary.source_cpg
        )
        for element in automation.ir.process.flowElements
    )
    context = InstanceContext(
        bindings=automation.bindings,
        evidence=automation.derivation_evidence,
        activities=[_activity()],
    )
    assert validate_instance(automation.ir, [template.ir], context) == []


def test_remote_validation_payload_excludes_patient_activity_and_phi_values():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    context = InstanceContext(activities=[{"description": "Patient-identifying activity"}])

    payload = _validation_payload(template.ir, [template.ir], context)

    assert "activities" not in payload["context"]
    assert "phi_known_values" not in payload


def test_semantic_reviewer_repair_is_bounded_and_can_be_accepted():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([
        _output(template),
        {"verdict": "REVISE", "issues": [{"description": "Clarify sequence support", "fix": "Retain template sequence."}]},
        _output(template),
        {"verdict": "APPROVE"},
    ])

    automations = _synthesize(template, llm, use_llm_reviewer=True)

    assert automations[0].enabled is True
    assert automations[0].validation.status == "valid"
    assert automations[0].validation.rungs_passed[-1] == "L7"
    assert len(llm.prompts) == 4


def test_renamed_node_fails_i6_and_is_repaired():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template, rename=True), _output(template)])

    automations = _synthesize(template, llm)

    assert automations[0].enabled is True
    assert len(llm.prompts) == 2
    assert "I6" in str(llm.prompts[1])


def test_unbound_required_parameter_is_incomplete_and_disabled():
    template = _template_with_parameter("scheduling_window", required=True)
    llm = ScriptedLLM([_output(template)])

    automations = _synthesize(template, llm)

    assert automations[0].enabled is False
    assert automations[0].validation.status == "incomplete"
    assert any("scheduling_window" in warning for warning in automations[0].validation.warnings)


def test_no_linked_template_returns_no_automation():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([])

    result = synthesize_for_activity(
        {**_activity(), "source_recommendation_id": "unlinked"},
        Registry([template]),
        [],
        brief={},
        state={},
        llm=llm,
    )

    assert result == []
    assert llm.prompts == []


def test_process_template_refs_are_replaced_with_selected_snapshot():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template, add_second_ref=True), {"verdict": "APPROVE"}])

    automations = _synthesize(template, llm, use_llm_reviewer=True)

    assert automations[0].enabled is True
    assert len(automations[0].template_refs) == 1
    assert len(llm.prompts) == 2


def test_synthesis_attempt_artifacts_are_phi_redacted(tmp_path):
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template), {"verdict": "APPROVE"}])
    state = {
        "patient_demographics": {"name": "Ada Patient"},
        "run_id": "run-123",
        "output_dir": str(tmp_path / "careplans" / "careplan-123"),
    }

    _synthesize(template, llm, use_llm_reviewer=True, state=state)

    artifact_dir = tmp_path / "acp-writer" / "run-123" / "automation" / "activity-1"
    requests = sorted(artifact_dir.glob("attempt-*.request.json"))
    responses = sorted(artifact_dir.glob("attempt-*.response.json"))
    assert len(requests) == len(responses) == 2
    assert all("Ada Patient" not in path.read_text() for path in requests + responses)
    assert json.loads(responses[0].read_text())["role"] == "synthesizer"
    assert json.loads(responses[1].read_text())["role"] == "reviewer"
