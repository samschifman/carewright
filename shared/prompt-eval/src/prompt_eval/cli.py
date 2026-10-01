"""Thin JSON CLI. Credentials are read by MLflow from its standard environment."""

import argparse
import importlib
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .comparison import compare, make_release
from .example import example_config
from .models import OperationalError, Receipt, Release, RunConfig, RunEnvelope
from .registry import Registry
from .retrieval import export_run, retrieve
from .runner import RunFailed, run
from .security import Redactor, canonical_bytes, traced
from .storage import LocalStore, MlflowStore, PersistenceError


@traced
def write_exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(canonical_bytes(value) + b"\n")
    path.chmod(0o600)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("example-config")
    schema = sub.add_parser("schema")
    schema.add_argument(
        "--model", choices=["envelope", "config", "release", "receipt"], default="envelope"
    )
    run_p = sub.add_parser("run")
    run_p.add_argument("--config", required=True, type=Path)
    run_p.add_argument(
        "--adapter", required=True, help="Trusted module:factory accepting tuple[StageConfig]"
    )
    run_p.add_argument("--registry", required=True, type=Path)
    run_p.add_argument("--output", type=Path, default=Path("working/prompt-eval"))
    run_p.add_argument("--receipt", required=True, type=Path)
    run_p.add_argument("--store", choices=["local", "mlflow"], default="local")
    run_p.add_argument("--experiment", default="carewright-prompt-eval")
    run_p.add_argument("--release", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--receipt", required=True, type=Path)
    verify.add_argument("--export-dir", type=Path, help="Verify an offline export")
    export = sub.add_parser("export")
    export.add_argument("--receipt", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    cmp = sub.add_parser("compare")
    cmp.add_argument("--receipt", required=True, action="append", type=Path)
    cmp.add_argument("--varying", choices=["prompt", "model"], default="prompt")
    rel = sub.add_parser("release")
    rel.add_argument("--baseline", required=True, type=Path)
    rel.add_argument("--selected", required=True, type=Path)
    rel.add_argument("--registry", required=True, type=Path)
    rel.add_argument("--output", required=True, type=Path)
    return p


@traced
def dispatch(args):
    tracking = os.environ.get("MLFLOW_TRACKING_URI")
    redactor = Redactor()
    if args.command == "example-config":
        return example_config()
    if args.command == "schema":
        return {
            "envelope": RunEnvelope,
            "config": RunConfig,
            "release": Release,
            "receipt": Receipt,
        }[args.model].model_json_schema()
    if args.command == "export":
        receipt = Receipt.model_validate_json(args.receipt.read_bytes())
        export_run(receipt, args.output, tracking_uri=tracking)
        return {"run_id": receipt.run_id, "export_dir": str(args.output)}
    if args.command in ("verify", "compare", "release"):

        def load(path):
            return retrieve(
                Receipt.model_validate_json(path.read_bytes()),
                tracking_uri=tracking,
                export_dir=getattr(args, "export_dir", None),
            )

        if args.command == "verify":
            env = load(args.receipt)
            return {
                "run_id": env.run_id,
                "status": env.status,
                "verified_artifacts": len(env.artifacts) + 1,
            }
        if args.command == "compare":
            return compare([load(p) for p in args.receipt], args.varying)
        if args.output.exists():
            raise ValueError("Release output already exists")
        baseline, selected = load(args.baseline), load(args.selected)
        release = make_release(baseline, selected)
        Registry(args.registry).release(release, selected.config)
        write_exclusive(args.output, release)
        return release
    cfg = RunConfig.model_validate_json(args.config.read_bytes())
    if args.receipt.exists():
        raise ValueError("Receipt already exists; never overwrite a run identity")
    release = Release.model_validate_json(args.release.read_bytes()) if args.release else None
    run_id = str(uuid4())
    registry = Registry(args.registry)
    if args.store == "mlflow":
        if not tracking:
            raise ValueError("Set MLFLOW_TRACKING_URI explicitly")
        store = MlflowStore(
            tracking,
            args.experiment,
            run_id,
            redactor,
            official=cfg.variant != "development",
            approved_uri=os.environ.get("PROMPT_EVAL_RHOAI_TRACKING_URI"),
        )
    else:
        store = LocalStore(args.output, run_id, redactor)
    # Import only after configuration validation. Factories must not invoke models;
    # all execution belongs in execute() after run() completes preflight.
    module, sep, factory = args.adapter.partition(":")
    if not sep:
        raise ValueError("Adapter must be module:factory")
    adapters = getattr(importlib.import_module(module), factory)(cfg.stages)
    try:
        receipt = run(cfg, adapters, store, registry, release=release)
    except RunFailed as exc:
        if exc.receipt:
            write_exclusive(args.receipt, exc.receipt)
        raise
    write_exclusive(args.receipt, receipt)
    return receipt


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            value = dispatch(args)
        print(canonical_bytes(value).decode())
        return 0
    except RunFailed as exc:
        value = {
            "error": exc.error.model_dump(mode="json"),
            "receipt": exc.receipt.model_dump(mode="json") if exc.receipt else None,
        }
    except ValidationError:
        # Pydantic error strings include input_value; never echo an invalid config.
        value = {
            "error": OperationalError(
                kind="configuration", message="Invalid input schema"
            ).model_dump()
        }
    except Exception as exc:  # noqa: BLE001 -- CLI must serialize errors without raw traceback
        kind = "persistence" if isinstance(exc, (PersistenceError, OSError)) else "configuration"
        value = {
            "error": OperationalError(
                kind=kind, message=Redactor().text(str(exc)), exception_type=type(exc).__name__
            ).model_dump()
        }
    print(canonical_bytes(Redactor().clean(value)).decode())
    return 1
