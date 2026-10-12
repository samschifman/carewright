from unittest.mock import patch

from cpg_ingester.cli import _configure_mlflow_experiment


def test_configures_run_scoped_mlflow_experiment():
    with patch.dict("os.environ", {"MLFLOW_EXPERIMENT_NAME": "slice-test-run"}), \
            patch("cpg_ingester.cli.mlflow.set_experiment") as set_experiment:
        _configure_mlflow_experiment()

    set_experiment.assert_called_once_with("slice-test-run")


def test_leaves_mlflow_default_when_no_experiment_is_requested():
    with patch.dict("os.environ", {"MLFLOW_EXPERIMENT_NAME": ""}), \
            patch("cpg_ingester.cli.mlflow.set_experiment") as set_experiment:
        _configure_mlflow_experiment()

    set_experiment.assert_not_called()
