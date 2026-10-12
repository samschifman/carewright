"""CLI entry point for care plan generation.

Reads a FHIR IPS Bundle from a file and runs the LangGraph
care plan composition pipeline.
"""

import json
import logging
import os
import uuid

import click

from acp_writer.pipeline import build_pipeline
from acp_writer import api


@click.command()
@click.argument("bundle_file", type=click.Path(exists=True))
@click.option("--litellm-url", default=None, help="LiteLLM proxy URL.")
@click.option("--model", default=None, help="LLM model name.")
@click.option("--output-dir", "-o", type=click.Path(), default=None, help="Output directory for artifacts.")
def main(bundle_file: str, litellm_url: str, model: str, output_dir: str):
    """Generate a FHIR CarePlan from a patient IPS Bundle."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    with open(bundle_file) as f:
        bundle = json.load(f)

    run_id = str(uuid.uuid4())[:8]
    if not output_dir:
        output_dir = f"output/{run_id}"

    graph = build_pipeline(
        automation_registry=api._automation_registry,
        automation_validator=api._automation_validation_result,
        guidelines_store=api._guidelines_store,
        vector_store=api._vector_store,
        decision_models_provider=lambda: [
            entry["summary"].model_dump(mode="json")
            for entry in api._dynamic_models.values()
        ],
    )
    compiled = graph.compile()

    result = compiled.invoke({
        "ips_bundle": bundle,
        "run_id": run_id,
        "output_dir": output_dir,
        "litellm_url": litellm_url or os.environ.get("LITELLM_URL", "http://localhost:4000"),
        "llm_model": model or os.environ.get("LLM_MODEL", "default"),
    })

    fhir_bundle = result.get("fhir_bundle", {})
    careplan_json = json.dumps(fhir_bundle, indent=2)

    click.echo(f"Run ID: {run_id}")
    click.echo(f"Delivery status: {result.get('delivery_status', 'unknown')}")
    click.echo(f"Artifacts: {output_dir}")

    if result.get("careplan_id"):
        click.echo(f"Care plan ID: {result['careplan_id']}")


if __name__ == "__main__":
    main()
