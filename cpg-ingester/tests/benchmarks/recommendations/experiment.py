"""Run and publish one recommendation extraction experiment."""

import argparse
import hashlib
import importlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import mlflow
from prompt_eval.capture import Capture
from prompt_eval.models import CaseRef, ModelSpec, StageConfig
from prompt_eval.security import Redactor, digest, traced
from prompt_eval.storage import LocalStore

from .dataset import load_case
from .extractor_eval import ExtractorAdapter, normalize_extractor_rows
from .scoring import score

STAGE = "recommendation-extractor"


@traced
def replay_case(ref, case_root, rows):
    case = load_case(ref, case_root)
    return score(case, normalize_extractor_rows(case, rows))


@traced
def run(args):
    dataset = json.loads(args.dataset.read_bytes())
    refs = [CaseRef.model_validate(item) for item in dataset["cases"]]
    if not refs or len({ref.case_id for ref in refs}) != len(refs):
        raise ValueError("Select a nonempty manifest with unique case IDs")
    case_root = args.dataset.parent / dataset["case_root"]
    for ref in refs:
        case = load_case(ref, case_root, require_reviewed=True)
        if case.dataset_version != dataset["version"]:
            raise ValueError("Case dataset version differs from manifest")

    module = importlib.import_module("cpg_ingester.nodes.rec_extractor")
    prompt_uri = args.prompt if args.prompt.startswith("prompts:/") else None
    if prompt_uri:
        if not prompt_uri.rsplit("/", 1)[-1].isdigit() or "@" in prompt_uri:
            raise ValueError("Select an exact numeric prompt registry version")
        system = mlflow.genai.load_prompt(prompt_uri).template
    else:
        system = Path(args.prompt).read_bytes().decode("utf-8")
    if not isinstance(system, str) or not system.strip():
        raise ValueError("Select a nonblank system prompt")
    templates = {"system": system, "user": module.REC_EXTRACTOR_USER}
    model = ModelSpec.model_validate_json(args.model_config.read_bytes())
    stage = StageConfig(
        name=STAGE,
        configuration={"version": "1", "digest": digest(dataset)},
        evaluator={
            "version": "1.0.0",
            "digest": digest(
                {
                    name: (Path(__file__).parent / name).read_text()
                    for name in (
                        "dataset.py",
                        "extractor_eval.py",
                        "matching.py",
                        "models.py",
                        "scoring.py",
                        "text.py",
                    )
                }
            ),
        },
        prompt={"version": prompt_uri or "file", "digest": digest(templates)},
        parameters={"case_root": str(case_root), "timeout_seconds": args.timeout},
    )
    adapter = ExtractorAdapter(stage, model, require_reviewed=True)
    redactor = Redactor()
    metadata = {
        "dataset_version": dataset["version"],
        "dataset_digest": digest(dataset),
        "prompt_digest": stage.prompt.digest,
        "prompt_version": stage.prompt.version,
        "evaluator_version": stage.evaluator.version,
        "evaluator_digest": stage.evaluator.digest,
        "source_revision": args.source_revision,
        "model": model.model_dump(mode="json"),
        "repetitions": args.repetitions,
    }
    parameters = metadata["model"]["inference_parameters"]
    if "max_tokens" in parameters:
        limit = parameters.pop("max_tokens")
        if type(limit) is not int or limit < 1:
            raise ValueError("max_tokens must be a positive integer")
        metadata["model"]["max_output_budget"] = limit
    if redactor.clean(metadata) != metadata:
        raise ValueError(
            "Configuration contains credentials; use environment variables"
        )
    store = LocalStore(args.output, uuid4().hex, redactor)
    print(f"Local evidence: {store.path}", flush=True)
    store.put("configuration.json", metadata)
    store.put("dataset.json", dataset)
    store.put("prompt.json", templates)
    records = []
    feedback = []
    usage_totals = Counter()
    usage_coverage = Counter()
    attempt_count = 0
    with patch.object(module, "REC_EXTRACTOR_SYSTEM", system):
        for repetition in range(1, args.repetitions + 1):
            for ref in refs:
                capture = Capture(store, STAGE, ref.case_id, repetition)
                record = adapter.execute(ref, repetition, capture)
                store.put(f"{capture.prefix}/record.json", record)
                if not adapter.validate(record):
                    raise ValueError("Invalid extraction accounting")
                records.append(record)
                usage = Counter()
                coverage = Counter()
                for attempt in range(1, capture.attempts + 1):
                    artifact = (
                        store.path
                        / capture.prefix
                        / f"attempt-{attempt}"
                        / "attempt.json"
                    )
                    quantities = json.loads(artifact.read_bytes())["usage"]
                    usage.update(quantities)
                    coverage.update(quantities.keys())
                attempt_count += capture.attempts
                usage_totals.update(usage)
                usage_coverage.update(coverage)
                feedback.append(
                    {
                        "case": len(records),
                        "usable": record.usable,
                        **record.metrics,
                        "usage": dict(usage),
                        "usage_coverage": dict(coverage),
                        "attempts": capture.attempts,
                    }
                )
    summary = adapter.summarize(records)
    store.put(
        "results.json",
        {
            "metrics": summary.metrics,
            "findings": summary.findings,
            "cases": feedback,
            "usage": {
                "totals": dict(usage_totals),
                "coverage": dict(usage_coverage),
                "attempts": attempt_count,
            },
        },
    )
    store.put("checksums.json", {ref.name: ref.sha256 for ref in store.refs})
    if args.publish:
        publish(store.path, args.experiment)
    return 0 if summary.metrics["usable_cases"] else 1


@traced
def publish(path, experiment):
    from mlflow.genai.scorers import scorer

    inventory = json.loads((path / "checksums.json").read_bytes())
    root = path.resolve()
    for name, checksum in inventory.items():
        artifact = (root / name).resolve()
        if (
            not artifact.is_relative_to(root)
            or hashlib.sha256(artifact.read_bytes()).hexdigest() != checksum
        ):
            raise ValueError("Local evidence checksum mismatch")
    metadata = json.loads((path / "configuration.json").read_bytes())
    results = json.loads((path / "results.json").read_bytes())

    @scorer
    def extraction_usable(outputs):
        return outputs["usable"]

    @scorer
    def recommendation_recall(outputs):
        return outputs.get("recommendation_recall")

    mlflow.set_experiment(experiment)
    tags = {key: value for key, value in metadata.items() if isinstance(value, str)}
    tags["local_run_id"] = path.name
    tags["model"] = metadata["model"]["model"]
    tags["provider"] = metadata["model"]["provider"]
    with mlflow.start_run(tags=tags) as flow_run:
        print(f"MLflow run: {flow_run.info.run_id}", flush=True)
        for name in (*inventory, "checksums.json"):
            parent = Path(name).parent
            mlflow.log_artifact(
                str(path / name), None if parent == Path(".") else str(parent)
            )
        mlflow.log_metrics(results["metrics"])
        if usage := results.get("usage"):
            mlflow.log_metrics(
                {
                    "usage.attempts": usage["attempts"],
                    **{f"usage.{k}": v for k, v in usage["totals"].items()},
                    **{
                        f"usage.reported_attempts.{k}": v
                        for k, v in usage["coverage"].items()
                    },
                }
            )
        mlflow.genai.evaluate(
            data=[
                {
                    "inputs": {"case": row["case"]},
                    "outputs": {
                        key: row[key]
                        for key in ("usable", "recommendation_recall")
                        if key in row
                    },
                }
                for row in results["cases"]
            ],
            scorers=[extraction_usable, recommendation_recall],
        )


def promotion_recommendation(args):
    """Apply the policy to reviewer findings and a matched cost comparison."""
    if args.critical_regression:
        return "block"
    if args.evidence == "inconclusive":
        return "inconclusive"
    if args.evidence == "no-gain":
        return "keep-baseline"
    if (
        args.noncritical_regression
        or args.cost_delta is None
        or args.cost_allowance is None
    ):
        return "human-review"
    if args.cost_delta > args.cost_allowance:
        return "human-review"
    return "recommend-promotion"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--dataset", type=Path, required=True)
    execute.add_argument("--prompt", required=True)
    execute.add_argument("--model-config", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--source-revision", required=True)
    execute.add_argument("--repetitions", type=int, default=1)
    execute.add_argument("--timeout", type=float, default=300)
    execute.add_argument("--publish", action="store_true")
    execute.add_argument("--experiment", default=STAGE)
    replay = commands.add_parser("replay-case")
    replay.add_argument("--manifest", type=Path, required=True)
    replay.add_argument("--case-id", required=True)
    replay.add_argument("--record", type=Path, required=True)
    upload = commands.add_parser("publish")
    upload.add_argument("path", type=Path)
    upload.add_argument("--experiment", default=STAGE)
    decision = commands.add_parser("recommend")
    decision.add_argument(
        "--evidence", choices=("gain", "no-gain", "inconclusive"), required=True
    )
    decision.add_argument("--critical-regression", action="store_true")
    decision.add_argument("--noncritical-regression", action="store_true")
    decision.add_argument("--cost-delta", type=float)
    decision.add_argument("--cost-allowance", type=float)
    args = parser.parse_args(argv)
    if args.command == "recommend":
        for value in (args.cost_delta, args.cost_allowance):
            if value is not None and not math.isfinite(value):
                parser.error("Cost values must be finite")
        if args.cost_allowance is not None and args.cost_allowance < 0:
            parser.error("Cost allowance must be nonnegative")
        print(promotion_recommendation(args))
        return 0
    if args.command == "replay-case":
        manifest = json.loads(args.manifest.read_bytes())
        record = json.loads(args.record.read_bytes())
        if record["case_id"] != args.case_id:
            parser.error("Record case ID differs from requested case")
        refs = [item for item in manifest["cases"] if item["case_id"] == args.case_id]
        if len(refs) != 1:
            parser.error("Expected exactly one manifest entry for the case")
        result = replay_case(
            CaseRef.model_validate(refs[0]),
            args.manifest.parent / manifest["case_root"],
            record["artifacts"]["raw-rows.json"],
        )
        print(result.model_dump_json())
        return 0
    if args.command == "publish":
        publish(args.path, args.experiment)
        return 0
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.source_revision):
        parser.error("--source-revision must be a full Git revision")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
