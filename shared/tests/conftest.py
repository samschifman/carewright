"""Shared-contract test configuration."""
import pytest


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
