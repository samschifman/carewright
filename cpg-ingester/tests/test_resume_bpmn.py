import json
from pathlib import Path
from unittest.mock import patch

from cpg_ingester import resume_bpmn as resume_module


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) if not isinstance(value, str) else value)


def test_resume_reuses_saved_analysis_and_redrives_only_bpmn(tmp_path):
    run_dir = tmp_path / "saved-run"
    _write(run_dir / "parsed.md", (
        "Guideline ID: SYN-HTN-2026-002 Version: 2.0 "
        "Effective Date: October 2026 Issuing Body: Example\n"
        "## 3.5 Home monitoring\nReview readings.\n"
    ))
    _write(run_dir / "analysis-result.json", {
        "cpg_metadata": {"cpg_id": "UNK-HTN-UNDATED"},
        "section_map": [{"heading": "3.5 Home monitoring"}],
        "item_manifest": [{"type": "process", "name": "Home monitoring"}],
    })
    _write(run_dir / "generation-result.json", {
        "dmn_results": [{"item": {"name": "Existing decision"}}],
        "recommendation_results": [{"title": "Existing recommendation"}],
        "bpmn_results": [],
    })
    _write(run_dir / "run-summary.json", {"run_id": "saved-run"})
    _write(run_dir / "abbreviations.json", {"BP": "blood pressure"})
    generated_state = {}

    def generate_bpmn_only(state):
        generated_state.update(state)
        return {"bpmn_results": [{"item": {"name": "Home monitoring"}, "escalated": False}]}

    with patch.object(resume_module, "generate_bpmn_only", side_effect=generate_bpmn_only), \
            patch.object(resume_module, "assembly", return_value={"escalated_items": []}), \
            patch.object(resume_module, "delivery", return_value={
                "delivery_status": {"published": True},
            }):
        result = resume_module.resume_bpmn(
            run_dir, litellm_url="https://api.openai.com", llm_model="gpt-6-luna"
        )

    assert generated_state["dmn_results"][0]["item"]["name"] == "Existing decision"
    assert generated_state["recommendation_results"][0]["title"] == "Existing recommendation"
    assert "artifact_store" not in generated_state
    assert generated_state["cpg_metadata"]["cpg_id"] == "SYN-HTN-2026-002"
    assert generated_state["cpg_metadata"]["effective_date"] == "October 2026"
    assert result["delivery_status"]["published"] is True
    assert json.loads((run_dir / "generation-result.json").read_text())["bpmn_results"]
