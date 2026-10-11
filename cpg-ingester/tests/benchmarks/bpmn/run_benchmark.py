"""CLI for the ProcessIR creator benchmark."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import mlflow
import yaml

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

INGESTER_ROOT = BENCH_DIR.parents[2]
REPO_ROOT = INGESTER_ROOT.parent
logger = logging.getLogger(__name__)
REQUIRED_LLM_ENV = ("LITELLM_URL", "LLM_MODEL", "LITELLM_API_KEY")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _git_describe() -> str:
    try:
        return subprocess.check_output(
            ["git", "describe", "--always", "--dirty"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict):
        raise click.ClickException(f"Expected a YAML mapping in {path}")
    return value


def _llm_config() -> dict:
    missing = [
        name for name in REQUIRED_LLM_ENV
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise click.ClickException(
            "Live creator benchmark requires these environment variables: "
            + ", ".join(missing)
            + ". Configure them in the agent environment; do not use placeholder credentials."
        )
    key = os.environ["LITELLM_API_KEY"].strip()
    if key.casefold() in {"sk-change-me", "change-me", "placeholder"}:
        raise click.ClickException("LITELLM_API_KEY is a placeholder; benchmark was not started.")
    return {
        "litellm_url": os.environ["LITELLM_URL"].strip().rstrip("/"),
        "llm_model": os.environ["LLM_MODEL"].strip(),
        "llm_api_key": key,
    }


def _scalar_metrics(value: dict, prefix: str = "") -> dict[str, float]:
    result = {}
    for name, item in value.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(item, bool):
            result[key] = float(item)
        elif isinstance(item, (int, float)):
            result[key] = float(item)
        elif isinstance(item, dict):
            result.update(_scalar_metrics(item, key))
    return result


def _configure_mlflow(config: dict) -> None:
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "").strip()
    if not tracking_uri:
        local_dir = (INGESTER_ROOT / config["local_tracking_dir"]).resolve()
        local_dir.mkdir(parents=True, exist_ok=True)
        tracking_uri = local_dir.as_uri()
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(config["mlflow_experiment"])


@click.command()
@click.option("--suite", type=click.Choice(["creator"]), default="creator")
@click.option("--corpus", default="hypertension-v2", show_default=True)
@click.option("--prompt-rev", default="baseline", show_default=True)
def main(suite: str, corpus: str, prompt_rev: str) -> None:
    """Run the live BPMN creator suite and write its reproducible report."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    llm_config = _llm_config()
    config = _load_yaml(BENCH_DIR / "config.yaml")
    manifest = _load_yaml(BENCH_DIR / config["corpus_manifest"])
    corpora = manifest.get("corpora", {})
    if corpus not in corpora:
        raise click.ClickException(
            f"Unknown corpus {corpus!r}; configured corpora: {', '.join(sorted(corpora))}"
        )

    corpus_entry = corpora[corpus]
    markdown_path = INGESTER_ROOT / corpus_entry["source_cpg"]
    if not markdown_path.is_file():
        raise click.ClickException(f"Source CPG not found: {markdown_path}")
    report_dir = (INGESTER_ROOT / config["report_dir"]).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    run_dir = report_dir / f"creator-{corpus}-{prompt_rev}-{_timestamp()}"
    run_dir.mkdir(parents=True, exist_ok=True)

    from creator_eval import run_creator_suite

    _configure_mlflow(config)
    report = {
        "suite": suite,
        "corpus": corpus,
        "prompt_rev": prompt_rev,
        "git": _git_describe(),
        "model": llm_config["llm_model"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "creator": run_creator_suite(
            corpus_entry,
            markdown_path.read_text(),
            llm_config,
            run_dir / "artifacts",
            INGESTER_ROOT,
        ),
    }
    report_path = report_dir / (
        f"eval-{suite}-{corpus}-{prompt_rev}-{_timestamp()}.json"
    )
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    metrics = _scalar_metrics(report["creator"]["summary"], "creator")
    with mlflow.start_run(run_name=f"{suite}-{corpus}-{prompt_rev}"):
        mlflow.log_params({
            "suite": suite,
            "corpus": corpus,
            "prompt_rev": prompt_rev,
            "git": report["git"],
            "model": report["model"],
        })
        if metrics:
            mlflow.log_metrics(metrics)
        mlflow.log_artifact(str(report_path))
        mlflow.log_artifacts(str(run_dir / "artifacts"))

    summary = report["creator"]["summary"]
    click.echo(f"Report: {report_path}")
    click.echo(
        "Headline: "
        f"accepted_rate={summary['accepted_rate']:.3f}, "
        f"mean_attempts={summary['mean_attempts']:.2f}, "
        f"escalation_rate={summary['escalation_rate']:.3f}, "
        f"mean_latency_seconds={summary['mean_latency_seconds']:.2f}, "
        f"total_tokens={summary['total_tokens']}"
    )


if __name__ == "__main__":
    main()
