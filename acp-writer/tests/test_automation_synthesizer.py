"""The synthesizer binds a pinned template without letting the model edit it."""

from pathlib import Path
from types import SimpleNamespace
import json
from unittest.mock import Mock

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cpg_contracts.automation.instances import ActivityAutomation, ParameterBinding
from cpg_contracts.automation.ir import ConceptRef, Parameter, TemplateRef, ir_version
from cpg_contracts.automation.templates import AutomationTemplate
from cpg_contracts.automation.validators.grounding import InstanceContext, validate_instance
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.decisions import DecisionVariable
from acp_writer import api
from acp_writer.nodes.automation_synthesizer import (
    _instance_from_template,
    _validation_payload,
    synthesize_for_activity,
)


GOLDEN = Path(__file__).parent / "fixtures/home-bp-monitoring.template.json"


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


def _binding(name="max_duration", value="P90D", reason="From the plan monitoring period."):
    return {"name": name, "value": value, "reason": reason}


def _template_with_parameter(name: str, *, required: bool, default=None, type="duration"):
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    template_ir = template.ir.model_copy(deep=True)
    template_ir.process.acp.parameters.append(Parameter(
        name=name,
        type=type,
        required=required,
        default=default,
    ))
    template.summary.version = ir_version(template_ir)
    template.summary.parameters = list(template_ir.process.acp.parameters)
    template.ir = template_ir
    return template


def _activity():
    return {
        "id": "activity-1",
        "type": "monitoring",
        "description": "Monitor home blood pressure readings.",
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
    brief=None,
    validator=None,
):
    effective_brief = brief or {"dmn_audit_trail": []}
    return synthesize_for_activity(
        _activity(),
        Registry([template]),
        effective_brief.get("dmn_audit_trail", []),
        brief=effective_brief,
        state=state or {"patient_demographics": {"name": "Ada Patient"}},
        llm=llm,
        validator=validator or (lambda _payload: {"valid": True, "rungs_passed": ["L6"]}),
        reviewer=reviewer if use_llm_reviewer else (reviewer or (lambda **_: (True, ""))),
    )


def test_instance_is_code_built_from_snapshot_and_passes_l0_through_l5a():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([{"bindings": []}, {"verdict": "APPROVE"}])

    automation = _synthesize(template, llm, use_llm_reviewer=True)[0]

    assert isinstance(automation, ActivityAutomation)
    assert automation.validation.status == "valid"
    assert automation.validation.rungs_passed == ["L0", "L1", "L2", "L3", "L4", "L5a", "L6", "L7"]
    assert automation.ir.process.kind == "instance"
    assert automation.ir.process.id == automation.id
    expected = template.ir.model_copy(deep=True)
    expected.process.kind = "instance"
    expected.process.id = automation.id
    expected.process.acp.template_refs = [TemplateRef(
        template_id=template.summary.id,
        version=ir_version(template.ir),
        source_cpg=template.summary.source_cpg,
    )]
    for element in expected.process.flowElements:
        element.acp.template_ref = TemplateRef(
            template_id=template.summary.id,
            version=ir_version(template.ir),
            source_cpg=template.summary.source_cpg,
            element_id=element.id,
        )
        actual = next(item for item in automation.ir.process.flowElements if item.id == element.id)
        if hasattr(element, "inputs"):
            for input_name, input_value in list(element.inputs.items()):
                if isinstance(input_value, ConceptRef):
                    assert actual.inputs[input_name] == input_value
                    binding = next(
                        item for item in automation.bindings
                        if item.name == f"{element.id}__{input_name}"
                    )
                    assert "|" in binding.value
                    assert binding.source == "plan-derived"
                else:
                    assert actual.inputs[input_name] == input_value
    assert automation.ir == expected
    context = InstanceContext(
        bindings=automation.bindings,
        evidence=automation.derivation_evidence,
        activities=[_activity()],
    )
    assert validate_instance(automation.ir, [template.ir], context) == []
    assert "Ada Patient" not in str(llm.prompts)


def test_semantic_review_receives_bindings_and_unlinked_dmn_facts():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([{"bindings": []}, {"verdict": "APPROVE"}])
    brief = {
        "dmn_audit_trail": [{
            "model_id": "treatment-recommendation",
            "model_name": "Treatment Recommendation",
            "inputs": {"Has Diabetes": True, "Has Kidney Disease": False},
            "outputs": {"Treatment Recommendation": {"Action": "Start medication"}},
        }],
    }

    automation = _synthesize(
        template,
        llm,
        use_llm_reviewer=True,
        brief=brief,
        state={
            "patient_demographics": {"name": "Synthetic Patient"},
            "automation_policy": {"environment_default": "P90D"},
        },
    )[0]

    reviewer_system = llm.prompts[1][0]["content"]
    reviewer_user = llm.prompts[1][1]["content"]
    assert automation.validation.status == "valid"
    assert "generated `main` subprocess" in reviewer_system
    assert "ActivityAutomation bindings" in reviewer_user
    assert '"name": "max_duration"' in reviewer_user
    assert '"value": "P90D"' in reviewer_user
    assert '"Has Diabetes": true' in reviewer_user


def test_authored_string_binding_is_discarded_and_template_text_is_used():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([{
        "bindings": [
            _binding("instructions_text", "rewritten instructions", "Model suggestion."),
            _binding(),
        ],
        # Structure is not part of the model contract and has no effect.
        "ir": {"process": {"name": "model edited this"}},
    }])

    automation = _synthesize(template, llm)[0]

    original = next(p for p in template.ir.process.acp.parameters if p.name == "instructions_text")
    bound = next(p for p in automation.bindings if p.name == "instructions_text")
    assert bound.value == original.default
    assert bound.source == "cpg-default"
    assert "rewritten instructions" not in {item.value for item in automation.bindings}
    assert automation.validation.status == "valid"
    ladder = run_static_ladder(
        automation.ir,
        catalog=load_catalog(),
        stage="instance",
        templates=[template.ir],
        context=InstanceContext(
            bindings=automation.bindings,
            evidence=automation.derivation_evidence,
            activities=[_activity()],
        ),
    )
    assert ladder.ok
    assert ladder.rungs_passed == ["L0", "L1", "L2", "L3", "L4", "L5a"]


def test_clinician_feedback_can_replace_authored_text_default():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    brief = {
        "prior_planning_brief": {
            "activities": [{
                "id": "activity-1",
                "automations": [{"bindings": [{
                    "name": "instructions_text",
                    "value": "Clinician-approved instructions.",
                    "source": "clinician",
                }]}],
            }],
        },
    }
    automation = _synthesize(template, ScriptedLLM([{"bindings": []}]), brief=brief)[0]

    binding = next(item for item in automation.bindings if item.name == "instructions_text")
    assert binding.value == "Clinician-approved instructions."
    assert binding.source == "clinician"
    assert automation.validation.status == "valid"


def test_missing_required_binding_is_incomplete_not_escalated():
    template = _template_with_parameter("scheduling_window", required=True)
    automation = _synthesize(template, ScriptedLLM([{"bindings": []}]))[0]

    assert automation.enabled is False
    assert automation.validation.status == "incomplete"
    assert any("scheduling_window" in warning for warning in automation.validation.warnings)


def test_engine_rejection_with_messages_is_a_repairable_l6_finding():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([{"bindings": []}, {"bindings": []}])
    results = iter([
        {"valid": False, "messages": [{"severity": "ERROR", "text": "engine rejected BPMN"}]},
        {"valid": True, "messages": []},
    ])

    automation = _synthesize(template, llm, validator=lambda _payload: next(results))[0]

    assert automation.validation.status == "valid"
    assert len(llm.prompts) == 2
    assert "kogito-validation" in str(llm.prompts[1])


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("engine connection failed"),
        requests.HTTPError("engine returned HTTP 503", response=Mock(status_code=503)),
        HTTPException(status_code=503, detail="engine returned HTTP 503"),
    ],
)
def test_engine_transport_failure_escalates_without_another_model_call(error):
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([{"bindings": []}])

    def unavailable(_payload):
        raise error

    automation = _synthesize(template, llm, validator=unavailable)[0]

    assert automation.validation.status == "escalated"
    assert automation.enabled is False
    assert any("engine" in warning for warning in automation.validation.warnings)
    assert not automation.validation.errors
    assert len(llm.prompts) == 1


def test_unexpected_validator_exception_fails_loudly():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([{"bindings": []}])

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

    llm = ScriptedLLM([{"bindings": []}, {"verdict": "APPROVE"}])
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


def test_remote_validation_payload_excludes_patient_activity_and_phi_values():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    context = InstanceContext(activities=[{"description": "Patient-identifying activity"}])

    payload = _validation_payload(template.ir, [template.ir], context)

    assert "activities" not in payload["context"]
    assert "phi_known_values" not in payload


def test_semantic_reviewer_repair_uses_bindings_only():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([
        {"bindings": []},
        {"verdict": "REVISE", "issues": [{"description": "Clarify sequence support", "fix": "Retain template sequence."}]},
        {"bindings": []},
        {"verdict": "APPROVE"},
    ])

    automation = _synthesize(template, llm, use_llm_reviewer=True)[0]

    assert automation.enabled is True
    assert automation.validation.status == "valid"
    assert automation.validation.rungs_passed[-1] == "L7"
    assert len(llm.prompts) == 4


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


def test_synthesis_attempt_artifacts_are_phi_redacted(tmp_path):
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([{"bindings": []}, {"verdict": "APPROVE"}])
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
