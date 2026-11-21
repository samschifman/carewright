# V23 recommendation-extractor integration

2026-11-19. The user selected exact measured V23 for integration review after the
compact-prompt comparison. This supersedes the local V20 application selection.
The original research decision remains `retain_for_iteration_with_open_regressions`;
selection does not accept the known defective output or approve a pipeline release.
The runtime identity below identifies the selected V23 strings.

## Application change

The extractor now uses `compact-abbreviation-v23`, digest
`88e87d41d8f9d6b727bfd6fac458573f76252a9da6b828787db6d790ffeb0faa`.
The integration test computes that identity from the runtime system/user strings
and checks every scripted extractor invocation receives the same system prompt.
The user template is unchanged from upstream/V20/V22. Python line wrapping does
not change either measured string.

V23 consolidates V20's output/metadata, applicability, clinical-purpose,
referenced-qualifier and type instructions, retaining the full V20 abbreviation
expansion rule. Its system estimate is 1,095 tokens versus V20's 1,935. The measured
resource result, rather than that size estimate, supports the selection.

This integration remains based on upstream-main snapshot
`6de0bbd322390dde5e0f7a9c3c083cc3c36aecae`. Only the extractor prompt changes in
application code. Recommendation/RecommendationBundle, schema validation,
reviewer prompt/contract, routing, retry limits, model factory and inference
configuration are unchanged. ACP Writer and deployment artifacts are untouched.
This local selection does not identify the prompt running in any deployment.

## Measured evidence and open findings

[The frozen V23 study and machine-readable results](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/116/runs/cf98eeb496dd4a2d89cb4a339dc92899?workspace=ksulayma-cpg-to-acp) used dataset 0.10.0, evaluator 1.1.0,
frozen production runtime and OpenAI `gpt-6.1-sol`, medium reasoning,
max_tokens=12000, default tier,300-second requests and zero SDK generation retries.
All 39 executions are usable, with114 assigned IDs returned exactly once and 114
contract-valid objects. There are 13 sections/38 references from five real CPGs,
repeated three times. Main 24/63 and controls 15/51 remain separate; one validity-only
case is excluded. These sources were previously exposed, not fresh validation.

| Measurement | Upstream main | V20 control | Selected V23 |
|---|---:|---:|---:|
| Total API tokens | 137,654 | 187,435 | 163,774 |
| Increase over upstream snapshot | — | 36.2% | 19.0% |
| Contract-valid objects /114 | 90 | 114 | 114 |
| Supplied sections preserved /114 | 78 | 114 | 114 |
| Raw type disagreements /114 | 13 | 6 | 6 |
| Treatment-response purpose /3 | 0 | 3 | 3 |
| Inclusive-age reporting scope /3 | 0 | 0 | 2 |
| Supplied actionable preference /3 | 0 | 3 | 3 |
| Diagnostic primary-hypertension context /3 | 0 | 2 | 3 |

V23 uses 19.0% more total tokens than upstream and 12.6% fewer than the longer
V20 research candidate. All three configurations share the frozen benchmark
and model settings. Upstream raw IDs are present despite contract/section
exclusions; its unmatched score labels are not missing clinical bodies. It recovers the predeclared renal,
NICE and UK content expansions missing or partial in V22. These are checked
examples, not exhaustive abbreviation compliance or a measured retrieval gain.

**One material age-scope omission remains:** reporting reference
`nice-ckd-r1-1-1`, repetition1 lacks the explicit inclusive-age applicability.
This is a regression from V22's 3/3 retention, although better than the V20
control's 0/3. Topic labels, adult-specific cautions and other objects' ages do
not repair it. Selection preserves this finding; that output is not accepted
merely because the prompt improves overall measured tradeoffs.

The exact same six type-disagreement instances as V20 remain; their downstream
selection/filtering effect is unknown. Effective source grading is unresolved
66/114 despite raw certainty agreement 114/114. One nonverbatim timing-origin
absence comment and some awkward nested abbreviation formatting remain ancillary
findings. Extra committee/evidence-review remarks were verified in supplied source.
All original scores, literal flags, raw outputs and judgments are preserved.

Controls were generated on November18 and V23 on November19, allowing cross-day
provider drift. Three repetitions are not independent clinical cases. No
independent clinical adjudication, statistical significance, cross-model or
patient-outcome claim follows. Source excerpts and raw outputs stay in private
MLflow storage, rather than being committed with this integration.

## Integration verification

Before replacement, the existing V20 application passes85 relevant tests. With
V23, the same 85 checks pass, including the updated eight integration scenarios.
The suite covers the exact prompt digest and compiled-graph acceptance, minor-only
acceptance, schema feedback/repair, semantic feedback/repair, retry-budget
escalation and immediate operational escalation on missing source or two invalid
reviewer responses. Test inputs are generic scripted fixtures, not new clinical
benchmark cases. The existing MLflow/SQLAlchemy deprecation warning is unchanged.

```sh
MLFLOW_TRACE_SAMPLING_RATIO=0 PYTHONPATH=cpg-ingester/src:shared/src \
  .venv/bin/python -m pytest -q \
  cpg-ingester/tests/test_rec_prompt_integration.py \
  cpg-ingester/tests/test_rec.py \
  cpg-ingester/tests/test_rec_semantic_reviewer.py \
  cpg-ingester/tests/test_rec_loop_mechanics.py \
  cpg-ingester/tests/test_recommendation_escalation_transport.py \
  cpg-ingester/tests/test_review_feedback_consumed.py
```

These tests establish wiring and routing with scripted LLM boundaries. They do
not establish semantic reviewer detection or that a model repairs the age/type
findings. There are no new V23 live review/repair quality probes in this integration.
The [historical V20 live probes](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/118/runs/422aa9b0f94a4579a2506c29bf3bcc88?workspace=ksulayma-cpg-to-acp) observed known
scope/type findings escaping review and assignment-boundary concerns; those are
warnings about relying on repair, not V23 measurements. Reviewer tuning, retrieval,
scope delivery to the composer, final accepted quality and release validation
remain separate work.

## Frozen handoff

- Prompt and all benchmark/model/configuration details:
  [machine-readable V23 results in MLflow](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/116/runs/cf98eeb496dd4a2d89cb4a339dc92899?workspace=ksulayma-cpg-to-acp).
- [Application integration record](recommendation-extractor-v23.json): selection,
  runtime identity, checks, open findings and unchanged boundaries.
- Dataset0.10.0 digest:
  `566ebdaeb17e090c60f084d7d8864c9660adcdc4b27d03dcd291d09421ffd584`.
- Evaluator1.1.0 digest:
  `4cd1cd5304f32f8cb2d59c7207229af72085eaa8f8ee6bc0dc037cb3709663bd`.
- Evaluator source `8c91df0cb1c318865ca449737de2278fd8955ad7` plus exact
  working-file pins; the commit alone is insufficient. Runtime snapshot is
  production-81f2feb; this integration's surrounding code is newer.
- V23 experiment manifest:
  `4d0796b115728ba3c112a7628778031c5d9937f5439731763252640eee008a8f`.
- Raw run UUID`4ab6a3f8-b260-41ed-84fe-6d09d1fbbf97`,
  [MLflow119/3e37261d](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/119/runs/3e37261d3f7e47f887ac302b76db6b53?workspace=ksulayma-cpg-to-acp).
- [Verified full source/results checkpoint116/cf98eeb4](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/116/runs/cf98eeb496dd4a2d89cb4a339dc92899?workspace=ksulayma-cpg-to-acp):
  3,366 files, archive SHA
  `9bbfd03937b1e519cb0a63d1a1af62d872f93e8655fadbfb702ec528ca406ebb`;
  full archive and every inventory member readback-verified.

The checkpoint includes the versioned case/golden/scoring schemas, source/document
and parsing pins, configurations, all raw outputs and controls, strict scores,
source judgments, tests, environment and limitations. The exact historical command
belongs in the preserved evaluation workspace, not this newer integration checkout:

```sh
MLFLOW_DISABLE_AGENT_HINT=1 \
PYTHONPATH=cpg-ingester/tests:cpg-ingester/src:shared/src:shared/prompt-eval/src \
caffeinate -dims "$EVAL_PY" \
working/rec-eval/context-v010/v23-abbreviation/experiment.py run v23
```

`EVAL_PY` is the recorded recommendation-eval environment. Exclusive markers reject
duplicate identities; historical wrapper verification requires its preserved parent
evidence. A new run would need its own identity, not an overwrite of this baseline.
The integration package preserves the separate application patch/test/handoff
and pre-change V20 copies without making additional model calls. Its remote
readback status is recorded separately in the persistence receipt after upload. No merge, deployment or benchmark rescore is included.

## Verified integration review checkpoint

The [persistence receipt](recommendation-extractor-v23-persistence.json) links
[MLflow118/ed2eb49d](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/118/runs/ed2eb49da34e4b9ca11a8245de17584d?workspace=ksulayma-cpg-to-acp):
153 files, SHA
`934d6489a08774e080596ed1c247b5f3e80b1657a75d9e02b6c1f4f7359ad68d`. The remote archive and every member were
readback-verified. It preserves the application/test sources, pre-change V20
copies, checks, environment and review handoff. The application/test files are
unchanged from that checkpoint; publication handoff links were subsequently
consolidated. It made zero additional model calls and performs no commit, push or deployment.
