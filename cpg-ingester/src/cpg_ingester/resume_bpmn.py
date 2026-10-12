"""Resume only BPMN generation from a saved cpg-ingester run directory."""

import json
import logging
import os
import sys
from pathlib import Path

import mlflow
from cpg_ingester.generation import generate_bpmn_only
from cpg_ingester.nodes.assembly import assembly
from cpg_ingester.nodes.delivery import delivery
from cpg_ingester.nodes.metadata_extractor import _extract_front_matter
from cpg_ingester.output import write_artifact

logger = logging.getLogger(__name__)


def resume_bpmn(run_dir: Path, *, litellm_url: str, llm_model: str) -> dict:
    """Rerun process subgraphs and reassemble/publish, reusing other artifacts."""
    analysis_path = run_dir / "analysis-result.json"
    generation_path = run_dir / "generation-result.json"
    markdown_path = run_dir / "parsed.md"
    for required in (analysis_path, generation_path, markdown_path):
        if not required.is_file():
            raise FileNotFoundError(f"Saved BPMN resume input is missing: {required}")

    analysis = json.loads(analysis_path.read_text())
    generation = json.loads(generation_path.read_text())
    markdown = markdown_path.read_text()
    metadata = dict(analysis.get("cpg_metadata") or {})
    for field in ("cpg_id", "version", "effective_date"):
        if field in (front_matter := _extract_front_matter(markdown)):
            metadata[field] = front_matter[field]

    state = {
        **analysis,
        "markdown": markdown,
        "section_map": analysis.get("section_map") or _read_json(run_dir / "section-map.json", []),
        "abbreviations": _read_json(run_dir / "abbreviations.json", {}),
        "cpg_metadata": metadata,
        "dmn_results": generation.get("dmn_results", []),
        "recommendation_results": generation.get("recommendation_results", []),
        "recommendation_escalations": generation.get("recommendation_escalations", []),
        "output_dir": str(run_dir),
        "litellm_url": litellm_url,
        "llm_model": llm_model,
    }
    state.update(generate_bpmn_only(state))
    generation["bpmn_results"] = state["bpmn_results"]
    state.update(generation)
    assembly_result = assembly(state)
    state.update(assembly_result)
    delivery_result = delivery(state)
    state.update(delivery_result)
    generation["bpmn_results"] = state.get("bpmn_results", [])
    generation["escalated_items"] = state.get("escalated_items", [])

    write_artifact(str(run_dir), "analysis-result.json", {
        **analysis,
        "cpg_metadata": metadata,
    })
    write_artifact(str(run_dir), "generation-result.json", {
        key: generation.get(key, [])
        for key in (
            "dmn_results", "recommendation_results", "bpmn_results",
            "recommendation_escalations", "escalated_items",
        )
    })
    previous_summary = _read_json(run_dir / "run-summary.json", {})
    previous_summary.update({
        "cpg_metadata": metadata,
        "bpmn_results": len(state.get("bpmn_results", [])),
        "escalated_items": len(state.get("escalated_items", [])),
        "delivery_status": state.get("delivery_status"),
        "resumed_bpmn_only": True,
    })
    write_artifact(str(run_dir), "run-summary.json", previous_summary)
    logger.info(
        "BPMN resume finished: %d process item(s), %d escalated",
        len(state.get("bpmn_results", [])),
        sum(bool(item.get("escalated")) for item in state.get("bpmn_results", [])),
    )
    return state


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m cpg_ingester.resume_bpmn RUN_DIR")
    try:
        mlflow.langchain.autolog()
    except Exception:
        pass
    experiment_name = os.environ.get("MLFLOW_EXPERIMENT_NAME")
    if experiment_name:
        mlflow.set_experiment(experiment_name)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    run_dir = Path(sys.argv[1]).resolve()
    resume_bpmn(
        run_dir,
        litellm_url=os.environ.get("LITELLM_URL", "http://localhost:4000"),
        llm_model=os.environ.get("LLM_MODEL", "default"),
    )


if __name__ == "__main__":
    main()
