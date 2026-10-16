"""Tests for BPMN creation, validation repair, semantic review, and preflight."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.ir import ProcessIR
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_ingester.generation import (
    _build_bpmn_subgraph,
    _bpmn_escalate,
    _bpmn_source_sections,
    _route_after_bpmn_engine,
    _route_after_bpmn_validation,
    bpmn_engine_preflight,
    generate_all,
)
from cpg_ingester.nodes.bpmn_ir_creator import _normalize_template_ir, bpmn_ir_creator
from cpg_ingester.nodes.bpmn_semantic_reviewer import bpmn_semantic_reviewer
from cpg_ingester.nodes.dmn_creator import dmn_creator
from cpg_ingester.nodes.rec_extractor import rec_extractor


GOLDEN_DIR = Path(__file__).parents[1] / "data" / "golden" / "automation"
SOURCE_TEXT = (
    Path(__file__).parents[1] / "data" / "synthetic-hypertension-cpg-v2.md"
).read_text()


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.responses.pop(0))


def _ir(name="home-bp-monitoring"):
    return json.loads((GOLDEN_DIR / f"{name}.ir.json").read_text())


def _implicit_join(ir):
    changed = deepcopy(ir)
    elements = changed["process"]["flowElements"]
    start = next(element for element in elements if element["type"] == "startEvent")
    task = next(element for element in elements if element["type"] == "task")
    elements.append({
        "id": "f_implicit_join",
        "type": "sequenceFlow",
        "sourceRef": start["id"],
        "targetRef": task["id"],
    })
    return changed


def _rename_id(ir, old_id, new_id):
    changed = deepcopy(ir)
    for element in changed["process"]["flowElements"]:
        if element.get("id") == old_id:
            element["id"] = new_id
        for field in ("sourceRef", "targetRef", "attachedToRef"):
            if element.get(field) == old_id:
                element[field] = new_id
    return changed


def _review(*, discrepancy=False):
    if discrepancy:
        return json.dumps({
            "claims_checked": [{
                "claim": "The process repeats weekly",
                "verdict": "DISCREPANCY",
                "severity": "CRITICAL",
                "feedback": "The generated process adds a weekly repeat not present in the CPG.",
                "evidence": "The source gives a one-time schedule.",
            }],
            "discrepancies_found": True,
            "summary": "Unsupported cadence.",
            "discrepancies": ["Remove the unsupported weekly repeat."],
        })
    return json.dumps({
        "claims_checked": [{
            "claim": "The process follows the source sequence",
            "verdict": "VERIFIED",
            "severity": "CRITICAL",
            "evidence": "The source describes the same sequence.",
        }],
        "discrepancies_found": False,
        "summary": "",
        "discrepancies": [],
    })


def _state(output_dir):
    return {
        "item": {
            "type": "process",
            "name": "Home Blood Pressure Monitoring",
            "template_id": "home-bp-monitoring",
            "section": "3.5",
            "description": "Collect and review home blood pressure readings.",
            "linked_recommendations": ["Home BP Monitoring"],
            "linked_decisions": [],
            "tier_hint": "A",
        },
        "source_pages": SOURCE_TEXT,
        "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        "dmn_summaries": {},
        "linked_recs": [],
        "abbreviations": {},
        "output_dir": str(output_dir),
    }


def _invoke(scripted_llm, tmp_path, monkeypatch, reviewer_llm=None):
    monkeypatch.setattr(
        "cpg_ingester.nodes.bpmn_ir_creator.get_llm", lambda _state: scripted_llm
    )
    monkeypatch.setattr(
        "cpg_ingester.nodes.bpmn_semantic_reviewer.get_llm",
        lambda _state: reviewer_llm or scripted_llm,
    )
    return _build_bpmn_subgraph().compile().invoke(_state(tmp_path))


def test_golden_is_accepted_on_first_creation_attempt(tmp_path, monkeypatch):
    llm = ScriptedLLM([json.dumps(_ir()), _review()])

    result = _invoke(llm, tmp_path, monkeypatch)

    assert result["escalated"] is False
    assert result["process_ir"]["process"]["id"] == "home-bp-monitoring"
    assert result["bpmn_xml"].startswith("<?xml")
    assert result["static_retry_count"] == 0
    assert result["join_normalizer_fired"] is False
    assert len(llm.calls) == 2
    response_dir = tmp_path / "bpmn" / "responses" / "home-blood-pressure-monitoring"
    assert (response_dir / "creator-001.json").is_file()
    assert (response_dir / "reviewer-001.json").is_file()


def test_implicit_join_is_normalized_before_l0(tmp_path):
    normalized = _normalize_template_ir(
        _implicit_join(_ir()),
        _state(tmp_path),
    )
    ir = ProcessIR.model_validate(normalized)
    result = run_static_ladder(
        ir,
        catalog=load_catalog(),
        stage="template",
        section_text=SOURCE_TEXT,
    )
    elements = {element["id"]: element for element in normalized["process"]["flowElements"]}

    assert result.rungs_passed and result.rungs_passed[0] == "L0"
    assert elements["request_readings_join"]["gatewayDirection"] == "Converging"
    assert elements["request_readings_join_in"]["sourceRef"] == "request_readings_join"
    assert elements["request_readings_join_in"]["targetRef"] == "request_readings"


def test_normalizer_fills_omitted_structural_data_before_l0(tmp_path):
    raw = _ir()
    process = raw["process"]
    counter_names = {
        element.get("acp", {}).get("counter")
        for element in process["flowElements"]
        if element.get("type") == "task"
    }
    process["properties"] = [
        prop for prop in process.get("properties", [])
        if prop.get("name") not in counter_names
    ]
    by_id = {element["id"]: element for element in process["flowElements"]}
    for element in process["flowElements"]:
        if element["type"] != "sequenceFlow":
            element.pop("name", None)
        if element["type"] in {"startEvent", "endEvent"}:
            element.get("acp", {}).pop("provenance", None)
        elif (
            element["type"] == "exclusiveGateway"
            and element.get("gatewayDirection") == "Converging"
        ):
            element.get("acp", {}).pop("provenance", None)
        elif element["type"] == "sequenceFlow":
            source = by_id.get(element.get("sourceRef"), {})
            is_default = source.get("default") == element["id"]
            if is_default or element.get("conditionExpression") is None:
                element.get("acp", {}).pop("provenance", None)

    normalized = _normalize_template_ir(raw, _state(tmp_path))
    result = run_static_ladder(
        ProcessIR.model_validate(normalized),
        catalog=load_catalog(),
        stage="template",
        section_text=SOURCE_TEXT,
    )
    normalized_process = normalized["process"]
    normalized_by_id = {element["id"]: element for element in normalized_process["flowElements"]}

    assert result.rungs_passed and result.rungs_passed[0] == "L0"
    assert normalized_by_id["start"]["name"] == "Start"
    assert normalized_by_id["start"]["acp"]["provenance"]["derivation_rule"] == "start-event"
    assert normalized_by_id["end_period"]["acp"]["provenance"]["derivation_rule"] == "end-event"
    assert any(
        element.get("acp", {}).get("provenance", {}).get("derivation_rule") == "sequence"
        for element in normalized_process["flowElements"]
        if element["type"] == "sequenceFlow"
    )
    sequence_flow = next(
        element for element in normalized_process["flowElements"]
        if element["type"] == "sequenceFlow"
        and element.get("acp", {}).get("provenance", {}).get("derivation_rule") == "sequence"
    )
    assert sequence_flow["acp"]["provenance"]["supports"] == [
        sequence_flow["sourceRef"], sequence_flow["targetRef"],
    ]
    assert all(
        normalized_by_id[gateway["default"]]["acp"]["provenance"]["derivation_rule"] == "default-flow"
        for gateway in normalized_process["flowElements"]
        if gateway["type"] == "exclusiveGateway" and gateway.get("default")
    )
    assert all(
        prop["type"] == "integer"
        for prop in normalized_process["properties"]
        if prop["name"] in counter_names
    )


def test_normalizer_leaves_populated_structural_data_unchanged(tmp_path):
    once = _normalize_template_ir(_ir(), _state(tmp_path))
    twice = _normalize_template_ir(deepcopy(once), _state(tmp_path))

    assert twice == once


def test_normalizer_does_not_invent_source_provenance(tmp_path):
    raw = _ir()
    task = next(
        element for element in raw["process"]["flowElements"]
        if element["id"] == "request_readings"
    )
    task["acp"].pop("provenance", None)

    normalized = _normalize_template_ir(raw, _state(tmp_path))
    normalized_task = next(
        element for element in normalized["process"]["flowElements"]
        if element["id"] == "request_readings"
    )

    assert "provenance" not in normalized_task["acp"]


def test_l5b_warning_is_nonblocking_and_is_given_to_reviewer(tmp_path, monkeypatch):
    warning = {
        "rung": "L5b",
        "severity": "WARNING",
        "code": "T4-value-uncorroborated",
        "element_id": "wait_interval",
        "message": "literal 'P7D' is not corroborated by its cited text",
    }
    assert _route_after_bpmn_validation({"static_findings": [warning]}) == "bpmn_semantic_reviewer"

    llm = ScriptedLLM([_review()])
    monkeypatch.setattr(
        "cpg_ingester.nodes.bpmn_semantic_reviewer.get_llm", lambda _state: llm
    )
    bpmn_semantic_reviewer({
        "process_ir": _ir(),
        "item": {"name": "Home BP monitoring"},
        "source_pages": "Source text for the saved replay.",
        "static_findings": [warning],
        "output_dir": str(tmp_path),
    })

    user_prompt = llm.calls[0][1]["content"]
    assert "Mechanical warnings to judge" in user_prompt
    assert "T4-value-uncorroborated" in user_prompt
    assert "P7D" in user_prompt


def test_creator_reports_when_join_normalizer_fires(tmp_path, monkeypatch):
    llm = ScriptedLLM([json.dumps(_implicit_join(_ir()))])
    monkeypatch.setattr(
        "cpg_ingester.nodes.bpmn_ir_creator.get_llm", lambda _state: llm
    )

    result = bpmn_ir_creator(_state(tmp_path))

    assert result["join_normalizer_fired"] is True


def test_three_invalid_irs_exhaust_static_budget(tmp_path, monkeypatch):
    invalid = _rename_id(_ir(), "request_readings", "request_readings_route_suffix")
    llm = ScriptedLLM([json.dumps(invalid)] * 3)

    result = _invoke(llm, tmp_path, monkeypatch)

    assert result["escalated"] is True
    assert result["escalation_reason"] == "static-budget-exhausted"
    assert len(llm.calls) == 3


def test_two_critical_reviews_exhaust_semantic_budget(tmp_path, monkeypatch):
    llm = ScriptedLLM([
        json.dumps(_ir()), _review(discrepancy=True),
        json.dumps(_ir()), _review(discrepancy=True),
    ])

    result = _invoke(llm, tmp_path, monkeypatch)

    assert result["escalated"] is True
    assert result["escalation_reason"] == "semantic-budget-exhausted"
    assert len(llm.calls) == 4


def test_static_failures_after_semantic_review_retry_and_escalate_at_l0(
    tmp_path, monkeypatch
):
    invalid = _rename_id(_ir(), "request_readings", "request_readings_route_suffix")
    creator = ScriptedLLM([
        json.dumps(invalid),
        json.dumps(_ir()),
        json.dumps(invalid),
        json.dumps(invalid),
    ])
    reviewer = ScriptedLLM([_review(discrepancy=True)])

    result = _invoke(creator, tmp_path, monkeypatch, reviewer_llm=reviewer)

    assert len(creator.calls) == 4  # after attempt 3, static retries are 2 < 3
    assert len(reviewer.calls) == 1
    assert result["semantic_retry_count"] == 1
    assert result["static_retry_count"] == 3
    assert result["escalated"] is True
    assert result["escalation_reason"] == "static-budget-exhausted"
    assert any("compiler-reserved sequence '_route'" in error for error in result["escalation_errors"])
    assert any(
        finding.get("rung") == "L0"
        for finding in result["static_findings"]
    )


def test_unset_preflight_url_does_not_fail(monkeypatch):
    monkeypatch.delenv("BPMN_PREFLIGHT_URL", raising=False)

    result = bpmn_engine_preflight({"bpmn_xml": "<definitions/>"})

    assert result["engine_errors"] == []


def test_preflight_unreachable_adds_warning_and_continues(monkeypatch):
    monkeypatch.setenv("BPMN_PREFLIGHT_URL", "http://engine/api/v1/automation/templates")
    monkeypatch.setattr(
        "cpg_ingester.generation.AutomationTemplate.model_validate",
        lambda _data: SimpleNamespace(model_dump=lambda **_kwargs: {"summary": {}}),
    )
    with patch("cpg_ingester.generation.requests.post", side_effect=__import__("requests").ConnectionError("offline")):
        result = bpmn_engine_preflight({
            "template_summary": {"validation": {}},
            "process_ir": {},
            "bpmn_xml": "<definitions/>",
        })

    assert result["engine_errors"] == []
    assert any("preflight unavailable" in warning for warning in result["engine_validation_warnings"])
    assert result["template_summary"]["validation"]["warnings"]


def test_non_validation_preflight_body_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setenv("BPMN_PREFLIGHT_URL", "http://engine/api/v1/automation/templates")
    monkeypatch.setattr(
        "cpg_ingester.generation.AutomationTemplate.model_validate",
        lambda _data: SimpleNamespace(model_dump=lambda **_kwargs: {"summary": {}}),
    )
    response = SimpleNamespace(status_code=200, ok=True, json=lambda: {"status": "UP"})
    with patch("cpg_ingester.generation.requests.post", return_value=response):
        result = bpmn_engine_preflight({
            "template_summary": {"validation": {}},
            "process_ir": {},
            "bpmn_xml": "<definitions/>",
        })

    assert result["engine_errors"] == []
    assert result["engine_validation_warnings"] == [
        "BPMN preflight unavailable (HTTP 200; no validation result)"
    ]


def test_preflight_422_is_sent_to_creator_repair(monkeypatch):
    monkeypatch.setenv("BPMN_PREFLIGHT_URL", "http://engine/api/v1/automation/templates")
    monkeypatch.setattr(
        "cpg_ingester.generation.AutomationTemplate.model_validate",
        lambda _data: SimpleNamespace(model_dump=lambda **_kwargs: {"summary": {}}),
    )
    response = SimpleNamespace(
        status_code=422,
        ok=False,
        json=lambda: {"error": "static-validation-failed", "detail": "reserved parameter missing"},
    )
    with patch("cpg_ingester.generation.requests.post", return_value=response):
        result = bpmn_engine_preflight({
            "template_summary": {"validation": {}},
            "process_ir": {},
            "bpmn_xml": "<definitions/>",
            "static_retry_count": 0,
        })

    assert result["engine_errors"] == ["reserved parameter missing"]
    assert result["static_retry_count"] == 1
    assert _route_after_bpmn_engine(result) == "bpmn_ir_creator"


def test_preflight_posts_source_cpg(monkeypatch):
    monkeypatch.setenv("BPMN_PREFLIGHT_URL", "http://engine/api/v1/automation/templates")
    monkeypatch.setattr(
        "cpg_ingester.generation.AutomationTemplate.model_validate",
        lambda _data: SimpleNamespace(model_dump=lambda **_kwargs: {"summary": {}}),
    )
    response = SimpleNamespace(status_code=200, ok=True, json=lambda: {"valid": True})
    with patch("cpg_ingester.generation.requests.post", return_value=response) as post:
        bpmn_engine_preflight({
            "template_summary": {"validation": {}},
            "process_ir": {},
            "bpmn_xml": "<definitions/>",
            "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
        })

    assert "validate_only=true" in post.call_args.args[0]
    assert "source_cpg=SYN-HTN-2026-002" in post.call_args.args[0]


def test_bpmn_source_context_resolves_linked_decision_model_ids():
    state = {
        "markdown": "# 3.2 Treatment Decision\nTreatment source.\n# 3.3 Monitoring Plan\nMonitoring source.",
        "section_map": [
            {"heading": "3.2 Treatment Decision"},
            {"heading": "3.3 Monitoring Plan"},
        ],
    }
    process = {
        "section": "3.3 Monitoring Plan",
        "linked_decisions": ["treatment-recommendation"],
    }
    manifest = [{
        "type": "decision",
        "id": "decision-guid",
        "model_id": "treatment-recommendation",
        "name": "Treatment Recommendation",
        "section": "3.2 Treatment Decision",
    }]

    source = _bpmn_source_sections(process, state, manifest)

    assert "Treatment source." in source
    assert "Monitoring source." in source


def test_creator_adds_omitted_reserved_timeout_and_boundary():
    raw_ir = _ir()
    process = raw_ir["process"]
    process["acp"]["parameters"] = [
        value for value in process["acp"]["parameters"]
        if value["name"] != "max_duration"
    ]
    process["flowElements"] = [
        value for value in process["flowElements"]
        if not (value["type"] == "boundaryEvent" and value["attachedToRef"] == "main")
    ]

    result = _normalize_template_ir(raw_ir, _state(Path("/tmp")))
    parameters = result["process"]["acp"]["parameters"]
    boundary = next(
        value for value in result["process"]["flowElements"]
        if value["type"] == "boundaryEvent" and value["attachedToRef"] == "main"
    )
    task_ids = {
        value["id"] for value in result["process"]["flowElements"]
        if value["type"] == "task"
    }

    assert any(value["name"] == "max_duration" and value["reserved"] for value in parameters)
    assert boundary["id"] == "period_elapsed"
    assert boundary["timerEventDefinition"]["timeDuration"] == {"param": "max_duration"}
    assert set(boundary["acp"]["provenance"]["supports"]) == task_ids


def test_saved_home_bp_implicit_join_is_normalized_before_l0(tmp_path):
    saved_ir = Path(__file__).resolve().parents[2] / "working" / "slice-run" / "ingester" / "20261012T130810Z-28693" / "bpmn" / "home-blood-pressure-monitoring.ir.json"
    raw_ir = json.loads(saved_ir.read_text())

    normalized = _normalize_template_ir(raw_ir, _state(tmp_path))
    ir = ProcessIR.model_validate(normalized)
    result = run_static_ladder(
        ir,
        catalog=load_catalog(),
        stage="template",
        section_text=SOURCE_TEXT,
    )

    elements = {element["id"]: element for element in normalized["process"]["flowElements"]}
    join = elements["end_reviewed_join"]
    join_flow = elements["end_reviewed_join_in"]
    assert result.rungs_passed and result.rungs_passed[0] == "L0"
    assert join["type"] == "exclusiveGateway"
    assert join["gatewayDirection"] == "Converging"
    assert join["acp"]["provenance"] == {
        "kind": "structural",
        "derivation_rule": "exclusive-join",
        "supports": ["gw_notify", "period_elapsed"],
    }
    assert join_flow["sourceRef"] == "end_reviewed_join"
    assert join_flow["targetRef"] == "end_reviewed"
    assert elements["f_no_notification"]["targetRef"] == "end_reviewed_join"
    assert elements["f_timeout"]["targetRef"] == "end_reviewed_join"


def test_existing_converging_gateway_is_not_rewritten(tmp_path):
    raw_ir = _ir()
    original_elements = {element["id"]: deepcopy(element) for element in raw_ir["process"]["flowElements"]}
    original_gateway = original_elements["loop_entry"]
    original_incoming = {
        element_id: element
        for element_id, element in original_elements.items()
        if element.get("type") == "sequenceFlow" and element.get("targetRef") == "loop_entry"
    }

    normalized = _normalize_template_ir(raw_ir, _state(tmp_path))
    normalized_elements = {
        element["id"]: element for element in normalized["process"]["flowElements"]
    }

    assert normalized_elements["loop_entry"] == original_gateway
    assert {
        element_id: normalized_elements[element_id]["targetRef"]
        for element_id in original_incoming
    } == {
        element_id: element["targetRef"]
        for element_id, element in original_incoming.items()
    }


def test_model_named_join_gateway_is_valid_and_unchanged(tmp_path):
    raw_ir = _rename_id(_ir(), "loop_entry", "readings_join")

    normalized = _normalize_template_ir(raw_ir, _state(tmp_path))
    ir = ProcessIR.model_validate(normalized)
    result = run_static_ladder(
        ir,
        catalog=load_catalog(),
        stage="template",
        section_text=SOURCE_TEXT,
    )

    assert normalized == raw_ir
    assert result.rungs_passed and result.rungs_passed[0] == "L0"


def test_implicit_join_allocates_around_authored_join_id(tmp_path):
    raw_ir = _rename_id(_implicit_join(_ir()), "loop_entry", "request_readings_join")

    normalized = _normalize_template_ir(raw_ir, _state(tmp_path))
    elements = {
        element["id"]: element for element in normalized["process"]["flowElements"]
    }

    assert elements["request_readings_join"]["type"] == "exclusiveGateway"
    assert elements["request_readings_join_2"]["gatewayDirection"] == "Converging"
    assert elements["request_readings_join_in"]["sourceRef"] == "request_readings_join_2"
    assert elements["request_readings_join_in"]["targetRef"] == "request_readings"
    assert elements["f_implicit_join"]["targetRef"] == "request_readings_join_2"


def test_implicit_join_allocates_around_authored_connector_id(tmp_path):
    raw_ir = _implicit_join(
        _rename_id(_ir(), "f_none", "request_readings_join_in")
    )

    normalized = _normalize_template_ir(raw_ir, _state(tmp_path))
    elements = {
        element["id"]: element for element in normalized["process"]["flowElements"]
    }

    assert elements["request_readings_join"]["gatewayDirection"] == "Converging"
    assert elements["request_readings_join_in"]["sourceRef"] == "gw_received"
    assert elements["request_readings_join_in"]["targetRef"] == "remind"
    assert elements["request_readings_join_in_2"]["sourceRef"] == "request_readings_join"
    assert elements["request_readings_join_in_2"]["targetRef"] == "request_readings"


def test_appendix_b_fixture_is_unchanged_by_join_normalization(tmp_path):
    fixture = Path(__file__).resolve().parents[2] / "shared" / "tests" / "fixtures" / "automation" / "home-bp-monitoring.ir.json"
    raw_ir = json.loads(fixture.read_text())
    state = _state(tmp_path)
    state["item"]["template_id"] = raw_ir["process"]["id"]
    state["item"]["name"] = raw_ir["process"]["name"]

    normalized = _normalize_template_ir(raw_ir, state)

    assert normalized == raw_ir


def test_generate_all_passes_linked_dmn_namespace_and_recommendation():
    decision = {
        "type": "decision", "id": "decision-guid", "model_id": "treatment-plan",
        "name": "Treatment Plan", "section": "3.2", "inputs": [], "outputs": [],
    }
    recommendation_item = {
        "type": "recommendation", "id": "recommendation-guid",
        "title": "Home BP Monitoring", "section": "3.5",
    }
    process = {
        "type": "process", "id": "process-guid", "template_id": "home-bp-monitoring",
        "name": "Home BP Monitoring", "section": "3.5",
        "steps": ["Review weekly readings"],
        "linked_decisions": ["Treatment Plan"],
        "linked_recommendations": ["Home BP Monitoring"],
        "tier_hint": "A",
    }
    sibling = {
        **process,
        "id": "outreach-guid",
        "template_id": "missing-reading-outreach",
        "name": "Missing Reading Outreach",
        "steps": ["Send a reminder after a week without readings"],
    }
    dmn_graph = MagicMock()
    dmn_graph.invoke.return_value = {
        "dmn_xml": (
            '<definitions namespace="https://example.org/treatment-plan">'
            '<decision><decisionTable><output name="Action" typeRef="string"/>'
            '</decisionTable></decision></definitions>'
        )
    }
    rec_graph = MagicMock()
    rec_graph.invoke.return_value = {
        "recommendations": [{
            "id": "recommendation-guid", "title": "Home BP Monitoring",
            "content": "Review home BP readings.",
        }]
    }
    bpmn_graph = MagicMock()
    bpmn_graph.invoke.return_value = {
        "bpmn_xml": "<definitions/>", "process_ir": {}, "template_summary": {},
    }
    with (
        patch("cpg_ingester.generation._build_dmn_subgraph") as dmn_builder,
        patch("cpg_ingester.generation._build_rec_subgraph") as rec_builder,
        patch("cpg_ingester.generation._build_bpmn_subgraph") as bpmn_builder,
    ):
        dmn_builder.return_value.compile.return_value = dmn_graph
        rec_builder.return_value.compile.return_value = rec_graph
        bpmn_builder.return_value.compile.return_value = bpmn_graph
        result = generate_all({
            "item_manifest": [decision, recommendation_item, process, sibling],
            "markdown": "# 3.2 Treatment Plan\nText.\n# 3.5 Home BP Monitoring\nReview readings.",
            "section_map": [
                {"heading": "3.2 Treatment Plan"},
                {"heading": "3.5 Home BP Monitoring"},
            ],
            "cpg_metadata": {"cpg_id": "SYN-HTN-2026-002"},
            "review_feedback": "Retain the source's follow-up interval.",
        })

    bpmn_state = bpmn_graph.invoke.call_args_list[0].args[0]
    sibling_state = bpmn_graph.invoke.call_args_list[1].args[0]
    assert bpmn_state["dmn_summaries"][0]["namespace"] == "https://example.org/treatment-plan"
    assert bpmn_state["linked_recs"][0]["id"] == "recommendation-guid"
    assert bpmn_state["human_feedback"] == "Retain the source's follow-up interval."
    assert bpmn_state["sibling_items"] == [{
        "name": "Missing Reading Outreach",
        "steps": ["Send a reminder after a week without readings"],
    }]
    assert sibling_state["sibling_items"] == [{
        "name": "Home BP Monitoring",
        "steps": ["Review weekly readings"],
    }]
    assert dmn_graph.invoke.call_args.args[0]["review_feedback"] == "Retain the source's follow-up interval."
    assert rec_graph.invoke.call_args.args[0]["review_feedback"] == "Retain the source's follow-up interval."
    assert len(result["bpmn_results"]) == 2


def test_tier_c_and_unlinked_processes_are_gated_before_bpmn_subgraph():
    processes = [
        {"type": "process", "name": "Narrative only", "tier_hint": "C"},
        {"type": "process", "name": "Orphan", "tier_hint": "A", "linked_recommendations": ["missing"]},
    ]
    with patch("cpg_ingester.generation._build_bpmn_subgraph") as bpmn_builder:
        result = generate_all({"item_manifest": processes, "markdown": "", "section_map": []})

    bpmn_builder.assert_not_called()
    assert [item["escalation_reason"] for item in result["bpmn_results"]] == [
        "tier-not-automatable", "unlinked-process",
    ]


def test_engine_failure_has_a_distinct_escalation_reason():
    result = _bpmn_escalate({"engine_errors": ["engine rejected template"]})
    assert result["escalation_reason"] == "engine-validation-failed"


def test_bpmn_escalation_separates_static_errors_and_warnings():
    result = _bpmn_escalate({
        "static_findings": [
            {"severity": "ERROR", "message": "invalid condition"},
            {"severity": "WARNING", "message": "name-like phrase"},
        ],
        "engine_validation_warnings": ["preflight unavailable"],
    })

    assert result["escalation_errors"] == ["invalid condition"]
    assert result["escalation_warnings"] == [
        "name-like phrase",
        "preflight unavailable",
    ]


def test_human_review_feedback_appears_in_dmn_and_rec_creator_prompts(tmp_path, monkeypatch):
    feedback = "The clinical reviewer says to preserve the stated timing."
    dmn_llm = ScriptedLLM(["<definitions/>"])
    rec_llm = ScriptedLLM(['{"recommendations": []}'])
    monkeypatch.setattr("cpg_ingester.nodes.dmn_creator.get_llm", lambda _state: dmn_llm)
    monkeypatch.setattr("cpg_ingester.nodes.rec_extractor.get_llm", lambda _state: rec_llm)

    dmn_creator({
        "item": {"type": "decision", "name": "Treatment", "inputs": [], "outputs": []},
        "review_feedback": feedback,
        "source_pages": "CPG text",
        "output_dir": str(tmp_path),
    })
    rec_extractor({
        "items": [{"id": "rec-1", "section": "3.4"}],
        "review_feedback": feedback,
        "source_pages": "CPG text",
        "output_dir": str(tmp_path),
    })

    assert feedback in dmn_llm.calls[0][1]["content"]
    assert feedback in rec_llm.calls[0][1]["content"]
