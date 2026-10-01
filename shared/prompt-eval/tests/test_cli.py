import json
import os
import subprocess
import sys
from pathlib import Path


def cli(*args):
    env = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "prompt_eval", *map(str, args)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_two_stage_example_run_verify_and_schema(tmp_path):
    result = cli("example-config")
    assert result.returncode == 0, result.stderr
    cfg = tmp_path / "config.json"
    cfg.write_text(result.stdout)
    receipt = tmp_path / "receipt.json"
    result = cli(
        "run",
        "--config",
        cfg,
        "--adapter",
        "prompt_eval.example:adapters",
        "--registry",
        tmp_path / "registry.db",
        "--output",
        tmp_path / "artifacts",
        "--receipt",
        receipt,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert receipt.exists()
    result = cli("verify", "--receipt", receipt)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["run_id"] == data["run_id"]
    result = cli("schema")
    assert result.returncode == 0
    assert json.loads(result.stdout)["title"] == "RunEnvelope"


def test_cli_configuration_failure_is_json_and_nonzero(tmp_path):
    cfg = tmp_path / "invalid.json"
    cfg.write_text('{"api_key":"do-not-print"}')
    result = cli(
        "run",
        "--config",
        cfg,
        "--adapter",
        "prompt_eval.example:adapters",
        "--registry",
        tmp_path / "registry.db",
        "--output",
        tmp_path / "artifacts",
        "--receipt",
        tmp_path / "receipt.json",
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["kind"] == "configuration"
    assert "do-not-print" not in result.stdout + result.stderr
