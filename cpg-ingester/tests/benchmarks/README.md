# CPG Ingester Benchmarks

This directory indexes the self-contained quality benchmarks for
`cpg-ingester`. It is a discovery page, not a shared evaluation schema: each
pipeline stage has a different output contract and owns the metrics, dataset,
runner, and result format needed to evaluate that contract faithfully.

## Benchmark index

| Stage | Location | Tracking | Status |
|---|---|---|---|
| PDF parsing | [`parsing/`](parsing/) | RHAIENG-6461 | Available |
| Recommendation extraction | [`recommendations/`](recommendations/) | GitHub #34 / RHAIENG-6456 | Offline-tested evaluator; campaign corpus/baseline pending |
| Recommendation semantic review | `recommendations/` | GitHub #34 / RHAIENG-6456 | Subsequent layer; not implemented |

Add an entry when a benchmark becomes available, and link its exact command,
versioned dataset and scoring schema, frozen prompt/evaluator revision, model
configuration, raw and summarized results, known limitations, and
machine-readable result format from its own README.

## Starting another prompt-improvement effort

1. Read the Phase 5 workflow in
   [`dev_docs/project-plan.md`](../../../dev_docs/project-plan.md) and inspect
   the completed benchmark handoffs above.
2. Establish and preserve an untuned baseline before changing prompts.
3. Keep the benchmark self-contained and appropriate to the production output
   contract; do not force a stage into another benchmark's metrics or schema.
4. Version the stage-specific corpus with synthetic hypertension, synthetic
   diabetes, and at least one runtime-acquired real CPG. Reserve at least one
   case or section from prompt tuning for final validation.
5. Link the completed handoff from its GitHub and Jira tracking issues and add
   any candidate reusable lessons to GitHub #31 / RHAIENG-6453.

After at least two prompt-improvement efforts are complete, #31/RHAIENG-6453
will compare the concrete approaches and document only the patterns that proved
useful across them.

## Real CPG inputs

Do not commit real CPG PDFs. Reuse
[`parsing/real-cpgs.manifest.yaml`](parsing/real-cpgs.manifest.yaml) and
[`parsing/fetch-benchmark-cpgs.sh`](parsing/fetch-benchmark-cpgs.sh) to acquire
them at runtime under the gitignored working directory.

The manifest is the source inventory, not evaluation ground truth. Before a
real CPG participates in a reproducible baseline, pin and verify its SHA-256 and
create versioned, source-auditable goldens for the behavior being measured.
Freeze source identity, transformation configuration, and artifact digests;
keep real PDFs, full extracted sections, detailed goldens where required by
content policy, and raw model outputs out of Git and in the approved artifact
store.
