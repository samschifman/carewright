"""CLI entrypoint for the DMN generation-quality benchmark.

    ./run-benchmark.sh --suite creator|reviewer|all --corpus hypertension

(or run this file directly with the cpg-ingester venv python). One MLflow run per
invocation: params record the prompt version (git describe + a manual --prompt-rev
tag), metrics record per-suite scores. Results are also written as JSON to the
report directory for eyeballing. LLM configuration is read from the environment
exactly as the pipeline reads it (LITELLM_URL / LLM_MODEL / LITELLM_API_KEY).
"""

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
from mlflow.exceptions import MlflowException

# Non-package benchmark: make sibling modules (creator_eval, reviewer_eval, ...)
# importable whether invoked via the wrapper, `python run_benchmark.py`, or pytest.
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

logger = logging.getLogger(__name__)

# tests/benchmarks/dmn/run_benchmark.py -> parents[3]=cpg-ingester, parents[4]=repo root.
INGESTER_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = Path(__file__).resolve().parents[4]


def _git_describe() -> str:
    try:
        return subprocess.check_output(
            ["git", "describe", "--always", "--dirty"], cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _llm_config() -> dict:
    return {
        "litellm_url": os.environ.get("LITELLM_URL", "http://localhost:4000"),
        "llm_model": os.environ.get("LLM_MODEL", "default"),
    }


def _load_config() -> dict:
    return yaml.safe_load((BENCH_DIR / "config.yaml").read_text())


def _flatten_metrics(prefix: str, d: dict) -> dict:
    """Pull scalar metrics out of a nested report for MLflow logging."""
    out = {}
    for k, v in d.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[f"{prefix}.{k}"] = v
        elif isinstance(v, dict):
            out.update(_flatten_metrics(f"{prefix}.{k}", v))
    return out


def _expand_corpora(corpus: str, config: dict) -> list[str]:
    """Expand the CLI selector into configured corpus names."""
    if corpus == "all":
        return list(config.get("corpora", []))
    return [corpus]


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _configure_mlflow(config: dict) -> None:
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    try:
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(config["mlflow_experiment"])
    except MlflowException as exc:
        logger.warning("MLflow tracking unavailable (%s); using local file store", exc)
        mlflow.set_tracking_uri(f"file://{INGESTER_ROOT / 'mlruns'}")
        mlflow.set_experiment(config["mlflow_experiment"])


@click.command()
@click.option("--suite", type=click.Choice(["creator", "reviewer", "all"]), default="all")
@click.option("--corpus", default="hypertension")
@click.option("--prompt-rev", default="baseline", help="Manual prompt-version tag.")
@click.option("--no-compile", is_flag=True, help="Skip decision-service compile checks.")
@click.option("--skip-real-corpora", is_flag=True, help="Skip the no-golden real corpora.")
def main(suite: str, corpus: str, prompt_rev: str, no_compile: bool, skip_real_corpora: bool):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config = _load_config()
    manifest = yaml.safe_load((BENCH_DIR / config["corpus_manifest"]).read_text())
    llm_config = _llm_config()
    holdout = set(config.get("reviewer_holdout_classes", []))
    report_dir = (INGESTER_ROOT / config["report_dir"]).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    _configure_mlflow(config)
    names = _expand_corpora(corpus, config)
    if corpus != "all":
        report, _creator_records, _reviewer_cases = _run_corpus(
            suite, names[0], prompt_rev, no_compile, skip_real_corpora,
            config, manifest, llm_config, holdout, report_dir,
        )
        _print_summary(report)
        return

    reports = {}
    creator_records = []
    reviewer_cases = []
    for name in names:
        report, records, cases = _run_corpus(
            suite, name, prompt_rev, no_compile, skip_real_corpora,
            config, manifest, llm_config, holdout, report_dir,
        )
        reports[name] = report
        creator_records.extend(records)
        reviewer_cases.extend(cases)

    overall = {"suite": suite, "corpus": "all", "prompt_rev": prompt_rev,
               "git": _git_describe(), "corpora": reports}
    if suite in ("creator", "all"):
        from creator_eval import _aggregate
        overall["overall"] = overall.get("overall", {})
        overall["overall"]["creator"] = _aggregate(creator_records)
    if suite in ("reviewer", "all"):
        from reviewer_eval import _score
        overall["overall"] = overall.get("overall", {})
        overall["overall"]["reviewer"] = _score(reviewer_cases)

    with mlflow.start_run(run_name=f"{suite}-overall-{prompt_rev}"):
        mlflow.log_params({"suite": suite, "corpus": "all", "prompt_rev": prompt_rev,
                           "git": overall["git"], "llm_model": llm_config["llm_model"]})
        mlflow.log_metrics(_flatten_metrics("overall", overall.get("overall", {})))
        out_path = report_dir / f"eval-{suite}-all-{prompt_rev}-{_timestamp()}.json"
        out_path.write_text(json.dumps(overall, indent=2, default=str))
        mlflow.log_artifact(str(out_path))
    _print_summary(overall.get("overall", {}))


def _run_corpus(suite: str, corpus_name: str, prompt_rev: str, no_compile: bool,
                skip_real_corpora: bool, config: dict, manifest: dict,
                llm_config: dict, holdout: set[str], report_dir: Path):
    """Run one corpus and return its report plus raw records for pooling."""
    from creator_eval import run_creator_suite, score_generated_corpus
    from reviewer_eval import run_reviewer_suite

    corpus_entry = manifest["corpora"][corpus_name]
    markdown = (INGESTER_ROOT / corpus_entry["source_cpg"]).read_text()
    run_dir = report_dir / f"{suite}-{corpus_name}-{prompt_rev}-{_timestamp()}"
    (run_dir / "final").mkdir(parents=True, exist_ok=True)
    report = {"suite": suite, "corpus": corpus_name, "prompt_rev": prompt_rev,
              "git": _git_describe()}
    creator_records = []
    reviewer_cases = []

    with mlflow.start_run(run_name=f"{suite}-{corpus_name}-{prompt_rev}"):
        mlflow.log_params({"suite": suite, "corpus": corpus_name,
                           "prompt_rev": prompt_rev, "git": report["git"],
                           "llm_model": llm_config["llm_model"]})
        if suite in ("creator", "all"):
            logger.info("Running creator suite on %s ...", corpus_name)
            creator_report, creator_records = run_creator_suite(
                corpus_entry, markdown, llm_config, str(run_dir), INGESTER_ROOT,
                run_compile=not no_compile, return_records=True,
            )
            report["creator"] = creator_report
            for record in creator_records:
                if record.final_dmn:
                    decision_id = next(
                        decision["id"] for decision in corpus_entry["decisions"]
                        if decision["name"] == record.decision
                    )
                    path = run_dir / "final" / f"{decision_id}.dmn"
                    path.write_text(record.final_dmn)
            mlflow.log_metrics(_flatten_metrics("creator", creator_report))

            if not skip_real_corpora:
                report["real_corpora"] = {}
                for rel in config.get("real_corpora", []):
                    directory = REPO_ROOT / rel
                    if not directory.exists():
                        logger.warning("Real corpus missing: %s", rel)
                        continue
                    paths = sorted(directory.glob("*.dmn"))
                    if paths:
                        report["real_corpora"][rel] = score_generated_corpus(
                            paths, run_compile=not no_compile)
                mlflow.log_metrics(_flatten_metrics("real", report["real_corpora"]))

        if suite in ("reviewer", "all"):
            logger.info("Running reviewer suite on %s ...", corpus_name)
            reviewer_report, reviewer_cases = run_reviewer_suite(
                corpus_entry, markdown, llm_config, str(run_dir), holdout, INGESTER_ROOT,
                return_cases=True,
            )
            report["reviewer"] = reviewer_report
            mlflow.log_metrics(_flatten_metrics("reviewer", reviewer_report))

        mlflow.log_artifacts(str(run_dir))
        out_path = report_dir / f"eval-{suite}-{corpus_name}-{prompt_rev}-{_timestamp()}.json"
        out_path.write_text(json.dumps(report, indent=2, default=str))
        mlflow.log_artifact(str(out_path))
        latest = report_dir / "latest"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(out_path.name)
        logger.info("Report written to %s", out_path)
    return report, creator_records, reviewer_cases


def _print_summary(report: dict):
    click.echo("\n=== Eval summary ===")
    if "overall" in report and isinstance(report["overall"], dict):
        report = report["overall"]
    if "creator" in report:
        c = report["creator"]
        click.echo(f"Creator: first-attempt L0 {c.get('first_attempt_validity_rate')}, "
                   f"escalation {c.get('escalation_rate')}, "
                   f"mean F1 {c.get('mean_structural_f1')}, "
                   f"decision-exact {c.get('decision_exact_rate')}, "
                   f"compile-pass {c.get('compile_pass_rate')}, "
                   f"L0-pass-but-compile-fail {c.get('l0_pass_but_compile_fail')}")
    if "reviewer" in report:
        r = report["reviewer"]
        click.echo(f"Reviewer: recall {r.get('overall_recall')}, "
                   f"precision {r.get('overall_precision')}, "
                   f"false-escalation {r.get('false_escalation_rate')}")


if __name__ == "__main__":
    main()
