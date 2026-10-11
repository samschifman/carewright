"""Deterministic template selection by recommendation and DMN output."""

from pathlib import Path
from types import SimpleNamespace

from cpg_contracts.automation.templates import AutomationTemplate
from cpg_contracts.automation.ir import Trigger
from acp_writer.nodes.automation_selection import select_templates


GOLDEN = (
    Path(__file__).parent / "fixtures/home-bp-monitoring.template.json"
)


def _simple_template(template_id, *, recommendations=(), models=(), triggers=()):
    return SimpleNamespace(
        summary=SimpleNamespace(
            id=template_id,
            version="1" * 12,
            linked_recommendation_ids=list(recommendations),
            linked_decision_model_ids=list(models),
            triggers=list(triggers),
        )
    )


class Registry:
    def __init__(self, templates):
        self.templates = templates
        self.calls = []

    def list(self, *, recommendation_id=None, dmn_model_id=None):
        self.calls.append((recommendation_id, dmn_model_id))
        return [
            template for template in self.templates
            if (recommendation_id is None or recommendation_id in template.summary.linked_recommendation_ids)
            and (dmn_model_id is None or dmn_model_id in template.summary.linked_decision_model_ids)
        ]


def test_selects_linked_recommendation_template():
    template = AutomationTemplate.model_validate_json(GOLDEN.read_text())
    registry = Registry([template])
    activity = SimpleNamespace(source_recommendation_id="<placeholder-rec-id>", source_dmn_call=None)

    result = select_templates(activity, registry, [])

    assert result == [template]
    assert registry.calls == [("<placeholder-rec-id>", None)]


def test_selects_only_a_dmn_output_trigger_matching_the_audit_entry():
    trigger = Trigger(
        kind="dmn-output",
        model_id="treatment-recommendation",
        output_name="Action",
        output_value="Start medication",
    )
    template = _simple_template(
        "acei-follow-up",
        models=["treatment-recommendation"],
        triggers=[trigger],
    )
    registry = Registry([template])
    activity = SimpleNamespace(source_recommendation_id=None, source_dmn_call=0)
    audit = [{
        "model_id": "treatment-recommendation",
        "outputs": {"Action": "Start medication"},
    }]

    assert select_templates(activity, registry, audit) == [template]

    audit[0]["outputs"]["Action"] = "Lifestyle only"
    assert select_templates(activity, registry, audit) == []


def test_deduplicates_template_matching_both_links_and_ignores_invalid_audit_index():
    template = _simple_template(
        "combined",
        recommendations=["rec-1"],
        models=["dmn-1"],
        triggers=[Trigger(
            kind="dmn-output",
            model_id="dmn-1",
            output_name="Result",
            output_value="yes",
        )],
    )
    registry = Registry([template])
    activity = SimpleNamespace(source_recommendation_id="rec-1", source_dmn_call=7)

    assert select_templates(activity, registry, [])[0] is template
    assert len(select_templates(activity, registry, [{"model_id": "dmn-1", "outputs": {"Result": "yes"}}])) == 1
