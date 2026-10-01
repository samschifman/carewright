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
