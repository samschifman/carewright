"""Monolith ⇄ split-path parity guard.

The systemic bug behind issue #169's conflict miss: the cluster deployment
splits the LangGraph pipeline across per-pod FastAPI services that hand-
re-implement the node ordering. A node added to the monolith graph
(``pipeline.build_pipeline``) can therefore silently never run in the split
deployment — exactly what happened to ``conflict_analyst``.

This is a cheap structural guard, not a behavioral equivalence proof: it asserts
that every node the monolith runs is at least *referenced* by some split service
module. It will fail loudly the next time a node is wired into the monolith but
forgotten in the split path. The architectural fix (one shared phase module both
paths call) is tracked in issue #174; until then, this test is the backstop.
"""

from pathlib import Path
from types import SimpleNamespace
import json

from cpg_contracts.automation.templates import AutomationTemplate
from acp_writer.nodes.automation_synthesizer import synthesize_automations_for_brief
from .pipeline_helpers import build_pipeline

_SERVICES_DIR = Path(__file__).resolve().parent.parent / "src" / "acp_writer" / "services"


def _monolith_nodes() -> set[str]:
    graph = build_pipeline()
    return {n for n in graph.nodes if not n.startswith("__")}


def _split_service_source() -> str:
    return "\n".join(p.read_text() for p in _SERVICES_DIR.glob("*.py"))


def test_every_monolith_node_is_wired_into_a_split_service():
    nodes = _monolith_nodes()
    assert nodes, "expected the monolith pipeline to declare nodes"

    source = _split_service_source()
    missing = sorted(n for n in nodes if n not in source)

    assert not missing, (
        "These monolith pipeline nodes are not referenced by any split service "
        f"and would silently never run in the cluster deployment: {missing}. "
        "Wire them into the appropriate acp_writer/services/*.py handler "
        "(see the phase-runner fast-follow issue #174)."
    )


def test_conflict_analyst_specifically_runs_in_split_compose():
    # Regression pin for #169: conflict_analyst must run in the split compose
    # service (llm_reasoning), not only in the monolith graph.
    llm_reasoning_src = (_SERVICES_DIR / "llm_reasoning.py").read_text()
    assert "conflict_analyst" in llm_reasoning_src


class _Registry:
    def __init__(self, template):
        self.template = template

    def list(self, *, recommendation_id=None, dmn_model_id=None):
        return [self.template] if recommendation_id in self.template.summary.linked_recommendation_ids else []


class _LLM:
    def __init__(self, response):
        self.response = response

    def invoke(self, _messages):
        return SimpleNamespace(content=json.dumps(self.response))


def test_monolith_and_split_compose_share_automation_synthesis_result(monkeypatch):
    # The synthesizer intentionally generates a fresh instance id per call;
    # pin it so this parity assertion compares the orchestration results.
    monkeypatch.setattr(
        "acp_writer.nodes.automation_synthesizer._new_automation_id",
        lambda: "a0000000-0000-4000-8000-000000000000",
    )
    golden = Path(__file__).parent / "fixtures/home-bp-monitoring.template.json"
    template = AutomationTemplate.model_validate_json(golden.read_text())
    response = {"bindings": []}
    brief = {
        "patient_reference": "Patient/example",
        "applicable_cpgs": [template.summary.source_cpg],
        "goals": [],
        "activities": [{
            "id": "activity-1",
            "type": "monitoring",
            "description": "Monitor home blood pressure readings.",
            "source_recommendation_id": template.summary.linked_recommendation_ids[0],
            "source_cpg": template.summary.source_cpg,
        }],
    }
    common = {
        "planning_brief": brief,
        "dmn_results": [],
        "patient_demographics": {},
        "litellm_url": "http://unused",
    }
    validator = lambda _payload: {"valid": True, "bpmn_xml": template.bpmn_xml}

    # Both orchestration paths call this same node with the same inputs.
    monolith = synthesize_automations_for_brief(
        common,
        registry=_Registry(template),
        validator=validator,
        llm=_LLM(response),
    )
    split = synthesize_automations_for_brief(
        common,
        registry=_Registry(template),
        validator=validator,
        llm=_LLM(response),
    )

    left = monolith["planning_brief"]["activities"][0]["automations"][0]
    right = split["planning_brief"]["activities"][0]["automations"][0]
    for field in ("template_refs", "ir", "bpmn_xml", "bindings", "enabled", "revision", "validation"):
        assert left[field] == right[field]
    llm_reasoning_src = (_SERVICES_DIR / "llm_reasoning.py").read_text()
    assert "synthesize_automations_for_brief" in llm_reasoning_src
