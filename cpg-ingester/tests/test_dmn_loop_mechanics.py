"""Tests for the DMN generation loop mechanics: separate retry budgets,
repair-mode feedback, escalation propagation, and the no-silent-drop guarantee.
"""

from unittest.mock import MagicMock, patch

import requests

from cpg_ingester import generation
from cpg_ingester.generation import (
    MAX_DMN_SEMANTIC_RETRIES,
    MAX_DMN_SYNTAX_RETRIES,
    _dmn_escalate,
    _decision_summary,
    _rec_escalate,
    _route_after_dmn_engine,
    _route_after_dmn_semantic,
    dmn_engine_preflight,
    _route_after_dmn_syntax,
    generate_all,
)
from cpg_ingester.nodes.dmn_creator import _build_feedback
from cpg_ingester.nodes.dmn_creator import _ensure_model_id, _ensure_extraction_annotations
from cpg_ingester.reference.dmn_error_patterns import (
    format_error_pattern_hints,
    match_error_patterns,
)


class TestSeparateBudgets:
    def test_syntax_retries_until_budget_then_escalates(self):
        assert _route_after_dmn_syntax(
            {"syntax_errors": ["e"], "syntax_retry_count": 0}) == "dmn_creator"
        assert _route_after_dmn_syntax(
            {"syntax_errors": ["e"], "syntax_retry_count": MAX_DMN_SYNTAX_RETRIES}) == "dmn_escalate"

    def test_clean_syntax_advances_to_review(self):
        assert _route_after_dmn_syntax({"syntax_errors": []}) == "dmn_semantic_reviewer"

    def test_semantic_budget_is_independent_of_syntax(self):
        # A high syntax count must not push the semantic loop into escalation.
        state = {"semantic_discrepancies": ["d"], "semantic_retry_count": 0,
                 "syntax_retry_count": 99}
        assert _route_after_dmn_semantic(state) == "dmn_creator"
        state["semantic_retry_count"] = MAX_DMN_SEMANTIC_RETRIES
        assert _route_after_dmn_semantic(state) == "dmn_escalate"

    def test_force_escalate_bypasses_budget(self):
        assert _route_after_dmn_semantic(
            {"force_escalate": True, "semantic_discrepancies": [], "semantic_retry_count": 0}
        ) == "dmn_escalate"

    def test_clean_semantic_accepts(self):
        assert _route_after_dmn_semantic({"semantic_discrepancies": []}) == "dmn_accept"

    def test_engine_errors_share_syntax_retry_budget(self):
        assert _route_after_dmn_engine({"engine_errors": ["compile failed"], "syntax_retry_count": 0}) == "dmn_creator"
        assert _route_after_dmn_engine({"engine_errors": ["compile failed"], "syntax_retry_count": MAX_DMN_SYNTAX_RETRIES}) == "dmn_escalate"
        assert _route_after_dmn_engine({"engine_errors": []}) == "dmn_complete"


class TestEscalateNode:
    def test_records_syntax_budget_reason(self):
        out = _dmn_escalate({"item": {"name": "X"}, "syntax_errors": ["bad"]})
        assert out["escalated"] is True
        assert out["escalation_reason"] == "syntax-budget-exhausted"
        assert out["escalation_errors"] == ["bad"]

    def test_reviewer_reason_takes_precedence(self):
        out = _dmn_escalate({"item": {"name": "X"}, "escalation_reason": "no-source-text",
                             "semantic_discrepancies": ["no source"]})
        assert out["escalation_reason"] == "no-source-text"

    def test_rec_schema_reason_and_errors(self):
        out = _rec_escalate({"schema_errors": ["bad json"]})
        assert out == {
            "escalated": True,
            "escalation_reason": "schema-budget-exhausted",
            "escalation_errors": ["bad json"],
        }

    def test_rec_semantic_reason_and_errors(self):
        out = _rec_escalate({"semantic_discrepancies": ["unsupported claim"]})
        assert out["escalation_reason"] == "semantic-budget-exhausted"
        assert out["escalation_errors"] == ["unsupported claim"]


class TestRepairFeedback:
    def test_creator_embeds_stable_model_id(self):
        xml = '<definitions name="Treatment Recommendation"></definitions>'
        assert 'id="treatment-recommendation"' in _ensure_model_id(xml, "treatment-recommendation")

    def test_creator_emits_extraction_annotation(self):
        xml = '<definitions><inputData name="Systolic BP"><variable name="Systolic BP"/></inputData></definitions>'
        result = _ensure_extraction_annotations(xml, [{
            "name": "Systolic BP",
            "extraction": {"function": "observation_count", "params": {"duration": "P3M"}},
        }])
        assert "acp:extraction" in result
        assert "observation_count" in result

    def test_creator_replaces_malformed_extraction_with_manifest_annotation(self):
        xml = (
            '<definitions xmlns:acp="https://redhat.com/cpg-to-acp/dmn">'
            '<inputData><variable name="Systolic BP"/><extensionElements>'
            '<acp:extraction><![CDATA[{"function":"not_a_manifest_value"}]]>'
            '</acp:extraction></extensionElements></inputData></definitions>'
        )
        result = _ensure_extraction_annotations(xml, [{
            "name": "Systolic BP",
            "extraction": {"function": "observation_count", "params": {"duration": "P3M"}},
        }])
        assert result.count("<acp:extraction>") == 1
        assert '{"function":"observation_count","params":{"duration":"P3M"}}' in result
        assert "not_a_manifest_value" not in result

    def test_renders_both_sections_and_previous_xml(self):
        fb = _build_feedback(["missing hitPolicy"], ["threshold wrong"], "<definitions/>")
        assert "Syntax errors to fix" in fb
        assert "Semantic discrepancies to fix" in fb
        assert "<definitions/>" in fb
        assert "missing hitPolicy" in fb
        assert "threshold wrong" in fb

    def test_renders_engine_validation_section(self):
        fb = _build_feedback([], [], "<definitions/>", ["unknown variable 'age'"])
        assert "Engine validation errors to fix" in fb
        assert "unknown variable 'age'" in fb

    def test_empty_when_no_errors(self):
        assert _build_feedback([], [], "<definitions/>") == ""

    def test_appends_known_error_pattern(self):
        fb = _build_feedback(["DecisionTable 'x': missing hitPolicy attribute"], [], "")
        assert "Known error patterns" in fb


class TestErrorPatternKB:
    def test_extraction_annotation_hint_tells_creator_to_remove_annotation(self):
        hint = _build_feedback(["Extraction annotation: function: Field required"], [], "")
        assert "do not emit acp:extraction" in hint.lower()
        assert "remove it" in hint.lower()

    def test_matches_missing_hit_policy(self):
        hits = match_error_patterns(["DecisionTable 'x': missing hitPolicy attribute"])
        assert hits
        assert any("hitPolicy" in h.fix for h in hits)

    def test_regex_pattern_matches_entry_count(self):
        hits = match_error_patterns(["Rule 'r1': has 2 inputEntries, expected 3"])
        assert hits

    def test_no_match_returns_empty(self):
        assert match_error_patterns(["something totally unrecognized xyzzy"]) == []
        assert format_error_pattern_hints(["xyzzy"]) == ""

    def test_semantic_duplicate_word_does_not_match_duplicate_id_pattern(self):
        assert match_error_patterns(["semantic text contains a duplicate clinical claim"]) == []


class TestGenerateAllNoSilentDrop:
    """A decision must never vanish from the output — crashes and empty results
    both become flagged entries."""

    _MANIFEST = [{"type": "decision", "name": "D1", "section": "S1"}]

    def test_decision_summary_includes_dmn_namespace(self):
        summary = _decision_summary(
            {"name": "Treatment", "inputs": [], "outputs": []},
            {},
            [],
            '<definitions namespace="https://example.org/dmn">'
            '<decision><decisionTable/></decision></definitions>',
        )
        assert summary["namespace"] == "https://example.org/dmn"

    def _run_with_graph(self, fake_graph):
        state = {"item_manifest": self._MANIFEST, "markdown": "", "section_map": []}
        with patch.object(generation, "_build_dmn_subgraph") as mock_builder, \
             patch.object(generation, "_build_rec_subgraph") as mock_rec:
            mock_builder.return_value.compile.return_value = fake_graph
            mock_rec.return_value.compile.return_value = MagicMock(invoke=MagicMock(return_value={}))
            return generate_all(state)

    def test_subgraph_exception_becomes_flagged_entry(self):
        graph = MagicMock()
        graph.invoke = MagicMock(side_effect=RuntimeError("boom"))
        result = self._run_with_graph(graph)
        assert len(result["dmn_results"]) == 1
        entry = result["dmn_results"][0]
        assert entry["escalated"] is True
        assert entry["escalation_reason"] == "generation-exception"

    def test_source_page_range_is_not_used_as_review_source_text(self):
        graph = MagicMock()
        graph.invoke = MagicMock(return_value={"dmn_xml": "<definitions/>"})
        state = {
            "item_manifest": [{
                "type": "decision", "name": "D1", "section": "Missing section",
                "source_page_range": "pages 3-4",
            }],
            "markdown": "source exists but the section heading does not",
            "section_map": [],
        }
        with patch.object(generation, "_build_dmn_subgraph") as mock_builder, \
             patch.object(generation, "_build_rec_subgraph") as mock_rec:
            mock_builder.return_value.compile.return_value = graph
            mock_rec.return_value.compile.return_value = MagicMock(invoke=MagicMock(return_value={}))
            generate_all(state)
        assert graph.invoke.call_args.args[0]["source_pages"] == ""

    def test_accepted_decision_carries_model_summary(self):
        graph = MagicMock()
        graph.invoke = MagicMock(return_value={
            "dmn_xml": (
                "<definitions><decision><decisionTable>"
                "<output name=\"Action\" typeRef=\"string\"/>"
                "<output name=\"Follow Up Weeks\" typeRef=\"number\"/>"
                "</decisionTable></decision></definitions>"
            )
        })
        state = {
            "item_manifest": [{
                "type": "decision", "name": "Treatment Recommendation", "section": "S1",
                "category": "treatment", "inputs": [{"name": "BP", "type": "number"}],
                "outputs": ["Action"], "page_start": 3, "page_end": 4,
                "model_id": "treatment-recommendation", "id": "guid-1",
            }],
            "cpg_metadata": {"cpg_id": "CPG-1"}, "markdown": "", "section_map": [],
        }
        with patch.object(generation, "_build_dmn_subgraph") as mock_builder, \
             patch.object(generation, "_build_rec_subgraph") as mock_rec:
            mock_builder.return_value.compile.return_value = graph
            mock_rec.return_value.compile.return_value = MagicMock(invoke=MagicMock(return_value={}))
            result = generate_all(state)
        summary = result["dmn_results"][0]["decision_model_summary"]
        assert summary["id"] == "treatment-recommendation"
        assert summary["source_cpg"] == "CPG-1"
        assert summary["source_location"]["page_start"] == 3
        assert summary["outputs"] == [
            {"name": "Action", "type": "string"},
            {"name": "Follow Up Weeks", "type": "number"},
        ]

    def test_summary_skips_invalid_page_range_and_records_warning(self):
        graph = MagicMock(return_value={})
        graph.invoke = MagicMock(return_value={
            "dmn_xml": "<definitions><decision><decisionTable/></decision></definitions>"
        })
        state = {
            "item_manifest": [{
                "type": "decision", "name": "D1", "section": "S1",
                "inputs": [], "outputs": [], "page_start": "3-4",
            }],
            "markdown": "", "section_map": [],
        }
        with patch.object(generation, "_build_dmn_subgraph") as mock_builder, \
             patch.object(generation, "_build_rec_subgraph") as mock_rec:
            mock_builder.return_value.compile.return_value = graph
            mock_rec.return_value.compile.return_value = MagicMock(invoke=MagicMock(return_value={}))
            result = generate_all(state)
        entry = result["dmn_results"][0]
        assert "source_location" not in entry["decision_model_summary"]
        assert any("summary-build-failed" in warning for warning in entry["validation_warnings"])

    def test_summary_uses_only_modifies_not_cross_references(self):
        graph = MagicMock()
        graph.invoke = MagicMock(return_value={
            "dmn_xml": "<definitions><decision><decisionTable/></decision></definitions>"
        })
        target = {
            "type": "decision", "name": "Target", "section": "S1",
            "inputs": [], "outputs": [], "id": "target-guid", "model_id": "target-model",
        }
        source = {
            "type": "decision", "name": "Source", "section": "S1",
            "inputs": [], "outputs": [], "id": "source-guid",
            "model_id": "source-model", "cross_references": ["target-guid"],
        }
        state = {"item_manifest": [source, target], "markdown": "", "section_map": []}
        with patch.object(generation, "_build_dmn_subgraph") as mock_builder, \
             patch.object(generation, "_build_rec_subgraph") as mock_rec:
            mock_builder.return_value.compile.return_value = graph
            mock_rec.return_value.compile.return_value = MagicMock(invoke=MagicMock(return_value={}))
            result = generate_all(state)
        assert result["dmn_results"][0]["decision_model_summary"].get("modifies") is None

        source["modifies"] = "target-guid"
        with patch.object(generation, "_build_dmn_subgraph") as mock_builder, \
             patch.object(generation, "_build_rec_subgraph") as mock_rec:
            mock_builder.return_value.compile.return_value = graph
            mock_rec.return_value.compile.return_value = MagicMock(invoke=MagicMock(return_value={}))
            result = generate_all({"item_manifest": [source, target], "markdown": "", "section_map": []})
        assert result["dmn_results"][0]["decision_model_summary"]["modifies"] == ["target-model"]

    def test_empty_result_becomes_flagged_entry(self):
        graph = MagicMock()
        graph.invoke = MagicMock(return_value={"dmn_xml": ""})
        result = self._run_with_graph(graph)
        assert len(result["dmn_results"]) == 1
        entry = result["dmn_results"][0]
        assert entry["escalated"] is True
        assert entry["escalation_reason"] == "empty-result"

    def test_escalated_result_carries_reason_and_errors(self):
        graph = MagicMock()
        graph.invoke = MagicMock(return_value={
            "dmn_xml": "<definitions/>",
            "escalated": True,
            "escalation_reason": "syntax-budget-exhausted",
            "escalation_errors": ["missing hitPolicy"],
        })
        result = self._run_with_graph(graph)
        entry = result["dmn_results"][0]
        assert entry["escalated"] is True
        assert entry["escalation_reason"] == "syntax-budget-exhausted"
        assert entry["escalation_errors"] == ["missing hitPolicy"]

    def test_escalated_recommendation_carries_reason_and_errors(self):
        rec_graph = MagicMock()
        rec_graph.invoke = MagicMock(return_value={
            "recommendations": [{"id": "rec-1", "title": "Use therapy"}],
            "escalated": True,
            "escalation_reason": "schema-budget-exhausted",
            "escalation_errors": ["bad json"],
        })
        with patch.object(generation, "_build_dmn_subgraph") as dmn_builder, \
             patch.object(generation, "_build_rec_subgraph") as rec_builder:
            dmn_builder.return_value.compile.return_value = MagicMock()
            rec_builder.return_value.compile.return_value = rec_graph
            result = generate_all({
                "item_manifest": [{"type": "recommendation", "section": "Treatment"}],
                "markdown": "", "section_map": [],
            })
        assert result["recommendation_results"] == [{
            "id": "rec-1", "title": "Use therapy", "escalated": True,
            "escalation_reason": "schema-budget-exhausted",
            "escalation_errors": ["bad json"],
        }]
        assert result["recommendation_escalations"] == []

    def test_empty_escalated_recommendation_section_is_preserved_separately(self):
        rec_graph = MagicMock()
        rec_graph.invoke = MagicMock(return_value={
            "recommendations": [],
            "escalated": True,
            "escalation_reason": "schema-budget-exhausted",
            "escalation_errors": ["bad json"],
        })
        with patch.object(generation, "_build_dmn_subgraph") as dmn_builder, \
             patch.object(generation, "_build_rec_subgraph") as rec_builder:
            dmn_builder.return_value.compile.return_value = MagicMock()
            rec_builder.return_value.compile.return_value = rec_graph
            result = generate_all({
                "item_manifest": [{"type": "recommendation", "section": "Treatment"}],
                "markdown": "", "section_map": [],
            })
        assert result["recommendation_results"] == []
        assert result["recommendation_escalations"] == [{
            "type": "recommendation", "id": "Treatment", "name": "Section: Treatment",
            "section": "Treatment", "escalation_reason": "schema-budget-exhausted",
            "escalation_errors": ["bad json"],
        }]

    def test_crashed_recommendation_section_is_preserved_separately(self):
        rec_graph = MagicMock()
        rec_graph.invoke = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(generation, "_build_dmn_subgraph") as dmn_builder, \
             patch.object(generation, "_build_rec_subgraph") as rec_builder:
            dmn_builder.return_value.compile.return_value = MagicMock()
            rec_builder.return_value.compile.return_value = rec_graph
            result = generate_all({
                "item_manifest": [{"type": "recommendation", "section": "Treatment"}],
                "markdown": "", "section_map": [],
            })
        assert result["recommendation_results"] == []
        assert result["recommendation_escalations"][0]["escalation_reason"] == "generation-exception"
        assert result["recommendation_escalations"][0]["escalation_errors"] == ["boom"]

    def test_healthy_recommendation_section_has_no_escalation(self):
        rec_graph = MagicMock()
        rec_graph.invoke = MagicMock(return_value={
            "recommendations": [{"id": "rec-1", "title": "Use therapy"}],
        })
        with patch.object(generation, "_build_dmn_subgraph") as dmn_builder, \
             patch.object(generation, "_build_rec_subgraph") as rec_builder:
            dmn_builder.return_value.compile.return_value = MagicMock()
            rec_builder.return_value.compile.return_value = rec_graph
            result = generate_all({
                "item_manifest": [{"type": "recommendation", "section": "Treatment"}],
                "markdown": "", "section_map": [],
            })
        assert result["recommendation_escalations"] == []

    def test_non_blocking_validation_warnings_reach_review_payload(self):
        graph = MagicMock()
        graph.invoke = MagicMock(return_value={
            "dmn_xml": "<definitions/>",
            "syntax_warnings": ["FIRST is order-dependent"],
        })
        result = self._run_with_graph(graph)
        assert result["dmn_results"][0]["validation_warnings"] == [
            "FIRST is order-dependent"
        ]

    def test_empty_result_preserves_escalation_reason_and_errors(self):
        graph = MagicMock()
        graph.invoke = MagicMock(return_value={
            "dmn_xml": "",
            "escalation_reason": "no-source-text",
            "escalation_errors": ["source text unavailable"],
        })
        result = self._run_with_graph(graph)
        entry = result["dmn_results"][0]
        assert entry["escalation_reason"] == "no-source-text"
        assert entry["escalation_errors"] == ["source text unavailable"]


class TestDmnEnginePreflight:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("DMN_PREFLIGHT_URL", raising=False)
        result = dmn_engine_preflight({"dmn_xml": "<definitions/>"})
        assert result == {"engine_errors": [], "engine_validation_warnings": []}

    @patch("cpg_ingester.generation.requests.post")
    def test_engine_errors_feed_repair_loop(self, mock_post, monkeypatch):
        monkeypatch.setenv("DMN_PREFLIGHT_URL", "http://engine/decisions/models")
        response = MagicMock(status_code=422, ok=False)
        response.json.return_value = {
            "messages": [{"severity": "ERROR", "text": "FEEL compilation failed"}],
        }
        mock_post.return_value = response

        result = dmn_engine_preflight({"dmn_xml": "<definitions/>"})

        assert result["engine_errors"] == ["FEEL compilation failed"]
        assert result["engine_validation_warnings"] == []
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0].endswith("?validate_only=true")

    @patch("cpg_ingester.generation.requests.post")
    def test_valid_reply_preserves_engine_warnings(self, mock_post, monkeypatch):
        monkeypatch.setenv("DMN_PREFLIGHT_URL", "http://engine/decisions/models")
        response = MagicMock(status_code=200, ok=True)
        response.json.return_value = {
            "valid": True,
            "messages": [{"severity": "WARN", "text": "gap"}],
        }
        mock_post.return_value = response

        result = dmn_engine_preflight({"dmn_xml": "<definitions/>"})

        assert result == {"engine_errors": [], "engine_validation_warnings": ["gap"]}

    @patch("cpg_ingester.generation.requests.post", side_effect=requests.ConnectionError("offline"))
    def test_unreachable_engine_continues_with_warning(self, _mock_post, monkeypatch):
        monkeypatch.setenv("DMN_PREFLIGHT_URL", "http://engine/decisions/models")

        result = dmn_engine_preflight({"dmn_xml": "<definitions/>"})

        assert result["engine_errors"] == []
        assert "DMN preflight unavailable" in result["engine_validation_warnings"][0]

    @patch("cpg_ingester.generation.requests.post")
    def test_warn_only_engine_messages_are_not_repair_errors(self, mock_post, monkeypatch):
        monkeypatch.setenv("DMN_PREFLIGHT_URL", "http://engine/decisions/models")
        response = MagicMock(status_code=200, ok=True)
        response.json.return_value = {
            "valid": False,
            "messages": [{"severity": "WARN", "text": "table gap"}],
        }
        mock_post.return_value = response

        result = dmn_engine_preflight({"dmn_xml": "<definitions/>"})

        assert result["engine_errors"] == []
        assert result["engine_validation_warnings"] == ["table gap"]
