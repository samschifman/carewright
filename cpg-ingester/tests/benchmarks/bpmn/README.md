# BPMN Generation-Quality Benchmark

This benchmark evaluates the production ProcessIR creator, validation ladder,
and semantic reviewer against the four approved automation goldens and their
supporting sections in the synthetic hypertension v2 CPG. It measures the
current prompt before any prompt tuning.

Reports are written under working/benchmarks/bpmn/reports/ and logged to the
bpmn-generation-quality MLflow experiment. Each JSON report includes IR diffs
and per-rung first-attempt results.

## Prerequisites and run

The runner uses the cpg-ingester virtual environment and requires a live
LiteLLM-compatible endpoint. Configure the same variables used by the pipeline
in the environment running the command:

    export LITELLM_URL=...
    export LLM_MODEL=...
    export LITELLM_API_KEY=...
    ./cpg-ingester/tests/benchmarks/bpmn/run-benchmark.sh --suite creator --corpus hypertension-v2

The values are required; the runner refuses placeholder defaults. An unset
BPMN_PREFLIGHT_URL skips the optional Kogito preflight and is recorded as
skipped. Set MLFLOW_TRACKING_URI to use a remote tracking server; otherwise
the benchmark uses a local file store under working/benchmarks/bpmn/mlruns/.
Use BENCH_PYTHON=/path/to/python to select another interpreter.

Tag measured prompt revisions with --prompt-rev. Record a baseline before
changing the creator prompt and compare each measured iteration with it.

## Metrics

- First-attempt rung validity: whether the first creator output passed L0,
  L1, L2, L3, L4, L5a, and L5b. Rungs not reached are null.
- Attempts, escalation, tokens, and latency: creator attempts to acceptance,
  human escalation rate, provider-reported token counts when available, and
  wall-clock latency.
- IR fidelity: activity precision/recall/F1 aligned by capability and label,
  flow precision/recall, gateway-type accuracy, parameter names/defaults/units,
  and source-grounding coverage against the golden.

The harness drives the production node functions and routing rules; it does not
duplicate prompt or validation logic.
