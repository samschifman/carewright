# V13 recommendation extractor integration candidate

2026-11-03. Draft application change, stacked on evaluator PR #191. Refs #34 /
RHAIENG-6456. V13 is ready for code review; the original validation did not pass.
This document preserves that result alongside the later, explicitly nonblind
repeat. Merging this prompt would change application behavior; this draft does
not authorize deployment or claim clinical approval.

## Prompt change

The original prompt is available at commit
`81f2febb1fe42123e42929352eb12618b198a6b3`, in
`cpg-ingester/src/cpg_ingester/prompts/rec_extractor.py`.
The PR diff shows the complete change: three appended system instructions.

- Copy manifest `section` exactly, rather than substituting a source recommendation
  number. The section identifies the input selection in this pipeline.
- Preserve explicitly inherited population in `scope_notes` when absent from
  `content`. Keep narrower item restrictions and avoid deriving eligibility from
  a guideline topic or subgroup example.
- Classify diagnostic versus process recommendations by their primary action.
  Calculating/reporting a diagnostic measurement is diagnostic; arranging care
  or specimen logistics is process. The actor or delivery setting is insufficient
  to determine the category.

For example, the tuning outputs sometimes omitted a population stated in an
enclosing heading. V13 asks for that population in the extracted object's scope.
Another tuning failure classified a laboratory-equation recommendation as
`process`; V13 explicitly distinguishes the diagnostic method from its supporting
workflow. These examples informed the prompt and are tuning evidence.

The user template, Recommendation/RecommendationBundle contract, production node,
semantic reviewer, retry behavior, and evaluator are unchanged. This candidate
contains no guideline-specific answers. It is the original production prompt
plus these three blocks, not the separate historical V3 shared-context candidate.
There is no causal ablation establishing the contribution of each block.

## Frozen identities and configuration

| Item | Recorded identity |
|---|---|
| Prompt version | `purpose-type-v13` |
| Prompt digest | `d8e5568c8ce1519caa39fbd6b4e50c606604d6246e3dd54a17c775c468ce95ec` |
| System string SHA256 | `18afa5fe608db6f6c87d853fe54c3186497d40927d3a9ad83d46712b585e571b` |
| User template string SHA256 | `eed4ef0a334db9cf6f3a5daddeb56e82ac3ab91355cb2b9d6fa8e3392b31f9d1` |
| Original production prompt digest | `e091e2aa777254a00b77b4d17818c940fee14f1a4d6bc19fb3ab3fdd8da545ba` |
| Dataset | Reviewed real-only Docling v0.8; `a5eb0cdb4596680dce4b3a10f4449d3b5e837ec5ed2f9d68c4ac96f3440dea61` |
| Evaluator | `1.0.0`; `101417bde2f41792e9699e736bf7dac7a15d91128e9585a8da2e1873349d914c` |
| Model/provider | `gpt-6.1-sol`, OpenAI, `https://api.openai.com` |
| Inference | Medium reasoning; max output 12,000 tokens; default service tier; 300s timeout; SDK retries 0; three repetitions |

String hashes cover UTF-8 runtime values, not Python file formatting. The prompt
digest hashes the canonical JSON mapping of `system` and `user_template` hashes.
Provider model identifiers do not pin immutable weights. Astra-assisted golden
drafting is separate from the Sol extractor runs reported here. These results
do not establish V13 performance on Gemma or other model configurations.

## Results and limits

The dataset contains ten curated, bounded Docling sections / 38 unique
recommendations from real CPGs. Six sections / 21 recommendations were used for
tuning; four sections / 17 recommendations were originally held out. Repetitions
are repeated observations of those cases, not additional independent cases.
Section selection and manifest construction are curated; this does not evaluate
end-to-end recommendation discovery from entire guidelines.

| Run / scoring view | Usable executions | Returned instances | Section agreement | Type agreement | Full certainty agreement |
|---|---:|---:|---:|---:|---:|
| Original production, tuning cases | 18/18 | 63/63 | 27/63 | See baseline artifacts | See baseline artifacts |
| V13, tuning cases | 18/18 | 63/63 | 63/63 | 63/63 | 63/63 |
| V13, original heldout validation | 11/12 | 48/51 planned | 48/48 | 40/48 | 40/48 |
| V13, nonblind repeat, original strict scores | 12/12 | 51/51 | 51/51 | 42/51 | 46/51 |

The tuning run retained all three investigated inherited-population targets in
each of three repetitions. Engineering source review found no material new
clinical-content defect in the usable V13 tuning/validation/repeat outputs.
This is neither a measured clinical error rate nor independent clinical approval.
The repeat retained 15 minor context-omission instances across five references,
plus provenance/formatting limitations.

The original validation had one connection failure and did **not** pass its
frozen gates. Its heldout cases are consumed. The later unchanged-prompt repeat
completed under `caffeinate` without an operational failure. This does not prove
the cause of the original failure, and the repeat is not fresh unseen validation.

Separate, provisional convention diagnostics agree with V13 on all 51 repeat
types and grading-system fields. They use source-only judgments sealed before
model-output comparison, including proven grading-system inheritance. They do
not replace strict scores or approved goldens. In the broader 38-reference audit,
32 labels agree with the proposed taxonomy, three differ, and three remain
ambiguous. Independently supported grading defaults exist for five cases; five
remain unresolved. The proposal itself still needs review.

All other certainty fields (strength, evidence quality, original grade) agree in
48/48 available original-validation instances and 51/51 repeat instances. The
eight original and five repeat strict certainty disagreements concern grading-
system representation. On the same 48 returned instances in both runs, strict
type agreement remains 40/48 and strict certainty changes from 40/48 to 43/48.
Coverage and stochastic encoding variation do not demonstrate prompt improvement.

## Review evidence and persistence

Authenticated RHOAI MLflow links:

- [Original Sol baseline](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/74/runs/106f130474a64677b96e4a977e4a800f?workspace=ksulayma-cpg-to-acp)
- [V13 tuning](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/74/runs/7c4a33f7dfb94059b477470ea71135a5?workspace=ksulayma-cpg-to-acp)
- [Original V13 validation](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/74/runs/b6696582073d4917b35d4d6cd2167bb7?workspace=ksulayma-cpg-to-acp)
- [Post-validation repeat](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/111/runs/4d29cf1815f74a82882b7aae20a2c48f?workspace=ksulayma-cpg-to-acp)
- [Repeat analysis and handoff artifacts](https://rh-ai.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow/#/experiments/112/runs/a05de7bd48d142718945f8b318a7d0bb?workspace=ksulayma-cpg-to-acp)

Raw attempts, machine-readable strict results, source/golden identities,
provisional diagnostic sidecars, and review aggregates remain separate artifacts.
The repeat checkpoint contains 125 files, SHA256
`024138b48f809e87f79f683fdae293cc58bfb2e16197241e5822ac63b198983b`;
28 derived artifacts were uploaded and read-back verified at handoff. The source-
rich review packets require controlled access. Copyrighted sources, goldens and
raw responses are not included in this PR. This summary alone is not a portable
benchmark release; reproducing the campaign requires the frozen private bundle
and its launch configuration. MLflow retention remains an infrastructure concern.

## Before moving out of draft

Review and resolve the category/inheritance conventions, then version any
approved reference, metadata or scoring changes. Establish newly frozen controls
and fresh unseen cases for validation; retain the original failed result.
Review minor omissions and provenance limitations and explicitly decide whether
they meet the application's acceptance criteria. The nonblind diagnostics alone
are insufficient evidence to mark the original validation passed.

For local regression checks, use the evaluator setup in [README.md](README.md).
From `cpg-ingester`, run:

```bash
PYTHONPATH=tests:src:../shared/src:../shared/prompt-eval/src python -m pytest -q \
  tests/test_rec.py tests/test_rec_loop_mechanics.py \
  tests/test_rec_semantic_reviewer.py tests/test_rec_eval_dataset.py \
  tests/test_rec_eval_scoring.py tests/test_rec_eval_adapter.py
```

These mocked/offline checks test application and evaluator mechanics, not clinical
prompt quality. Integration verification must also compare both runtime prompt
string hashes above to the frozen V13 identity.
