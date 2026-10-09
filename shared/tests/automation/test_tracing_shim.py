import builtins
import importlib

import pytest

import cpg_contracts.automation._tracing as tracing


def test_mlflow_decorator_marks_function_when_installed():
    pytest.importorskip("mlflow")
    importlib.reload(tracing)

    @tracing.trace(name="automation.test-trace")
    def work() -> str:
        return "done"

    assert work() == "done"
    assert getattr(work, "__mlflow_traced__", False)


def test_function_runs_when_mlflow_import_fails(monkeypatch):
    real_import = builtins.__import__

    def import_without_mlflow(name, *args, **kwargs):
        if name == "mlflow" or name.startswith("mlflow."):
            raise ImportError("MLflow intentionally unavailable in this test")
        return real_import(name, *args, **kwargs)

    try:
        monkeypatch.setattr(builtins, "__import__", import_without_mlflow)
        importlib.reload(tracing)

        @tracing.trace(name="automation.test-fallback")
        def work() -> str:
            return "done"

        assert work() == "done"
    finally:
        monkeypatch.undo()
        importlib.reload(tracing)
