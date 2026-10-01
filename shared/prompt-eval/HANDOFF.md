# Consumer handoff: interface 1.0.0

The implementation is on branch `feat/prompt-eval-infrastructure`, based on
`93d8bed` (merge of Carewright PR #187). Frozen implementation commit: `28cb003f40b2a7066f3f588bc9f7d3e442b4bf62`.
Record this full Git SHA when consuming the interface.
The schema checksums in `INTERFACE.sha256` are the independent contract fingerprint.

The [package README](README.md) provides installation, exact development commands,
Python entry points, and a runnable two-stage adapter. The [user guide](../../docs/prompt-evaluation.md)
provides official RHOAI commands, comparison/release/holdout commands, exports,
retention requirements, and the coordinator/adapter trust boundaries.

## Verified behavior

- 56 offline tests pass; Ruff passes.
- The installed `prompt-eval` entry point runs both opaque synthetic stages,
  verifies their immutable artifacts, exports them, and verifies the export offline.
- Actual RHOAI upload/download verified a canary and the complete two-stage run
  with matching SHA-256 for every artifact. Managed route:
  `https://mlflow-redhat-ods-applications.apps.rosa.agentic-mcp.jolf.p3.openshiftapps.com/mlflow`.
  Workspace: `ksulayma-cpg-to-acp`. Traces target experiment 70 rather than the
  default experiment; the final run verified **33 artifacts** without trace
  destination warnings. [Verification metadata](VERIFICATION.json) records its
  external receipt and implementation commit. Raw artifacts remain outside Git.
- Fresh read-only review identified raw-string redaction, rejected-output accounting,
  and holdout-overlap issues. Regression tests reproduced each issue and now pass.

## Recommendation evaluation integration

1. Install `carewright-prompt-eval` in the benchmark environment; production
   `cpg-contracts` dependencies remain separate.
2. Pin interface 1.0.0, schema checksums, and implementation commit.
3. Supply separate `recommendation-extractor` and `recommendation-reviewer` adapters.
4. Provide actual dataset/evaluator/prompt/production-behavior digests and source
   commit, declared case/source identities, inference metadata, and repetitions.
5. Wrap every provider attempt and retry using capture hooks. Sanitize or disable
   independent adapter autologging before handling credentials or restricted content.
6. Keep one durable campaign registry, preserve receipts, and use the documented
   official baseline/tuned/release/holdout workflow.

Stage ownership includes corpora, goldens, clinical interpretation, matching,
scoring, defect families, prompt tuning, provider timeouts, and source byte checks.
A missing shared lifecycle capability should be versioned here rather than copied
into the recommendation benchmark.

## Operational limitations

- The registry is a durable SQLite coordinator, not a distributed lock service.
  Protect and back it up alongside receipts; do not reset it between campaign runs.
- Credential redaction is fail-closed for recognizable credential-bearing raw text;
  unknown unstructured credentials need explicit `Redactor` secret registration.
- JSON/text payloads are supported; binary sources remain external with pinned digests.
- Trace delivery is asynchronous. Immutable capture artifacts remain the audit source.
- Provider deadlines and external source access are adapter responsibilities.
- The RHOAI volume's `Delete` reclaim policy requires operator retention/backups;
  verified exports provide offline copies.

Issue creation/updates and recommendation-specific implementation are subsequent work.
