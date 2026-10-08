"""Real production node and shared capture/store/runner, with no network provider."""

import json
from dataclasses import dataclass

import pytest
from prompt_eval.capture import Capture
from prompt_eval.example import example_config
from prompt_eval.models import ModelSpec
from prompt_eval.registry import Registry
from prompt_eval.retrieval import retrieve
from prompt_eval.runner import run
from prompt_eval.security import Redactor
from prompt_eval.storage import LocalStore
from rec_eval_fixtures import write_case


@dataclass
class Reply:
    content: object
    response_metadata: dict
    usage_metadata: dict


class Provider:
    def __init__(self, reply):
        self.reply = reply

    def invoke(self, messages):
        assert messages[0]["role"] == "system"
        assert "minutes/week" in messages[1]["content"]
        if isinstance(self.reply, Exception):
            raise self.reply
        return Reply(
            self.reply,
            {"model_name": "offline-test"},
            {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        )


def setup_adapter(tmp_path, response=None):
    from benchmarks.recommendations.extractor_eval import ExtractorAdapter

    data, ref = write_case(tmp_path / "cases")
    config = example_config()
    stage = config.stages[0].model_copy(
        update={
            "parameters": {"case_root": str(tmp_path / "cases"), "timeout_seconds": 10}
        }
    )
    model = ModelSpec(
        provider="test",
        endpoint="https://example.invalid",
        model="offline-test",
        service_tier="default",
        inference_parameters={},
    )
    if response is None:
        response = json.dumps({"recommendations": data["golden"]["recommendations"]})
    adapter = ExtractorAdapter(
        stage,
        model,
        require_reviewed=False,
        provider_factory=lambda _: Provider(response),
    )
    store = LocalStore(tmp_path / "outputs", "test-run", Redactor())
    capture = Capture(store, stage.name, ref.case_id, 1)
    return adapter, ref, capture, store


def test_real_node_capture_and_artifacts(tmp_path):
    adapter, ref, capture, store = setup_adapter(tmp_path)
    record = adapter.execute(ref, 1, capture)
    assert record.usable and adapter.validate(record) is True
    assert record.expected_count == record.produced_count == record.accounted_count == 1
    assert record.metrics["content_f1"] == 1
    attempts = [r for r in store.refs if r.name.endswith("attempt.json")]
    attempt = json.loads(store.read(attempts[0]))
    assert attempt["response"]["response_metadata"]["model_name"] == "offline-test"
    assert attempt["usage"]["input_tokens"] == 5
    assert attempt["latency_ms"] >= 0
    assert (
        record.artifacts["node-artifacts.json"]["recommendations-1.json"][0]["id"]
        == "walk"
    )


@pytest.mark.parametrize(
    "response", ["not json", "[]", '{"recommendations": null}', "{}"]
)
def test_malformed_results_preserve_attempt_and_fail(tmp_path, response):
    adapter, ref, capture, store = setup_adapter(tmp_path, response)
    record = adapter.execute(ref, 1, capture)
    assert not record.usable
    assert record.errors[0].kind == "parse/contract"
    assert any(r.name.endswith("response.json") for r in store.refs)
    assert any("validation-" in r.name for r in store.refs)
    assert not record.accounting_known


def test_invalid_rows_are_retained_and_counted(tmp_path):
    adapter, ref, capture, _ = setup_adapter(
        tmp_path, '{"recommendations": [{"id": "bad"}]}'
    )
    record = adapter.execute(ref, 1, capture)
    assert record.produced_count == record.accounted_count == 1
    assert record.metrics["schema_error_count"] == 1
    assert record.metrics["recommendation_precision"] == 0
    assert record.errors[0].kind == "parse/contract"
    assert capture.errors


def test_empty_valid_array_is_a_miss(tmp_path):
    adapter, ref, capture, _ = setup_adapter(tmp_path, '{"recommendations": []}')
    record = adapter.execute(ref, 1, capture)
    assert record.usable
    assert record.metrics["recommendation_recall"] == 0
    assert record.metrics["missed_recommendations"] == 1


def test_timeout_and_digest_failure(tmp_path):
    adapter, ref, capture, _ = setup_adapter(tmp_path, TimeoutError("offline timeout"))
    record = adapter.execute(ref, 1, capture)
    assert not record.usable and record.errors[0].kind == "timeout"
    bad_ref = ref.model_copy(
        update={"source_digests": {"source": "0" * 64, "golden": "0" * 64}}
    )
    record = adapter.execute(
        bad_ref, 2, Capture(capture.store, capture.stage, ref.case_id, 2)
    )
    assert not record.usable and record.errors[0].kind == "configuration"
    assert not any(
        "repetition-2" in r.name and "attempt-" in r.name for r in capture.store.refs
    )


def test_factory_has_no_case_or_provider_io_and_rejects_reviewer(tmp_path):
    from benchmarks.recommendations.run_benchmark import adapters, bind_config

    cfg = example_config()
    stage = cfg.stages[0].model_copy(
        update={
            "parameters": {"case_root": str(tmp_path / "absent"), "timeout_seconds": 10}
        }
    )
    cfg = cfg.model_copy(update={"stages": (stage,)})
    with bind_config(cfg):
        assert set(adapters(cfg.stages)) == {"recommendation-extractor"}
        with pytest.raises(ValueError, match="configuration"):
            adapters((stage.model_copy(update={"parameters": {}}),))
    with (
        bind_config(example_config()),
        pytest.raises(ValueError, match="recommendation-extractor"),
    ):
        adapters(example_config().stages)


def test_rejected_record_cannot_validate_as_clean(tmp_path):
    adapter, ref, capture, _ = setup_adapter(tmp_path)
    record = adapter.execute(ref, 1, capture)
    bad = record.model_copy(update={"accounted_count": 0})
    assert adapter.validate(bad) is False


def test_shared_runner_receipt_and_aggregate(tmp_path):
    adapter, ref, _, store = setup_adapter(tmp_path)
    cfg = example_config().model_copy(
        update={
            "stages": (adapter.stage,),
            "cases": (ref,),
            "repetitions": 2,
            "model": adapter.model,
        }
    )
    receipt = run(
        cfg,
        {adapter.stage.name: adapter},
        store,
        Registry(tmp_path / "registry.sqlite"),
    )
    envelope = retrieve(receipt)
    assert envelope.status == "complete"
    summary = envelope.stages[0].aggregate
    assert summary.metrics["expected_count"] == 2
    assert summary.metrics["usable_cases"] == 2
    assert summary.metrics["content_f1"] == 1
    assert summary.findings["by_corpus"]["unit"]["case_count"] == 2
    assert all(store.read(r) for r in store.refs)


def test_tracing_is_restored_after_failure(tmp_path):
    from mlflow.tracing.provider import is_tracing_enabled

    adapter, ref, capture, _ = setup_adapter(tmp_path, TimeoutError("no network"))
    was_enabled = is_tracing_enabled()
    adapter.execute(ref, 1, capture)
    assert is_tracing_enabled() == was_enabled


def test_schema_cli_exposes_typed_case_and_score(capsys):
    from benchmarks.recommendations.run_benchmark import main

    assert main(["schema", "--model", "case"]) == 0
    assert "golden" in json.loads(capsys.readouterr().out)["properties"]
    assert main(["schema", "--model", "score"]) == 0
    assert "accounted_count" in json.loads(capsys.readouterr().out)["properties"]


def test_official_local_run_stops_before_provider(tmp_path):
    from prompt_eval.runner import RunFailed

    adapter, ref, _, store = setup_adapter(tmp_path)
    cfg = example_config().model_copy(
        update={"stages": (adapter.stage,), "cases": (ref,), "variant": "baseline"}
    )
    with pytest.raises(RunFailed, match="RHOAI"):
        run(
            cfg,
            {adapter.stage.name: adapter},
            store,
            Registry(tmp_path / "registry.sqlite"),
        )
    assert not any("attempt-" in r.name for r in store.refs)


def test_safe_traces_do_not_include_source_text(tmp_path):
    import mlflow
    from prompt_eval.security import tracing_session
    from prompt_eval.storage import MlflowStore

    adapter, ref, _, _ = setup_adapter(tmp_path)
    uri = f"sqlite:///{tmp_path}/traces.sqlite"
    client = mlflow.MlflowClient(tracking_uri=uri)
    experiment_id = client.create_experiment(
        "unit-safe-traces", artifact_location=(tmp_path / "trace-artifacts").as_uri()
    )
    store = MlflowStore(
        uri, "unit-safe-traces", "trace-test", Redactor(), official=False
    )
    with tracing_session(store):
        record = adapter.execute(
            ref, 1, Capture(store, adapter.stage.name, ref.case_id, 1)
        )
    assert record.usable
    traces = client.search_traces(locations=[experiment_id])
    assert traces
    spans = [
        span
        for trace in traces
        for span in client.get_trace(trace.info.trace_id).data.spans
    ]
    assert any("execute" in span.name for span in spans)
    assert any("score" in span.name for span in spans)
    assert "minutes/week" not in str([span.to_dict() for span in spans])


def test_module_entry_point_reaches_shared_preflight(tmp_path):
    import os
    import subprocess
    import sys

    config = example_config()
    stage = config.stages[0].model_copy(
        update={
            "parameters": {"case_root": str(tmp_path / "absent"), "timeout_seconds": 10}
        }
    )
    config = config.model_copy(update={"stages": (stage,), "variant": "baseline"})
    path = tmp_path / "run.json"
    path.write_text(config.model_dump_json())
    env = {
        **os.environ,
        "PYTHONPATH": "tests:src:../shared/src:../shared/prompt-eval/src",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "benchmarks.recommendations.run_benchmark",
            "run",
            "--config",
            str(path),
            "--adapter",
            "benchmarks.recommendations.run_benchmark:adapters",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--output",
            str(tmp_path / "artifacts"),
            "--receipt",
            str(tmp_path / "receipt.json"),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "RHOAI" in json.loads(result.stdout)["error"]["message"]


def test_production_tbd_placeholder_resolves_only_for_scoring(tmp_path):
    from rec_eval_fixtures import example_case

    row = example_case()["golden"]["recommendations"][0]
    row["source_cpg"] = "TBD"
    adapter, ref, capture, _ = setup_adapter(
        tmp_path, json.dumps({"recommendations": [row]})
    )
    record = adapter.execute(ref, 1, capture)
    assert record.metrics["recommendation_recall"] == 1
    assert record.metrics["content_f1"] == 1
    assert record.artifacts["raw-rows.json"][0]["source_cpg"] == "TBD"
    assert record.artifacts["normalized-rows.json"][0]["source_cpg"] == "unit-cpg"
    assert (
        adapter.summarize((record,)).findings["by_cpg"]["unit-cpg"]["metrics"][
            "content_f1"
        ]
        == 1
    )
