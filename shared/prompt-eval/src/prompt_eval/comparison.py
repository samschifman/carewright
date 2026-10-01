"""Mechanical comparisons only; stage-defined scalar meanings remain opaque."""

from statistics import mean

from .models import Release, RunEnvelope
from .registry import manifest_digest
from .security import digest, traced


class ComparisonError(ValueError):
    pass


@traced
def validate_envelope(env):
    env = RunEnvelope.model_validate(env.model_dump())
    cfg = env.config
    if env.config_digest != digest(cfg):
        raise ComparisonError("Configuration digest mismatch")
    if env.status != "complete" or env.operational_errors:
        raise ComparisonError("Cannot compare failed runs")
    if len(env.stages) != len(cfg.stages) or {s.stage for s in env.stages} != {
        s.name for s in cfg.stages
    }:
        raise ComparisonError("Incomplete stage accounting")
    expected = {c.case_id for c in cfg.cases if c.split == cfg.split}
    for stage in env.stages:
        if stage.configuration != next(s for s in cfg.stages if s.name == stage.stage):
            raise ComparisonError("Stage metadata differs from run configuration")
        if [r.repetition for r in stage.repetitions] != list(range(1, cfg.repetitions + 1)):
            raise ComparisonError("Incomplete repetitions")
        records = [c for rep in stage.repetitions for c in rep.cases]
        for rep in stage.repetitions:
            if len(rep.cases) != len(expected) or {c.case_id for c in rep.cases} != expected:
                raise ComparisonError("Incomplete or duplicate case accounting")
            if not any(c.usable for c in rep.cases):
                raise ComparisonError("Repetition has no usable cases")
        for key, value in {
            "declared_cases": len(expected) * cfg.repetitions,
            "usable_cases": sum(c.usable for c in records),
            "unusable_cases": sum(not c.usable for c in records),
        }.items():
            if stage.denominator.get(key) != value:
                raise ComparisonError("Denominator metadata does not match case records")
    return env


def comparison_key(config, varying):
    value = config.model_dump(mode="json")
    # Source commits legitimately change when prompt text changes. The separately
    # frozen production-behavior digest must remain identical.
    for name in ("source_revision", "variant"):
        value.pop(name)
    if varying == "model":
        value.pop("model")
    else:
        for stage in value["stages"]:
            stage.pop("prompt")
    return value


@traced
def compare(envelopes, varying="prompt"):
    if varying not in ("prompt", "model") or len(envelopes) < 2:
        raise ComparisonError("Compare at least two runs varying prompt or model")
    envelopes = [validate_envelope(e) for e in envelopes]
    if len({e.run_id for e in envelopes}) != len(envelopes):
        raise ComparisonError("Cannot compare a run to itself")
    official = [e.config.variant != "development" for e in envelopes]
    if len(set(official)) != 1:
        raise ComparisonError("Development results cannot be promoted by comparison")
    first = comparison_key(envelopes[0].config, varying)
    if any(comparison_key(e.config, varying) != first for e in envelopes[1:]):
        raise ComparisonError("Unequal repetitions or incompatible frozen revisions/configuration")
    stages = {}
    for env in envelopes:
        for stage in env.stages:
            keys = set(stage.repetitions[0].summary.metrics)
            if any(set(rep.summary.metrics) != keys for rep in stage.repetitions):
                raise ComparisonError("Stage metric keys differ between repetitions")
            metrics = {}
            for key in sorted(keys):
                values = [r.summary.metrics[key] for r in stage.repetitions]
                metrics[key] = {
                    "mean": mean(values),
                    "min": min(values),
                    "max": max(values),
                    "n": len(values),
                }
            previous = stages.setdefault(stage.stage, [])
            if previous and set(previous[0]["metrics"]) != keys:
                raise ComparisonError("Stage metric keys differ between runs")
            previous.append(
                {
                    "run_id": env.run_id,
                    "variant": env.config.variant,
                    "metrics": metrics,
                    "aggregate": stage.aggregate.model_dump(mode="json"),
                    "denominator": stage.denominator,
                    "operational_errors": [
                        e.model_dump(mode="json") for e in stage.operational_errors
                    ],
                }
            )
    return {"schema_version": "1.0.0", "varying": varying, "stages": stages}


@traced
def make_release(baseline, selected):
    if baseline.config.variant != "baseline" or selected.config.variant != "tuned":
        raise ComparisonError("Release requires official baseline and tuned runs")
    if not baseline.mlflow_run_id or not selected.mlflow_run_id:
        raise ComparisonError("Release requires persisted MLflow runs")
    compare([baseline, selected], varying="prompt")
    cfg = selected.config
    return Release(
        experiment_id=cfg.experiment_id,
        dataset=cfg.dataset,
        manifest_digest=manifest_digest(cfg),
        production_behavior=cfg.production_behavior,
        stages=cfg.stages,
        baseline_prompts={s.name: s.prompt for s in baseline.config.stages},
        model=cfg.model,
        repetitions=cfg.repetitions,
        baseline_run_id=baseline.run_id,
        selected_run_id=selected.run_id,
    )
