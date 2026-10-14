"""CLI for the ProcessIR creator benchmark."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
_API_KEY_ENV_ORDER = ("LLM_API_KEY", "LITELLM_API_KEY", "OPENAI_API_KEY")


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
    model = os.environ.get("LLM_MODEL", "").strip()
    if not model:
        raise click.ClickException(
            "Live creator benchmark requires LLM_MODEL. Configure it in the agent environment."
        )
    key_name = next(
        (name for name in _API_KEY_ENV_ORDER if os.environ.get(name, "").strip()),
        None,
    )
    key = os.environ.get(key_name, "").strip() if key_name else ""
    if not key:
        raise click.ClickException(
            "Live creator benchmark requires a configured LLM API key environment variable."
        )
    if key.casefold() in {"sk-change-me", "change-me", "placeholder"}:
        raise click.ClickException("The configured LLM API key is a placeholder; benchmark was not started.")
    endpoint = (
        os.environ.get("LITELLM_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or os.environ.get("OPENAI_API_BASE", "").strip()
    )
    if not endpoint and key_name == "OPENAI_API_KEY":
        endpoint = "https://api.openai.com"
    if not endpoint:
        raise click.ClickException(
            "Live creator benchmark requires LITELLM_URL or an OpenAI-compatible base URL."
        )
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    return {
        "litellm_url": endpoint,
        "llm_model": model,
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
        # This harness intentionally uses a local file store when no remote
        # tracking URI is configured; recent MLflow versions require opt-in.
        os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(config["mlflow_experiment"])


def _select_templates(
    templates: list[dict], *, section_range: str | None = None, golden: str | None = None
) -> list[dict]:
    """Filter the corpus to a section range and/or one golden template id."""
    bounds = None
    if section_range:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*", section_range)
        if not match:
            raise click.BadParameter("use a section number or inclusive range such as 3.5-3.6")
        try:
            start = Decimal(match.group(1))
            end = Decimal(match.group(2) or match.group(1))
        except InvalidOperation as exc:
            raise click.BadParameter("section bounds must be numeric") from exc
        if end < start:
            raise click.BadParameter("section range end must not precede its start")
        bounds = (start, end)

    golden_id = None
    if golden:
        golden_id = golden.removesuffix(".template.json").removesuffix(".ir.json")
        golden_id = Path(golden_id).name

    selected = []
    for template in templates:
        if golden_id and template.get("id") != golden_id:
            continue
        if bounds:
            match = re.match(r"\s*(\d+(?:\.\d+)?)", str(template.get("section", "")))
            if not match:
                continue
            section_number = Decimal(match.group(1))
            if not bounds[0] <= section_number <= bounds[1]:
                continue
        selected.append(template)
    return selected


@click.command()
@click.option("--suite", type=click.Choice(["creator"]), default="creator")
@click.option("--corpus", default="hypertension-v2", show_default=True)
@click.option("--prompt-rev", default="baseline", show_default=True)
@click.option("--section", "section_range", help="Restrict to a section number or inclusive range, e.g. 3.5-3.6.")
@click.option("--golden", help="Restrict to one golden template id or filename.")
def main(suite: str, corpus: str, prompt_rev: str, section_range: str | None, golden: str | None) -> None:
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
    selected_templates = _select_templates(
        corpus_entry.get("templates", []),
        section_range=section_range,
        golden=golden,
    )
    if not selected_templates:
        raise click.ClickException(
            "No benchmark templates match the requested section and golden filters."
        )
    corpus_entry = {
        **corpus_entry,
        "templates": selected_templates,
        "all_templates": corpus_entry.get("templates", []),
    }
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
        "section": section_range,
        "golden": golden,
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
