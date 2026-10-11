"""CLI entrypoint for the cpg-ingester pipeline."""

import json
import logging
import uuid
from pathlib import Path

import click
import mlflow
from langgraph.checkpoint.memory import MemorySaver

from cpg_ingester.output import write_artifact
from cpg_ingester.pipeline import build_pipeline

try:
    mlflow.langchain.autolog()
except Exception:
    pass


@click.command()
@click.argument("input_pdf", type=click.Path(exists=True, path_type=Path))
@click.option("--output-dir", "-o", type=click.Path(path_type=Path), default=None)
@click.option("--litellm-url", envvar="LITELLM_URL", default="http://localhost:4000")
@click.option("--model", envvar="LLM_MODEL", default="default")
@click.option("--api-key", envvar="LITELLM_API_KEY", default="sk-change-me")
def main(
    input_pdf: Path,
    output_dir: Path | None,
    litellm_url: str,
    model: str,
    api_key: str,
):
    """Run the cpg-ingester pipeline on a CPG PDF."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    run_id = str(uuid.uuid4())[:8]
    if output_dir is None:
        output_dir = Path("output") / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    graph = build_pipeline()
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)

    initial_state = {
        "run_id": run_id,
        "output_dir": str(output_dir),
        "pdf_path": str(input_pdf),
        "litellm_url": litellm_url,
        "llm_model": model,
        "llm_api_key": api_key,
    }

    config = {"configurable": {"thread_id": run_id}}

    click.echo(f"Starting cpg-ingester pipeline (run: {run_id})")
    click.echo(f"  PDF: {input_pdf}")
    click.echo(f"  Output: {output_dir}")
    click.echo(f"  LLM: {litellm_url} (model: {model})")

    result = compiled.invoke(initial_state, config=config)

    write_artifact(str(output_dir), "analysis-result.json", {
        "cpg_metadata": result.get("cpg_metadata", {}),
        "section_map": result.get("section_map", []),
        "item_manifest": result.get("item_manifest", []),
    })
    write_artifact(str(output_dir), "generation-result.json", {
        "dmn_results": result.get("dmn_results", []),
        "recommendation_results": result.get("recommendation_results", []),
        "bpmn_results": result.get("bpmn_results", []),
        "recommendation_escalations": result.get("recommendation_escalations", []),
        "escalated_items": result.get("escalated_items", []),
    })

    write_artifact(str(output_dir), "run-summary.json", {
        "run_id": run_id,
        "pdf_path": str(input_pdf),
        "manifest_items": len(result.get("item_manifest", [])),
        "dmn_results": len(result.get("dmn_results", [])),
        "recommendations": len(result.get("recommendation_results", [])),
        "escalated_items": len(result.get("escalated_items", [])),
        "delivery_status": result.get("delivery_status"),
    })

    click.echo(f"\nPipeline complete (run: {run_id})")
    click.echo(f"  Artifacts written to: {output_dir}")


if __name__ == "__main__":
    main()
