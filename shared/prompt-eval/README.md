# Prompt evaluation infrastructure

`carewright-prompt-eval` provides experiment mechanics for stage-owned benchmarks.
Consumer interface and transport schema: **1.0.0**. It has no clinical scoring,
goldens, production prompts, or dependency on either application component.

See [the user guide](../../docs/prompt-evaluation.md) for configuration, official
runs, holdout release, persistence, retention, and security boundaries.

## Install and verify

From the repository root, with Python 3.11 or newer:

```bash
python -m venv .venv-prompt-eval
. .venv-prompt-eval/bin/activate
python -m pip install -e 'shared/prompt-eval[test]'
python -m pytest shared/prompt-eval/tests
ruff check --config shared/prompt-eval/pyproject.toml shared/prompt-eval
```

MLflow 3.16.1 or newer is required for the tested workspace-aware client and
explicit trace destinations. MLflow credentials use its standard environment;
this package never reads application or Kubernetes secrets itself.

## No-model example

```bash
mkdir -p working/prompt-eval
prompt-eval example-config > working/prompt-eval/example.json
prompt-eval run \
  --config working/prompt-eval/example.json \
  --adapter prompt_eval.example:adapters \
  --registry working/prompt-eval/campaigns.sqlite \
  --output working/prompt-eval/artifacts \
  --receipt working/prompt-eval/development-receipt.json
prompt-eval verify --receipt working/prompt-eval/development-receipt.json
```

The example emits independent `recommendation-extractor` and
`recommendation-reviewer` envelopes. Its `characters` and `checks` metrics are
synthetic transport demonstrations, **not recommendation-quality measurements**.
Every command emits one JSON value to stdout; SDK progress goes to stderr.
Failed commands exit nonzero with a typed operational error.

## Adapter contract

A trusted `module:factory` receives only `tuple[StageConfig, ...]` and returns
`dict[str, Adapter]`. It must not load cases or call models during import or
factory construction. Execution starts only after configuration validation,
registry reservation, and persistence preflight. Implement these methods:

```python
from prompt_eval.models import CaseResult, StageSummary
from prompt_eval.security import traced


class MyStage:
    @traced
    def execute(self, case, repetition, capture):
        # Load only this supplied case. Wrap EVERY provider attempt, including retries.
        with capture.attempt({"messages": [{"role": "user", "content": case.case_id}]}) as attempt:
            response = {"synthetic_answer": case.case_id}  # Replace with stage-owned invocation.
            attempt.response(response)
            attempt.usage(input_tokens=0, output_tokens=0)
            # On parse/schema failure: attempt.validation_error(details).
        return CaseResult(
            case_id=case.case_id,
            metrics={"stage_score": 1.0},
            findings=response,
            expected_count=1,
            produced_count=1,
            accounted_count=1,
        )

    @traced
    def validate(self, record):
        return "stage_score" in record.metrics

    @traced
    def summarize(self, records):
        usable = [r for r in records if r.usable]
        return StageSummary(
            metrics={
                "stage_score": sum(r.metrics["stage_score"] for r in usable) / len(usable)
                if usable
                else 0.0,
            }
        )
```

`execute` is synchronous; async production adapters should provide their own
synchronous bridge. Retry budgets, timeouts, source loading, matching, output
accounting, clinical validity, and metric denominators belong to the adapter.
The infrastructure records `TimeoutError`; the adapter must set actual provider
network deadlines. It does not try to interrupt arbitrary Python code.

`validate` must return exactly `True` for a usable record. `summarize` is called
once per repetition and once across all repetitions, including unusable records;
the adapter chooses how to score them. Unusable records can preserve mismatched
output counts with a contract error; usable records require complete accounting. Returning no usable case in any repetition
fails the run. A usable subset may complete with explicitly retained operational
errors and denominators; completeness is not a clinical pass.

JSON-compatible payloads, including Unicode strings, are accepted in `findings`
and `artifacts`. Bytes and arbitrary Python objects are rejected rather than
serialized with an unsafe `str()` fallback. Convert text explicitly; store binary
source documents separately and pin their digests in `CaseRef.source_digests`.
Case, repetition, and aggregate artifacts are preserved within their typed records.

## Public Python entry points

- `models`: `RunConfig`, `StageConfig`, `CaseRef`, `CaseResult`, `StageSummary`,
  `RunEnvelope`, `Release`, `Receipt`, `OperationalError`, `ArtifactRef`.
- `runner.run(config, adapters, store, registry, release=None) -> Receipt`.
  Failure raises `RunFailed`, with `.error` and an optional failed-run `.receipt`.
- `capture.Capture.attempt(request)` yields `Attempt` with `response`,
  `validation_error`, and `usage` hooks.
- `storage.LocalStore(root, run_id, redactor)` and
  `storage.MlflowStore(tracking_uri, experiment, run_id, redactor,
  official=True, approved_uri=...)`.
- `registry.Registry(path)` maintains a single coordinator's durable campaign state.
- `retrieval.retrieve(receipt, tracking_uri=..., export_dir=...)` verifies all bytes.
- `retrieval.export_run(receipt, destination, tracking_uri=...)` exports without
  changing original identities or digests.
- `comparison.compare(envelopes, varying="prompt" | "model")` returns stage-tagged
  repetition means/ranges, stage aggregates, denominators, and errors.
- `comparison.make_release(baseline, selected)` creates the frozen release;
  `Registry.release(release, selected.config)` atomically freezes the campaign.

The schemas in `schemas/` are generated by `prompt-eval schema --model
config|envelope|release|receipt`. [Interface fingerprint](INTERFACE.sha256) pins the
schema bytes independently of a Git commit. New incompatible changes require a
new interface version. Recommendation evaluation should record this version,
the fingerprint, and the implementation commit from its checkout.
