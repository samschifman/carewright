import pytest
from test_runner import Adapter, read, setup


def envelope(tmp_path, config):
    from prompt_eval.runner import run

    r, s = setup(tmp_path, config)
    return read(s, run(config, {"extractor": Adapter()}, s, r))


def test_comparison_preserves_stage_metrics_and_repetition_ranges(tmp_path, config):
    from prompt_eval.comparison import compare

    env = envelope(tmp_path, config)
    result = compare([env, env.model_copy(update={"run_id": "second"})])
    assert result["stages"]["extractor"][0]["metrics"]["quality"] == {
        "mean": 1.5,
        "min": 1.0,
        "max": 2.0,
        "n": 2,
    }


@pytest.mark.parametrize(
    "change", ["repetitions", "dataset", "model", "production", "cases", "stage", "partial"]
)
def test_comparison_rejects_incompatible_or_incomplete_runs(tmp_path, config, change):
    from prompt_eval.comparison import ComparisonError, compare
    from prompt_eval.security import digest

    env = envelope(tmp_path, config)
    cfg = config.model_dump()
    stages = env.stages
    if change == "repetitions":
        cfg["repetitions"] = 3
    if change == "dataset":
        cfg["dataset"]["digest"] = "c" * 64
    if change == "production":
        cfg["production_behavior"]["digest"] = "c" * 64
    if change == "model":
        cfg["model"]["model"] = "different"
    if change == "cases":
        cfg["cases"][0]["source_digests"]["source"] = "c" * 64
    if change == "stage":
        cfg["stages"][0]["evaluator"]["digest"] = "c" * 64
    if change == "partial":
        stages = (env.stages[0].model_copy(update={"repetitions": env.stages[0].repetitions[:1]}),)
    from prompt_eval.models import RunConfig

    cfg = RunConfig.model_validate(cfg)
    other = env.model_copy(
        update={"config": cfg, "config_digest": digest(cfg), "stages": stages, "run_id": "other"}
    )
    with pytest.raises(ComparisonError):
        compare([env, other])


def test_integrity_retrieval_detects_modified_child_artifact(tmp_path, config):
    from pathlib import Path

    from prompt_eval.retrieval import retrieve
    from prompt_eval.runner import run
    from prompt_eval.storage import PersistenceError

    r, s = setup(tmp_path, config)
    receipt = run(config, {"extractor": Adapter()}, s, r)
    assert retrieve(receipt).status == "complete"
    Path(s.path / "config.json").write_text("{}")
    with pytest.raises(PersistenceError):
        retrieve(receipt)


def test_release_rejects_development_runs(tmp_path, config):
    from prompt_eval.comparison import ComparisonError, make_release

    env = envelope(tmp_path, config)
    with pytest.raises(ComparisonError):
        make_release(env, env)
