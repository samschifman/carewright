"""Review feedback is rendered at the first creator attempt, without model calls."""

from cpg_ingester.review_feedback import render_reviewer_feedback


def test_feedback_is_rendered_with_round_and_manifest_context():
    block = render_reviewer_feedback(
        [{"itemId": "decision-1", "itemType": "decision", "comment": "Correct this item."}],
        "Keep the review scoped.",
        3,
        item_types={"decision"},
        item_context={"decision-1": "Decision item"},
        item_id="decision-1",
    )

    assert block == (
        "## Reviewer feedback from round 3\n"
        "- [decision] Decision item: Correct this item.\n"
        "- Overall comment: Keep the review scoped."
    )


def test_item_feedback_is_not_rendered_for_a_different_item():
    block = render_reviewer_feedback(
        [{"itemId": "decision-1", "itemType": "decision", "comment": "Only this item."}],
        item_types={"decision"},
        item_id="decision-2",
    )

    assert block == ""


def test_process_feedback_accepts_automation_api_alias():
    block = render_reviewer_feedback(
        [{"itemId": "process-1", "itemType": "automation", "comment": "Process note."}],
        item_types={"process"},
        item_id="process-1",
    )

    assert "[automation] process-1: Process note." in block


def test_generate_service_resolves_prior_result_ref_for_scoped_regeneration(monkeypatch):
    import cpg_ingester.generation as generation
    import cpg_ingester.services.llm_analysis as service

    prior = {"dmn_results": [{"item": {"id": "d1"}}]}
    captured = {}

    def fake_resolve_ref(data, name, _store):
        return {"item_manifest": []} if name == "analysis_result" else prior

    def fake_generate_all(state):
        captured.update(state)
        return {}

    monkeypatch.setattr(service, "resolve_ref", fake_resolve_ref)
    monkeypatch.setattr(service, "_store", None)
    monkeypatch.setattr(generation, "generate_all", fake_generate_all)

    service._do_generate({
        "analysis_result_ref": "analysis-ref",
        "prior_generate_result_ref": "previous-round-ref",
        "review_feedback": [{"itemId": "d1", "itemType": "decision", "comment": "Update."}],
        "review_iteration": 2,
    })

    assert captured["prior_generate_result"] == prior
    assert captured["review_iteration"] == 2


def test_structure_and_identifier_prompts_include_scoped_process_feedback(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from cpg_ingester.nodes import item_identifier, structure_analyzer

    feedback = [{"itemId": "process-1", "itemType": "process", "comment": "Keep this item."}]
    prior_manifest = [{"id": "process-1", "type": "process", "name": "Process One"}]
    llm = MagicMock()
    llm.invoke.side_effect = [
        SimpleNamespace(content='[{"heading":"1. Scope","classification":"process","reason":""}]'),
        SimpleNamespace(content='{"archetype":"institutional","reason":""}'),
        SimpleNamespace(content='{"definitions":""}'),
    ]
    monkeypatch.setattr(structure_analyzer, "get_llm", lambda _state: llm)
    structure_analyzer.structure_analyzer({
        "markdown": "# 1. Scope\nText.",
        "docling_json": {"texts": [{
            "label": "section_header", "text": "1. Scope",
            "prov": [{"page_no": 1}],
        }]},
        "prior_item_manifest": prior_manifest,
        "review_feedback": feedback,
        "review_iteration": 4,
        "output_dir": str(tmp_path),
    })
    classification_prompt = llm.invoke.call_args_list[0].args[0][1]["content"]
    assert "## Reviewer feedback from round 4" in classification_prompt
    assert "Process One: Keep this item." in classification_prompt

    identifier_llm = MagicMock()
    identifier_llm.invoke.return_value = SimpleNamespace(
        content='{"decisions":[],"recommendations":[],"processes":[]}'
    )
    monkeypatch.setattr(item_identifier, "get_llm", lambda _state: identifier_llm)
    item_identifier.item_identifier({
        "markdown": "Source text.", "section_map": [], "abbreviations": {},
        "prior_item_manifest": prior_manifest,
        "review_feedback": feedback, "review_iteration": 4,
        "output_dir": str(tmp_path),
    })
    identifier_prompt = identifier_llm.invoke.call_args.args[0][1]["content"]
    assert "## Reviewer feedback from round 4" in identifier_prompt
    assert "Process One: Keep this item." in identifier_prompt
