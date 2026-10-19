"""Scoped regeneration carries prior outputs outside the reviewed item ids."""

from unittest.mock import MagicMock, patch

from cpg_ingester.generation import generate_all


def test_item_feedback_regenerates_only_its_decision_rec_and_process():
    decision_1 = {"id": "decision-1", "model_id": "model-1", "type": "decision", "name": "One", "section": "S"}
    decision_2 = {"id": "decision-2", "model_id": "model-2", "type": "decision", "name": "Two", "section": "S"}
    rec_1 = {"id": "rec-1", "type": "recommendation", "name": "Rec One", "section": "S"}
    rec_2 = {"id": "rec-2", "type": "recommendation", "name": "Rec Two", "section": "S"}
    process_1 = {
        "id": "process-1", "type": "process", "name": "Process One", "tier_hint": "A",
        "linked_recommendations": ["rec-1"], "linked_decisions": [],
    }
    process_2 = {
        "id": "process-2", "type": "process", "name": "Process Two", "tier_hint": "A",
        "linked_recommendations": ["rec-2"], "linked_decisions": [],
    }
    prior = {
        "dmn_results": [
            {"item": decision_1, "dmn_xml": "old-one"},
            {"item": decision_2, "dmn_xml": "kept-two"},
        ],
        "recommendation_results": [
            {"id": "rec-1", "title": "old-one"},
            {"id": "rec-2", "title": "kept-two"},
        ],
        "recommendation_escalations": [],
        "bpmn_results": [
            {"item": process_1, "bpmn_xml": "old-one"},
            {"item": process_2, "bpmn_xml": "kept-two"},
        ],
    }
    dmn_graph = MagicMock()
    dmn_graph.invoke.return_value = {"dmn_xml": "<definitions/>"}
    rec_graph = MagicMock()
    rec_graph.invoke.return_value = {"recommendations": [{"id": "rec-1", "title": "new-one"}]}
    bpmn_graph = MagicMock()
    bpmn_graph.invoke.return_value = {
        "bpmn_xml": "<definitions/>", "process_ir": {"new": True}, "template_summary": {},
    }
    feedback = [
        {"itemId": "decision-1", "itemType": "decision", "comment": "Decision note."},
        {"itemId": "rec-1", "itemType": "recommendation", "comment": "Recommendation note."},
        {"itemId": "process-1", "itemType": "process", "comment": "Process note."},
    ]

    with (
        patch("cpg_ingester.generation._build_dmn_subgraph") as dmn_builder,
        patch("cpg_ingester.generation._build_rec_subgraph") as rec_builder,
        patch("cpg_ingester.generation._build_bpmn_subgraph") as bpmn_builder,
    ):
        dmn_builder.return_value.compile.return_value = dmn_graph
        rec_builder.return_value.compile.return_value = rec_graph
        bpmn_builder.return_value.compile.return_value = bpmn_graph
        result = generate_all({
            "item_manifest": [decision_1, decision_2, rec_1, rec_2, process_1, process_2],
            "markdown": "", "section_map": [], "review_feedback": feedback,
            "review_iteration": 2, "prior_generate_result": prior,
        })

    assert [call.args[0]["item"]["id"] for call in dmn_graph.invoke.call_args_list] == ["decision-1"]
    assert "Reviewer feedback from round 2" in dmn_graph.invoke.call_args.args[0]["human_feedback"]
    assert [item["id"] for item in rec_graph.invoke.call_args.args[0]["items"]] == ["rec-1"]
    assert "Recommendation note." in rec_graph.invoke.call_args.args[0]["human_feedback"]
    assert [call.args[0]["item"]["id"] for call in bpmn_graph.invoke.call_args_list] == ["process-1"]
    assert "Process note." in bpmn_graph.invoke.call_args.args[0]["human_feedback"]

    assert next(item for item in result["dmn_results"] if item["item"]["id"] == "decision-2")["dmn_xml"] == "kept-two"
    assert next(item for item in result["recommendation_results"] if item["id"] == "rec-2")["title"] == "kept-two"
    assert next(item for item in result["bpmn_results"] if item["item"]["id"] == "process-2")["bpmn_xml"] == "kept-two"


def test_decision_feedback_regenerates_linked_process_and_carries_unrelated_process():
    decision = {
        "id": "decision-1", "model_id": "model-one", "type": "decision",
        "name": "Monitoring Plan", "section": "S",
    }
    linked_process = {
        "id": "process-linked", "type": "process", "name": "Linked template",
        "tier_hint": "A", "linked_decisions": ["MONITORING PLAN"],
    }
    unrelated_process = {
        "id": "process-unrelated", "type": "process", "name": "Unrelated template",
        "tier_hint": "A", "linked_decisions": ["other-model"],
    }
    prior = {
        "dmn_results": [{"item": decision, "dmn_xml": "old decision"}],
        "recommendation_results": [],
        "recommendation_escalations": [],
        "bpmn_results": [
            {"item": linked_process, "bpmn_xml": "old linked"},
            {"item": unrelated_process, "bpmn_xml": "keep unrelated"},
        ],
    }
    dmn_graph = MagicMock()
    dmn_graph.invoke.return_value = {"dmn_xml": "new decision"}
    bpmn_graph = MagicMock()
    bpmn_graph.invoke.return_value = {
        "bpmn_xml": "new linked", "process_ir": {"new": True}, "template_summary": {},
    }

    with (
        patch("cpg_ingester.generation._build_dmn_subgraph") as dmn_builder,
        patch("cpg_ingester.generation._build_rec_subgraph") as rec_builder,
        patch("cpg_ingester.generation._build_bpmn_subgraph") as bpmn_builder,
    ):
        dmn_builder.return_value.compile.return_value = dmn_graph
        rec_builder.return_value.compile.return_value = MagicMock()
        bpmn_builder.return_value.compile.return_value = bpmn_graph
        result = generate_all({
            "item_manifest": [decision, linked_process, unrelated_process],
            "markdown": "", "section_map": [],
            "review_feedback": [
                {"itemId": "MODEL-ONE", "itemType": "decision", "comment": "Update this decision."}
            ],
            "review_iteration": 1,
            "prior_generate_result": prior,
        })

    assert [call.args[0]["item"]["id"] for call in dmn_graph.invoke.call_args_list] == ["decision-1"]
    assert [call.args[0]["item"]["id"] for call in bpmn_graph.invoke.call_args_list] == ["process-linked"]
    results = {item["item"]["id"]: item for item in result["bpmn_results"]}
    assert results["process-linked"]["bpmn_xml"] == "new linked"
    assert results["process-unrelated"]["bpmn_xml"] == "keep unrelated"
