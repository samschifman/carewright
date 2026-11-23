# Recommendation extractor evaluator

This is the **evaluator-only first layer** for GitHub #34 / RHAIENG-6456.
It invokes the production `rec_extractor`, scores its recommendations against
source-derived references, and uses the merged `carewright-prompt-eval` interface
**1.0.0**. It does not change production prompts or recommendation contracts.

The handwritten examples in `tests/rec_eval_fixtures.py` are unit-test inputs,
not reviewed clinical goldens or a benchmark baseline. Campaign corpus authoring
(synthetic hypertension, diabetes, and runtime-acquired real CPGs), review,
semantic-reviewer evaluation, live baselines, and tuning are subsequent layers.
No live model was used to develop this layer.
See [verification results and full-suite limitations](VERIFICATION.md).

## Install and test

From the repository root, in a dedicated Python 3.11+ environment:

```bash
python -m venv /tmp/carewright-rec-eval-venv
. /tmp/carewright-rec-eval-venv/bin/activate
python -m pip install -e shared -e 'shared/prompt-eval[test]'
python -m pip install -r cpg-ingester/tests/benchmarks/recommendations/requirements.txt
cd cpg-ingester
export PYTHONPATH=tests:src:../shared/src:../shared/prompt-eval/src
export MLFLOW_DISABLE_AGENT_HINT=1
export MLFLOW_TRACKING_URI=sqlite:////tmp/carewright-rec-eval-tests.sqlite
python -m pytest tests/test_rec_eval_dataset.py tests/test_rec_eval_scoring.py tests/test_rec_eval_adapter.py
```

The local SQLite URI is for **offline tests**, including trace verification;
it is not an official result destination. MLflow's default relative database
can otherwise write into the checkout. To run the entire component suite,
also install `-e '.[test]'` and its declared parser dependencies. Live tests
are excluded by the existing pytest configuration.

Production runtime dependencies do not include this benchmark package.
The adapter has no decision-engine or vector-store dependency.

## Versioned case and scoring schemas

Both stage-owned schemas start at **1.0.0**. Checked-in JSON schemas are under
[`schemas/`](schemas/); authoritative Pydantic types are in `models.py` and
`scoring.py`. The command surface exports their exact schemas:

```bash
python -m benchmarks.recommendations.run_benchmark schema --model case
python -m benchmarks.recommendations.run_benchmark schema --model score
```

A selected case is loaded from `<case_root>/<case_id>.json`. The adapter never
reads a whole corpus directory, so it cannot accidentally inspect holdouts.
`CaseRef` identity/split must agree with the file. Its `source_digests` must
contain these independently pinned identities:

| Key | Bytes hashed with SHA-256 |
|---|---|
| `source` | Exact UTF-8 `source_text` |
| `golden` | Canonical JSON of the file's raw `golden` object |
| `case` | Canonical JSON of the complete case, including annotations and inputs |

Canonical JSON uses sorted keys, compact separators, unescaped Unicode and no
non-finite numbers (`prompt_eval.security.canonical_bytes`). Document and
transformation digests are case metadata, not `CaseRef` source keys: putting a
whole-document digest in both tuning and holdout CaseRefs would deliberately
trigger the shared overlap guard even for distinct sections of that PDF.

Goldens accept recommendation bundle contracts **1.0** and **1.1**. Archived
1.0 cases retain their original bytes and digests. The 1.1 automation template
IDs are outside extraction scoring. Case
validation requires nonblank source and golden text; unique IDs; item/golden/
annotation coverage; matching CPG and section; and exact supporting source
quotes. Required atoms must be source-supported and also declared allowed.
`derivation_status: draft` is permitted only in development; official adapters
require `reviewed`. This label records an external human decision, not an
automatic clinical endorsement.

Each annotation supplies source evidence, action aliases, required/allowed
clinical atoms, population/qualification phrases, and normalization rationale.
Real PDF/text/golden files stay out of Git; select runtime sources from
[`../parsing/real-cpgs.manifest.yaml`](../parsing/real-cpgs.manifest.yaml).
This layer reads already-acquired case files; it neither fetches PDFs nor
authors or certifies campaign goldens.

## Exact evaluator command

From `cpg-ingester` with the environment above, and a complete shared
`RunConfig` at `working/rec-eval/run.json`:

```bash
python -m benchmarks.recommendations.run_benchmark run \
  --config working/rec-eval/run.json \
  --adapter benchmarks.recommendations.run_benchmark:adapters \
  --registry working/rec-eval/campaigns.sqlite \
  --output working/rec-eval/artifacts \
  --receipt working/rec-eval/receipt.json
```

Use the recommendation entry point, not a direct `prompt-eval run`: it binds the
adapter's model to the sole shared `RunConfig.model` without duplicating model
settings in stage parameters. Library callers use `bind_config(config)` while
constructing `adapters(config.stages)`, then call `prompt_eval.runner.run` with
that same configuration.

The only supported stage is `recommendation-extractor`. Its parameters are:

```json
{"case_root": "/absolute/path/to/runtime/cases", "timeout_seconds": 120}
```

The run records provider, bare endpoint origin (no `/v1`), requested model,
service tier, inference parameters, repetitions, source revision, and dataset,
prompt, evaluator and production-behavior revisions. Inference parameters
currently supported are `temperature`, `top_p`, `max_tokens`, `reasoning_effort`,
and `seed`; model/provider support must be checked before a live campaign.
`OPENAI_API_KEY` supplies credentials for the OpenAI-compatible client; never put
credentials in run JSON. SDK retries are disabled so no unrecorded retry can
occur. Each production invocation has one captured provider attempt and an
explicit finite network timeout.

Official `baseline`, `ceiling`, `tuned` and `holdout` variants use the shared
RHOAI MLflow workflow (`--store mlflow` and its environment); local stores cannot
promote a development result. Before baseline use, the corpus layer must supply
actual frozen revisions/digests and reviewed cases. Consult the shared
[consumer handoff](../../../../shared/prompt-eval/HANDOFF.md) and
[user guide](../../../../docs/prompt-evaluation.md) for preflight, release,
comparison, receipts, verified exports, secret redaction and retention.

## Matching and metric definitions

Matching is **identity alignment, not proof of clinical correctness**. An ID
match needs the same CPG and section. Wrong-ID fallback needs an exact normalized
source quote plus a curated action phrase. List position and semantic similarity
alone never match. Conflicting candidate sets remain ambiguous; no arbitrary
output-order tiebreaker is used. Duplicates may therefore leave the contested
golden unmatched; this conservative result requires manual review.

The production extractor explicitly emits `source_cpg: "TBD"`; assembly fills
that value later. The adapter resolves only this documented placeholder to the
case CPG for scoring, preserving both raw and normalized rows. An unrelated
CPG identifier is never silently repaired. This normalization is not a prompt
change or a production contract change.

Every raw row is accounted for as matched or extra. Invalid and ambiguous rows
are extras; duplicates remain in the produced denominator. “Extra” is a
conservative fabrication proxy, **not a diagnosis of hallucination**. Do not
report extras as clinically proven fabrication.

| Metric | Numerator / denominator |
|---|---|
| `recommendation_recall` | Matched identities / expected recommendations |
| `recommendation_precision` | Matched identities / all produced rows |
| `miss_rate` | Missing goldens / expected recommendations |
| `extra_rate`, `duplicate_rate` | Extra or duplicate rows / all produced rows |
| `contract_validity` | Valid recommendation rows / all produced rows |
| ID, source, type, certainty exactness | Exact matched fields / matched identities |
| Content precision / recall / F1 | Multiset token overlap per matched pair; macro at case level |
| Atom, population, qualification preservation | Preserved annotated phrases / declared phrases on matched identities |

Case reports retain numerator/denominator pairs, individual content scores,
missing IDs, extra/duplicate indices, ambiguity, schema errors and certainty
error counts for `strength`, `evidence_quality`, `grading_system`, and
`original_grade`. Zero-denominator ratios are omitted and listed in
`undefined_metrics`; they are never invented perfect scores. A valid empty
recommendations array on a positive case has recall zero. Unparseable/malformed
responses and timeouts are unusable operational errors, not ordinary misses.
Partially invalid lists preserve all rows and metrics plus contract errors;
they cannot be presented as contract-clean results.
Shared run status records lifecycle completion, not a clinical pass: inspect
stage operational errors, contract metrics and excluded-case counts even when
the envelope says `complete`.

Presentation normalization retains comparators, leading decimals, case-sensitive
units (including compound units), numbers,
negation and clinical words. Required phrases and unsupported **numeric** atoms
are checked across content, scope, rationale and remarks. These checks are a
limited deterministic screen, not exhaustive clinical hallucination detection.
The golden author must declare every allowed number used in those fields.
Paraphrases may require manual review even if clinically equivalent.

Aggregate scalar ratios recompute summed case numerators/denominators; content
scores remain averages over matched items, not pooled token counts. Findings
also retain case macro means/ranges/sample counts, per-corpus and per-CPG metrics, and excluded
operational cases. Contract errors and unusable-case counts remain visible.
Extractor scores never include semantic-reviewer decisions.

## Output, tracing and limitations

The machine-readable output is the shared `RunEnvelope` **1.0.0**, with
stage `recommendation-extractor` and detailed `ExtractionScore` **1.0.0** in each
case's findings. The shared layer stores raw request/response, returned model
metadata, token usage, latency, validation errors, raw rows, node artifacts and
summaries. Preserve its receipt and durable campaign registry.

Meaningful functions use the shared safe MLflow tracing wrapper: spans do not
automatically serialize clinical inputs/results. Immutable redacted capture
artifacts provide the full audit data. The production node's raw traces are
suppressed only during its invocation; a real local MLflow integration test
checks safe harness spans and absence of source text. This relies on MLflow
3.16.1's internal scoped `trace_disabled` helper with a fail-closed suppression
check; verify compatibility before SDK upgrades. Disable independent LangSmith
tracing (the adapter refuses enabled environment flags).

The scoped production `get_llm` patch and tracing suppression are process-global.
Run this harness in a dedicated **single-threaded process**, not inside a serving
application or concurrently with another adapter. No production node is forked
or replaced. Current fidelity screens cannot prove clinical equivalence, detect
every invented nonnumeric claim, or replace clinical golden review.

No baseline model choice, raw live baseline, prompt improvement, or frozen
clinical evaluator release is claimed by this first layer. Those are later
handoff deliverables before vLLM model comparisons begin.

## Run and publish a prompt comparison

Use `benchmarks.recommendations.experiment` for new extraction runs. The
manifest contains `version`, `case_root` (relative to the manifest or absolute),
and `cases` (the selected, digest-pinned CaseRefs). Each selected case needs a
reviewed golden and the same dataset version. Prepare a separate manifest for
each comparison set. The model JSON uses the existing ModelSpec fields.

```bash
python -m benchmarks.recommendations.experiment run \
  --dataset working/rec-eval/comparison.json \
  --prompt working/rec-eval/candidate.txt \
  --model-config working/rec-eval/model.json \
  --source-revision "$(git rev-parse HEAD)" \
  --repetitions 3 --output working/rec-eval/runs
```

The prompt selects the exact system text from a UTF-8 file or a numeric
`prompts:/name/version` registry URI. The production user template is reused.
The model route is direct OpenAI. A local run prints its artifact directory
and preserves attempts and scores without a tracking server. For a workspace
authorized to store the evidence, publish the saved directory using
`python -m benchmarks.recommendations.experiment publish RUN_DIRECTORY`.
Publishing checks local evidence digests and sends only opaque case ordinals
and approved score fields through GenAI evaluation. Files uploaded as run
artifacts contain the full redacted evaluation evidence.

Use `replay-case --manifest MANIFEST --case-id CASE --record RECORD` to
recompute a saved case score without inference. Keep archived scores and
goldens intact. Compare paired runs with the same manifest and model settings,
declare the intended gain and attempt budget before running, and review new
critical defects, differential failures, and material tradeoffs before
promotion. Keep the baseline when the evidence shows no meaningful gain.
Mark comparisons inconclusive when coverage or run variability could explain
the observed gain.

### Usage and promotion recommendation

Every verified gain counts, including a field-level improvement when both
prompts share another defect. Keep critical regressions and shared defects
visible separately. No minimum quality delta or universal token cap applies.

The saved report and MLflow run include measured token totals across all
attempts and the number of attempts reporting each quantity. Per-case usage
is in the report. Partial coverage means the total is only the observed
subtotal. Cached input and reasoning are optional breakdowns of input and
output. Include failed attempts with reported usage.

To estimate an incremental cost, subtract cached input from input before
applying the uncached rate, apply the cached rate to cached input, and apply
the output rate to all output. Divide by the attempted ingestion count for a
per-ingestion estimate, or scale by expected volume for a monthly estimate.
Record the rate schedule, coverage, and units for both arms. Reasoning
already included in output is charged only once. Uncertain billing or
missing required usage calls for human review. For self-hosted workloads,
review latency and throughput against the deployment allowance.

Use the policy command after reviewing the paired evidence. For example,
with both cost inputs in dollars per ingestion:

```bash
python -m benchmarks.recommendations.experiment recommend \
  --evidence gain --cost-delta 0.002 --cost-allowance 0.005
```

An observed gain within the allowance recommends promotion.
`--noncritical-regression` or an unavailable or exceeded allowance routes
the decision to human review. Use this flag for material noncritical
tradeoffs, including latency. `--critical-regression` blocks promotion.
Select `inconclusive` if evidence is insufficient, and `no-gain` if the paired
evidence supports keeping the baseline. This recommendation does not change
the application prompt.
