"""Stored reviewer-response regression from the 2026-10-11 slice run."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cpg_ingester.nodes.bpmn_semantic_reviewer import bpmn_semantic_reviewer


FIXTURES = Path(__file__).parent / "fixtures"
STORED_REVIEW = FIXTURES / "bpmn-review-from-20261011.json"


class ScriptedLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=self.response)


def _run_review(tmp_path, response):
    llm = ScriptedLLM(response)
    state = {
        "process_ir": {"process": {"id": "clinical-decision-support-alert"}},
        "item": {"name": "Clinical Decision Support Alert Workflow"},
        "source_pages": "Automated alerts should notify clinicians when criteria are met.",
        "output_dir": str(tmp_path),
    }
    with patch(
        "cpg_ingester.nodes.bpmn_semantic_reviewer.get_llm", return_value=llm
    ):
        result = bpmn_semantic_reviewer(state)
    return result, llm


def test_saved_review_claims_drive_routing_without_escalation(tmp_path):
    response = STORED_REVIEW.read_text()

    result, llm = _run_review(tmp_path, response)

    assert llm.calls == 1
    assert result["force_escalate"] is False
    assert len(result["semantic_discrepancies"]) == 4
    assert "criteria-based trigger" in result["semantic_discrepancies"][0]
    raw = list((tmp_path / "bpmn" / "responses").rglob("reviewer-001.json"))
    assert len(raw) == 1
    assert json.loads(raw[0].read_text())["response"] == response


def test_inconsistent_summary_is_rebuilt_from_parseable_claims(tmp_path):
    stored = json.loads(STORED_REVIEW.read_text())
    stored["discrepancies_found"] = False
    stored["discrepancies"] = []

    result, llm = _run_review(tmp_path, json.dumps(stored))

    assert llm.calls == 1
    assert result["force_escalate"] is False
    assert len(result["semantic_discrepancies"]) == 4


def test_raw_creator_and_reviewer_responses_are_redacted_and_sequenced(tmp_path):
    from cpg_ingester.llm_responses import persist_llm_response

    first = persist_llm_response(
        tmp_path,
        process_name="Home BP",
        role="creator",
        content='{"key":"sk-' + ("A" * 32) + '"}',
    )
    second = persist_llm_response(
        tmp_path,
        process_name="Home BP",
        role="creator",
        content='OPENAI_API_KEY=secret-value',
    )

    assert first.name == "creator-001.json"
    assert second.name == "creator-002.json"
    assert "sk-" not in first.read_text()
    assert "secret-value" not in second.read_text()
