"""Deploy extracted DMN models and BPMN automation templates to acp-writer."""

import json
import logging
from pathlib import Path

import click
import mlflow
import requests

from cpg_contracts import DecisionModelSummary

logger = logging.getLogger(__name__)


@mlflow.trace(name="deploy_dmn")
def deploy_dmn(
    dmn_path: Path,
    acp_writer_url: str,
    source_cpg: str | None = None,
    replace: bool = False,
) -> DecisionModelSummary:
    """POST a DMN file to the acp-writer decisions API."""
    dmn_xml = dmn_path.read_text()

    params = {}
    if source_cpg:
        params["source_cpg"] = source_cpg
    if replace:
        params["replace"] = "true"

    r = requests.post(
        f"{acp_writer_url}/api/v1/decisions/models",
        data=dmn_xml,
        headers={"Content-Type": "application/xml"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()

    summary = DecisionModelSummary.model_validate(r.json())
    logger.info(
        "Deployed %s (%s): %d inputs, %d outputs",
        summary.name, summary.id, len(summary.inputs), len(summary.outputs),
    )
    return summary


@mlflow.trace(name="deploy_automation_template")
def deploy_template(
    template_path: Path,
    acp_writer_url: str,
    source_cpg: str | None = None,
) -> dict:
    """POST a versioned AutomationTemplate JSON to the acp-writer registry."""
    payload = json.loads(template_path.read_text())
    params = {"source_cpg": source_cpg} if source_cpg else {}
    response = requests.post(
        f"{acp_writer_url}/api/v1/automation/templates",
        json=payload,
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError:
        result = {}
    summary = result.get("summary", result) if isinstance(result, dict) else {}
    logger.info(
        "Deployed automation template %s version %s",
        summary.get("id", template_path.stem),
        summary.get("version", "unknown"),
    )
    return result


@click.command()
@click.argument("dmn_files", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--template",
    "template_files",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="AutomationTemplate JSON to register; may be repeated.",
)
@click.option("--acp-writer-url", default="http://localhost:8082", help="ACP Writer base URL.")
@click.option("--source-cpg", default=None, help="CPG ID to link deployed models to.")
@click.option("--replace", is_flag=True, help="Replace a colliding model from another source CPG.")
def main(
    dmn_files: tuple[Path, ...],
    template_files: tuple[Path, ...],
    acp_writer_url: str,
    source_cpg: str | None,
    replace: bool,
):
    """Deploy DMN files and automation templates to the acp-writer service."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not dmn_files and not template_files:
        click.echo("No DMN files or templates specified.")
        return

    for dmn_path in dmn_files:
        try:
            summary = deploy_dmn(dmn_path, acp_writer_url, source_cpg=source_cpg, replace=replace)
            click.echo(f"  Deployed: {summary.name} ({summary.id})")
        except requests.HTTPError as e:
            click.echo(f"  FAILED: {dmn_path.name} — {e}", err=True)
        except requests.ConnectionError:
            click.echo(f"  FAILED: {dmn_path.name} — cannot connect to {acp_writer_url}", err=True)

    failures: list[tuple[str, str]] = []
    for template_path in template_files:
        try:
            result = deploy_template(
                template_path, acp_writer_url, source_cpg=source_cpg
            )
            summary = result.get("summary", result) if isinstance(result, dict) else {}
            click.echo(
                f"  Deployed template: "
                f"{summary.get('name') or summary.get('id') or template_path.stem}"
            )
        except requests.HTTPError as exc:
            body = exc.response.text if exc.response is not None else ""
            detail = body or str(exc)
            click.echo(f"  FAILED: {template_path.name} — {exc}", err=True)
            if body:
                click.echo(body, err=True)
            logger.error("Template registration rejected for %s: %s", template_path, detail)
            failures.append((str(template_path), detail))
        except requests.RequestException as exc:
            detail = str(exc)
            click.echo(
                f"  FAILED: {template_path.name} — {exc}",
                err=True,
            )
            logger.error("Template registration failed for %s: %s", template_path, detail)
            failures.append((str(template_path), detail))
    if failures:
        click.echo("Template registration failures:", err=True)
        for template_path, detail in failures:
            click.echo(f"  {template_path}: {detail}", err=True)
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    main()
