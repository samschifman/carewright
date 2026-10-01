import json

import pytest
from test_storage import FakeClient


class Adapter:
    def execute(self, case, repetition, capture):
        from prompt_eval.models import CaseResult

        try:
            with capture.attempt(
                {"messages": ["hello"], "headers": {"Authorization": "secret"}}
            ) as a:
                a.validation_error("invalid first response")
                a.response("not json")
                raise ValueError("parse failed")
        except ValueError:
            pass
        with capture.attempt({"retry": True}) as a:
            a.response({"answer": "ok"})
            a.usage(input_tokens=3, output_tokens=2)
        return CaseResult(
            case_id=case.case_id,
            metrics={"quality": repetition},
            produced_count=1,
            accounted_count=1,
        )

    def validate(self, record):
        return True

    def summarize(self, records):
        from prompt_eval.models import StageSummary

        usable = [r for r in records if r.usable]
        return StageSummary(
            metrics={
                "quality": sum(r.metrics["quality"] for r in usable) / len(usable) if usable else 0
            }
        )


def setup(tmp_path, config, official=False):
    from prompt_eval.registry import Registry
    from prompt_eval.security import Redactor
    from prompt_eval.storage import LocalStore, MlflowStore

    r = Registry(tmp_path / "registry.db")
    if official:
        s = MlflowStore(
            "https://mlflow.cluster.example",
            "test",
            "run-one",
            Redactor(),
            official=True,
            approved_uri="https://mlflow.cluster.example",
            client=FakeClient(),
        )
    else:
        s = LocalStore(tmp_path / "artifacts", "run-one", Redactor())
    return r, s


def read(s, receipt):
    from prompt_eval.models import RunEnvelope

    return RunEnvelope.model_validate_json(s.read(receipt.envelope))


def test_two_stages_repetitions_and_attempt_capture(tmp_path, config):
    from prompt_eval.models import RunConfig
    from prompt_eval.runner import run

    data = config.model_dump()
    data["stages"] = [*data["stages"], data["stages"][0] | {"name": "reviewer"}]
    config = RunConfig.model_validate(data)
    r, s = setup(tmp_path, config)
    receipt = run(config, {"extractor": Adapter(), "reviewer": Adapter()}, s, r)
    env = read(s, receipt)
    assert env.status == "complete"
    assert [e.stage for e in env.stages] == ["extractor", "reviewer"]
    for stage in env.stages:
        assert stage.aggregate.metrics == {"quality": 1.5}
        assert len(stage.repetitions) == 2
        assert stage.denominator["declared_cases"] == stage.denominator["usable_cases"] == 2
        assert stage.denominator["attempts"] == 4
        assert stage.denominator["failed_attempts"] == 2
        assert all(c.case_id != "sealed" for rep in stage.repetitions for c in rep.cases)
    attempts = [json.loads(s.read(a)) for a in env.artifacts if a.name.endswith("/attempt.json")]
    assert len(attempts) == 8
    assert attempts[0]["validation_errors"] == ["invalid first response"]
    assert attempts[1]["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert attempts[1]["latency_ms"] >= 0
    assert "secret" not in json.dumps(attempts)


def test_official_preflight_failure_prevents_all_executor_calls(tmp_path, config):
    from prompt_eval.runner import RunFailed, run

    cfg = config.model_copy(update={"variant": "baseline"})
    r, s = setup(tmp_path, cfg, official=True)
    s.client.corrupt = True

    class Never(Adapter):
        def execute(self, *args):
            pytest.fail("Paid call occurred before persistence preflight")

    with pytest.raises(RunFailed) as err:
        run(cfg, {"extractor": Never()}, s, r)
    assert err.value.error.kind == "persistence"


def test_official_local_run_is_rejected(tmp_path, config):
    from prompt_eval.runner import RunFailed, run

    r, s = setup(tmp_path, config)
    with pytest.raises(RunFailed) as err:
        run(config.model_copy(update={"variant": "baseline"}), {"extractor": Adapter()}, s, r)
    assert err.value.error.kind == "configuration"


@pytest.mark.parametrize(
    "behavior,kind",
    [
        ("timeout", "timeout"),
        ("wrong", "parse/contract"),
        ("none", "empty-result"),
        ("crash", "invocation"),
    ],
)
def test_unusable_cases_fail_and_remain_accounted(tmp_path, config, behavior, kind):
    from prompt_eval.models import CaseResult
    from prompt_eval.runner import RunFailed, run

    class Broken(Adapter):
        def execute(self, case, repetition, capture):
            if behavior == "timeout":
                raise TimeoutError("late")
            if behavior == "crash":
                raise RuntimeError("crashed")
            if behavior == "wrong":
                return CaseResult(case_id="unknown")
            return None

    r, s = setup(tmp_path, config)
    with pytest.raises(RunFailed) as err:
        run(config, {"extractor": Broken()}, s, r)
    env = read(s, err.value.receipt)
    assert env.status == "failed"
    stage = env.stages[0]
    assert stage.denominator["unusable_cases"] == 2
    assert stage.denominator["usable_cases"] == 0
    assert all(x.kind == kind for x in stage.operational_errors)
    assert len(stage.repetitions) == 2


def test_holdout_missing_release_prevents_execution(tmp_path, config):
    from prompt_eval.runner import RunFailed, run

    r, s = setup(tmp_path, config, official=True)
    cfg = config.model_copy(update={"variant": "holdout", "split": "holdout"})
    with pytest.raises(RunFailed) as err:
        run(cfg, {"extractor": Adapter()}, s, r)
    assert err.value.error.kind == "configuration"


def test_model_copy_cannot_bypass_validation(tmp_path, config):
    from prompt_eval.runner import RunFailed, run

    r, s = setup(tmp_path, config)
    with pytest.raises(RunFailed):
        run(config.model_copy(update={"repetitions": 0}), {"extractor": Adapter()}, s, r)
