import json

import pytest
from test_runner import Adapter, read, setup


def make_record(config):
    from prompt_eval.models import Release
    from prompt_eval.registry import manifest_digest

    return Release(
        experiment_id=config.experiment_id,
        dataset=config.dataset,
        manifest_digest=manifest_digest(config),
        production_behavior=config.production_behavior,
        stages=config.stages,
        baseline_prompts={s.name: s.prompt for s in config.stages},
        model=config.model,
        repetitions=config.repetitions,
        baseline_run_id="baseline",
        selected_run_id="selected",
    )


def test_release_cannot_freeze_an_active_tuning_run(tmp_path, config):
    from prompt_eval.registry import Registry, RegistryError

    registry = Registry(tmp_path / "registry.db")
    registry.reserve("active", config)
    with pytest.raises(RegistryError, match="active"):
        registry.release(make_record(config), config)


def test_reveal_requires_reserved_run_owner(tmp_path, config):
    from prompt_eval.registry import Registry, RegistryError

    registry = Registry(tmp_path / "registry.db")
    registry.reserve("base", config)
    registry.finish("base")
    release = make_record(config)
    registry.release(release, config)
    cfg = config.model_copy(update={"variant": "holdout", "split": "holdout"})
    with pytest.raises(RegistryError, match="owner"):
        registry.reveal(cfg, release, "unreserved")


def test_renaming_revealed_holdout_ids_and_dataset_does_not_reopen_it(tmp_path, config):
    from prompt_eval.models import RunConfig
    from prompt_eval.registry import Registry, RegistryError

    registry = Registry(tmp_path / "registry.db")
    registry.reserve("base", config)
    registry.finish("base")
    release = make_record(config)
    registry.release(release, config)
    cfg = config.model_copy(update={"variant": "holdout", "split": "holdout"})
    registry.reserve("holdout", cfg)
    registry.reveal(cfg, release, "holdout")
    registry.finish("holdout")
    new = config.model_dump()
    new["dataset"] = {"version": "v2", "digest": "c" * 64}
    new["cases"][1]["case_id"] = "renamed"
    with pytest.raises(RegistryError):
        registry.reserve("new", RunConfig.model_validate(new))


def test_failed_contract_retains_reported_output_counts(tmp_path, config):
    from prompt_eval.models import CaseResult
    from prompt_eval.runner import RunFailed, run

    class Reject(Adapter):
        def execute(self, case, repetition, capture):
            return CaseResult(
                case_id=case.case_id,
                produced_count=5,
                accounted_count=5,
                expected_count=3,
                metrics={"quality": 1},
            )

        def validate(self, record):
            return False

    registry, store = setup(tmp_path, config)
    with pytest.raises(RunFailed) as failed:
        run(config, {"extractor": Reject()}, store, registry)
    env = read(store, failed.value.receipt)
    assert env.stages[0].denominator["produced_outputs"] == 10
    assert env.stages[0].denominator["expected_outputs"] == 6


def test_case_latency_recorded_without_capture_hook(tmp_path, config):
    from prompt_eval.models import CaseResult
    from prompt_eval.runner import run

    class NoCapture(Adapter):
        def execute(self, case, repetition, capture):
            return CaseResult(case_id=case.case_id, metrics={"quality": 1})

    registry, store = setup(tmp_path, config)
    env = read(store, run(config, {"extractor": NoCapture()}, store, registry))
    latencies = [ref for ref in env.artifacts if ref.name.endswith("/execution.json")]
    assert len(latencies) == config.repetitions
    assert all(json.loads(store.read(ref))["latency_ms"] >= 0 for ref in latencies)


def test_plaintext_json_credentials_and_header_lines_are_redacted():
    from prompt_eval.security import Redactor

    redactor = Redactor()
    cleaned = redactor.clean(
        {
            "response": '{"api_key": "unlisted-credential"}',
            "exception": "X-API-Key: other-credential\nAuthorization: Bearer abc",
        }
    )
    text = json.dumps(cleaned)
    assert "unlisted-credential" not in text
    assert "other-credential" not in text
    assert "Bearer abc" not in text


def test_failure_to_finalize_mlflow_is_not_a_complete_receipt(tmp_path, config):
    from prompt_eval.runner import RunFailed, run

    registry, store = setup(
        tmp_path, config.model_copy(update={"variant": "baseline"}), official=True
    )

    def fail_finish(*args):
        from prompt_eval.storage import PersistenceError

        raise PersistenceError("unavailable")

    store.finish = fail_finish
    with pytest.raises(RunFailed) as failed:
        run(
            config.model_copy(update={"variant": "baseline"}),
            {"extractor": Adapter()},
            store,
            registry,
        )
    assert failed.value.receipt is None


def test_complete_envelope_cannot_hide_top_level_errors(tmp_path, config):
    from prompt_eval.comparison import ComparisonError, validate_envelope
    from prompt_eval.models import OperationalError
    from prompt_eval.runner import run

    registry, store = setup(tmp_path, config)
    env = read(store, run(config, {"extractor": Adapter()}, store, registry))
    env = env.model_copy(
        update={"operational_errors": (OperationalError(kind="internal", message="bad"),)}
    )
    with pytest.raises(ComparisonError):
        validate_envelope(env)


def test_successful_official_release_and_holdout_are_restart_safe(tmp_path, config):
    from prompt_eval.comparison import make_release
    from prompt_eval.registry import Registry, RegistryError
    from prompt_eval.runner import RunFailed, run
    from prompt_eval.security import Redactor
    from prompt_eval.storage import MlflowStore
    from test_storage import FakeClient

    registry = Registry(tmp_path / "registry.db")

    def execute(cfg, run_id, release=None):
        store = MlflowStore(
            "https://mlflow.cluster.example",
            "test",
            run_id,
            Redactor(),
            official=True,
            approved_uri="https://mlflow.cluster.example",
            client=FakeClient(),
        )
        return read(store, run(cfg, {"extractor": Adapter()}, store, registry, release=release))

    baseline = execute(config.model_copy(update={"variant": "baseline"}), "baseline")
    tuned = execute(config.model_copy(update={"variant": "tuned"}), "tuned")
    release = make_release(baseline, tuned)
    registry.release(release, tuned.config)
    held = config.model_copy(update={"variant": "holdout", "split": "holdout"})
    env = execute(held, "holdout", release)
    assert env.holdout_revealed_at
    assert env.holdout_release_digest
    assert all(c.case_id == "sealed" for rep in env.stages[0].repetitions for c in rep.cases)
    with pytest.raises(RunFailed):
        execute(held, "second-holdout", release)
    with pytest.raises(RegistryError):
        Registry(tmp_path / "registry.db").reserve("retune", config)


def test_changed_frozen_release_field_is_rejected(tmp_path, config):
    from prompt_eval.registry import Registry, RegistryError

    registry = Registry(tmp_path / "registry.db")
    registry.reserve("base", config)
    registry.finish("base")
    release = make_record(config)
    registry.release(release, config)
    held = config.model_copy(update={"variant": "holdout", "split": "holdout", "repetitions": 3})
    registry.reserve("held", held)
    with pytest.raises(RegistryError, match="frozen"):
        registry.reveal(held, release, "held")


def test_concurrent_campaign_reservations_have_one_winner(tmp_path, config):
    from concurrent.futures import ThreadPoolExecutor

    from prompt_eval.registry import Registry, RegistryError

    registry = Registry(tmp_path / "registry.db")

    def reserve(i):
        try:
            registry.reserve(f"run-{i}", config)
            return True
        except RegistryError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, range(2))) == 1


def test_changed_split_manifest_cannot_reuse_dataset_digest(tmp_path, config):
    from prompt_eval.models import RunConfig
    from prompt_eval.registry import Registry, RegistryError

    registry = Registry(tmp_path / "registry.db")
    registry.reserve("base", config)
    registry.finish("base")
    data = config.model_dump()
    data["cases"][1]["split"] = "tuning"
    with pytest.raises(RegistryError):
        registry.reserve("changed", RunConfig.model_validate(data))


def test_export_is_verified_and_retrievable_offline(tmp_path, config):
    from prompt_eval.retrieval import export_run, retrieve
    from prompt_eval.runner import run

    registry, store = setup(tmp_path, config)
    receipt = run(config, {"extractor": Adapter()}, store, registry)
    destination = tmp_path / "export"
    exported_receipt = export_run(receipt, destination)
    assert retrieve(exported_receipt, export_dir=destination).run_id == receipt.run_id
    assert (destination / "receipt.json").exists()
    with pytest.raises(FileExistsError):
        export_run(receipt, destination)


@pytest.mark.parametrize(
    "raw",
    [
        "{'token': 'private-value'}",
        "{'headers': {'Cookie': 'session=private-cookie'}}",
        "{'password': 'two words hidden'}",
    ],
)
def test_credential_bearing_raw_strings_fail_closed(raw):
    from prompt_eval.security import Redactor

    assert Redactor().text(raw) == "[REDACTED credential-bearing text]"


def test_rejected_raw_counts_are_preserved_and_unaccounted_outputs_visible(tmp_path, config):
    from prompt_eval.runner import RunFailed, run

    class InvalidAccounting(Adapter):
        def execute(self, case, repetition, capture):
            return {
                "case_id": case.case_id,
                "expected_count": 5,
                "produced_count": 4,
                "accounted_count": 3,
            }

    registry, store = setup(tmp_path, config)
    with pytest.raises(RunFailed) as failed:
        run(config, {"extractor": InvalidAccounting()}, store, registry)
    env = read(store, failed.value.receipt)
    counts = env.stages[0].denominator
    assert counts["expected_outputs"] == 10
    assert counts["produced_outputs"] == 8
    assert counts["accounted_outputs"] == 6
    assert counts["unaccounted_outputs"] == 2


@pytest.mark.parametrize("change", ["add", "rename-corpus", "rename-digest-key"])
def test_any_revealed_holdout_source_overlap_is_rejected(tmp_path, config, change):
    from prompt_eval.models import RunConfig
    from prompt_eval.registry import Registry, RegistryError

    registry = Registry(tmp_path / "registry.db")
    registry.reserve("base", config)
    registry.finish("base")
    release = make_record(config)
    registry.release(release, config)
    held = config.model_copy(update={"variant": "holdout", "split": "holdout"})
    registry.reserve("held", held)
    registry.reveal(held, release, "held")
    registry.finish("held")
    data = config.model_dump()
    data["dataset"] = {"version": "new", "digest": "d" * 64}
    if change == "add":
        data["cases"] = [
            *data["cases"],
            {
                "case_id": "fresh",
                "corpus": "other",
                "split": "holdout",
                "source_digests": {"source": "e" * 64},
            },
        ]
    elif change == "rename-corpus":
        data["cases"][1]["corpus"] = "renamed"
    else:
        data["cases"][1]["source_digests"] = {"renamed-key": "b" * 64}
    with pytest.raises(RegistryError):
        registry.reserve("new", RunConfig.model_validate(data))
