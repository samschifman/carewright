import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_local_artifacts_are_redacted_exclusive_and_verified(tmp_path):
    from prompt_eval.security import Redactor
    from prompt_eval.storage import LocalStore, PersistenceError

    s = LocalStore(tmp_path, "run-one", Redactor(["private"]))
    ref = s.put("raw/attempt.json", {"response": "private", "headers": {"X": "secret"}})
    assert b"private" not in s.read(ref)
    with pytest.raises(PersistenceError):
        s.put("raw/attempt.json", {"different": True})
    with pytest.raises(PersistenceError):
        LocalStore(tmp_path, "run-one", Redactor())
    for name in ("../outside", "/absolute", "a/../../bad", "a\\bad"):
        with pytest.raises(PersistenceError):
            s.put(name, {})
    Path(ref.uri.removeprefix("file://")).write_bytes(b"tampered")
    with pytest.raises(PersistenceError, match="digest"):
        s.read(ref)


class FakeClient:
    def __init__(self):
        self.data = {}
        self.corrupt = False
        self.status = None

    def get_experiment_by_name(self, name):
        return SimpleNamespace(experiment_id="1")

    def create_run(self, experiment_id, tags):
        return SimpleNamespace(
            info=SimpleNamespace(
                run_id="remote-id", artifact_uri="mlflow-artifacts:/1/remote/artifacts"
            )
        )

    def list_artifacts(self, run_id, path):
        return [SimpleNamespace(path=p) for p in self.data if p.rsplit("/", 1)[0] == path]

    def log_artifact(self, run_id, local_path, artifact_path):
        self.data[artifact_path + "/" + Path(local_path).name] = Path(local_path).read_bytes()

    def download_artifacts(self, run_id, path, dst_path):
        p = Path(dst_path) / Path(path).name
        p.write_bytes(b"corrupt" if self.corrupt else self.data[path])
        return str(p)

    def set_terminated(self, run_id, status):
        self.status = status


def test_mlflow_roundtrip_and_no_overwrite(tmp_path):
    from prompt_eval.security import Redactor
    from prompt_eval.storage import MlflowStore, PersistenceError

    c = FakeClient()
    s = MlflowStore(
        "https://mlflow.cluster.example",
        "test",
        "run-one",
        Redactor(),
        official=True,
        approved_uri="https://mlflow.cluster.example",
        client=c,
    )
    preflight = s.preflight()
    assert preflight.sha256 == hashlib.sha256(s.read(preflight)).hexdigest()
    ref = s.put("results/a.json", {"x": 1})
    assert s.read(ref) == b'{"x":1}'
    with pytest.raises(PersistenceError):
        s.put("results/a.json", {})
    c.corrupt = True
    with pytest.raises(PersistenceError):
        s.put("results/b.json", {})
    s.finish(False)
    assert c.status == "FAILED"


@pytest.mark.parametrize(
    "uri,approved",
    [
        ("file:///tmp/mlruns", "file:///tmp/mlruns"),
        ("http://localhost:5000", "http://localhost:5000"),
        ("https://remote", None),
        ("https://remote", "https://different"),
        ("https://user:pass@remote", "https://user:pass@remote"),
    ],
)
def test_official_mlflow_requires_explicit_remote_operator_uri(uri, approved):
    from prompt_eval.security import Redactor
    from prompt_eval.storage import MlflowStore, PersistenceError

    with pytest.raises(PersistenceError):
        MlflowStore(
            uri,
            "test",
            "one",
            Redactor(),
            official=True,
            approved_uri=approved,
            client=FakeClient(),
        )


def test_registry_freezes_releases_and_reveals_transactionally(tmp_path, config):
    from prompt_eval.models import Release, RunConfig
    from prompt_eval.registry import Registry, RegistryError, manifest_digest

    r = Registry(tmp_path / "registry.db")
    r.reserve("run-a", config)
    with pytest.raises(RegistryError):
        r.reserve("run-a", config)
    release = Release(
        experiment_id=config.experiment_id,
        dataset=config.dataset,
        manifest_digest=manifest_digest(config),
        production_behavior=config.production_behavior,
        stages=config.stages,
        baseline_prompts={"extractor": config.stages[0].prompt},
        model=config.model,
        repetitions=2,
        baseline_run_id="base",
        selected_run_id="tuned",
    )
    r.finish("run-a")
    r.release(release, config)
    with pytest.raises(RegistryError):
        r.release(release, config)
    cfg = RunConfig.model_validate(config.model_dump() | {"variant": "holdout", "split": "holdout"})
    r.reserve("holdout-run", cfg)
    stamp = r.reveal(cfg, release, "holdout-run")
    assert stamp
    with pytest.raises(RegistryError):
        r.reveal(cfg, release, "holdout-other")
    # Restart and renaming the experiment cannot bypass an already revealed dataset.
    other = Registry(tmp_path / "registry.db")
    with pytest.raises(RegistryError):
        other.reserve("later", config.model_copy(update={"experiment_id": "alias"}))
    with pytest.raises(RegistryError):
        other.reserve("later", config)
