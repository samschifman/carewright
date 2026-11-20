import os

os.environ["MLFLOW_TRACE_SAMPLING_RATIO"] = "0"
os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
import pytest


def revision(name="v1"):
    import hashlib

    return {"version": name, "digest": hashlib.sha256(name.encode()).hexdigest()}


def config_dict():
    return {
        "experiment_id": "synthetic-test",
        "source_revision": "a" * 40,
        "production_behavior": revision("production"),
        "dataset": revision("dataset"),
        "model": {
            "provider": "synthetic",
            "endpoint": "https://example.invalid/v1",
            "model": "echo",
            "service_tier": "test",
            "inference_parameters": {"seed": 1},
        },
        "repetitions": 2,
        "variant": "development",
        "stages": [
            {
                "name": "extractor",
                "configuration": revision("configuration"),
                "parameters": {},
                "evaluator": revision("evaluator"),
                "prompt": revision("prompt"),
            }
        ],
        "cases": [
            {
                "case_id": "one",
                "corpus": "synthetic",
                "split": "tuning",
                "source_digests": {"source": "a" * 64},
            },
            {
                "case_id": "sealed",
                "corpus": "synthetic",
                "split": "holdout",
                "source_digests": {"source": "b" * 64},
            },
        ],
    }


@pytest.fixture
def config():
    from prompt_eval.models import RunConfig

    return RunConfig.model_validate(config_dict())


@pytest.fixture(scope="session", autouse=True)
def isolated_mlflow_backend(tmp_path_factory):
    """Keep routine test traces out of persistent working-directory databases.

    Explicit telemetry tests can temporarily select their own backend. All
    subprocesses inherit this URI, and queued spans flush before teardown.
    """
    patch = pytest.MonkeyPatch()
    uri = "sqlite:///" + str(tmp_path_factory.mktemp("mlflow") / "traces.db")
    patch.setenv("MLFLOW_TRACKING_URI", uri)
    patch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    patch.delenv("MLFLOW_EXPERIMENT_NAME", raising=False)
    try:
        import mlflow
    except ImportError:
        try:
            yield
        finally:
            patch.undo()
        return
    previous_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("unit-tests")
    try:
        yield
    finally:
        mlflow.flush_trace_async_logging()
        mlflow.set_tracking_uri(previous_uri)
        patch.undo()
