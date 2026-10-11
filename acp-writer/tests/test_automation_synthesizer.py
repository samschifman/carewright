"""The slice synthesizer keeps template behavior and gates enablement."""

from pathlib import Path
from types import SimpleNamespace
import json

from cpg_contracts.automation.instances import ActivityAutomation
from cpg_contracts.automation.ir import Parameter, ProcessIR, TemplateRef, ir_version
from cpg_contracts.automation.templates import AutomationTemplate
from acp_writer.nodes.automation_synthesizer import _validation_payload, synthesize_for_activity
from cpg_contracts.automation.validators.grounding import InstanceContext


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


def _output(template, *, rename: bool = False, add_second_ref: bool = False):
    ir = _instance(template)
    if rename:
        next(item for item in ir.process.flowElements if item.id == "query_systolic").name = "Invented node name"
    if add_second_ref:
        ir.process.acp.template_refs.append(TemplateRef(template_id="other", version="0" * 12))
    return {"ir": ir.model_dump(mode="json", exclude_none=True), "bindings": []}


def _activity():
    return {
        "id": "activity-1",
        "type": "monitoring",
        "description": "Monitor home blood pressure readings for Ada Patient.",
        "source_recommendation_id": "<placeholder-rec-id>",
        "source_cpg": "SYN-HTN-2026-002",
    }


def _synthesize(template, llm, *, reviewer=None, use_llm_reviewer=False):
    return synthesize_for_activity(
        _activity(),
        Registry([template]),
        [],
        brief={"dmn_audit_trail": []},
        state={"patient_demographics": {"name": "Ada Patient"}},
        llm=llm,
        validator=lambda payload: {"valid": True, "rungs_passed": ["L6"]},
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


def test_composed_output_is_repaired_toward_single_template():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    llm = ScriptedLLM([_output(template, add_second_ref=True), _output(template)])

    automations = _synthesize(template, llm)

    assert automations[0].enabled is True
    assert len(automations[0].template_refs) == 1
    assert len(llm.prompts) == 2
    assert "I9-not-supported" in str(llm.prompts[1])
