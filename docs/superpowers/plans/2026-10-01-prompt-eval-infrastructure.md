# Shared Prompt Evaluation Infrastructure Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the approved experiment-mechanics handoff with no stage scoring semantics.
**Architecture:** Independently installable `carewright-prompt-eval` package under `shared/prompt-eval`. Pydantic transport contracts, explicit capture hooks, append-only artifact stores, a SQLite campaign registry, and thin CLI. Stage adapters own execution, validation, and metric aggregation.
**Tech Stack:** Python 3.11+, Pydantic 2, MLflow 3, SQLite, pytest.
**Spec:** ../specs/2026-09-30-prompt-evaluation-experiment-infrastructure-handoff.md

## Global Constraints
- Official variants require operator-selected RHOAI MLflow and upload/download SHA-256 preflight before execution. No automatic fallback.
- Preserve every repetition, stage result, raw attempt, and operational failure independently.
- Freeze dataset/evaluator/production behavior; comparison declares the varying prompt/model axis.
- No clinical semantics, corpora, production prompt changes, or component runtime dependencies.
- Specs remain uncommitted. Issue updates are deferred by the user.

## Review Focus
- Secrets in exception strings, URLs, nested headers, and MLflow traces must not bypass artifact redaction.
- Failed or concurrent writers must not overwrite artifacts or race holdout release/reveal.
- Dataset aliases and altered split manifests must not reopen a revealed holdout.
- Missing, duplicate, unusable, or wrong-case results must remain visible in denominators.
- Comparison must reject partial repetitions and incompatible frozen metadata.

### Task 1: Transport and redaction
Files: `models.py`, `security.py`, `tests/test_contracts.py`.
Interfaces: immutable validated RunConfig/StageConfig/CaseRef, CaseResult, StageSummary, RunEnvelope, Error, Release; canonical_bytes/digest; Redactor.
- [x] Write tests for revision validation, case uniqueness, finite metrics, deterministic digests, nested headers and exception/URL redaction; run pytest and observe missing API failures.
- [x] Implement models and redaction with input/output capture disabled on MLflow tracing; rerun tests.

### Task 2: Immutable persistence and campaign registry
Files: `storage.py`, `registry.py`, `tests/test_storage.py`.
Interfaces: LocalStore/MlflowStore.put/read/preflight/finish, ArtifactRef, Registry.reserve/release/reveal.
- [x] Test exclusive writes, digest corruption, traversal, fake remote round-trip failures, official policy, and transactional reveal/reuse rejection.
- [x] Implement content digests and MLflow round-trip preflight; SQLite serializes campaign transitions. Re-run tests.

### Task 3: Capture and lifecycle
Files: `capture.py`, `runner.py`, `tests/test_runner.py`.
Interfaces: Capture.attempt context manager; Adapter.execute(case, repetition, capture), validate(result), summarize(records); run(config, adapters, store, registry, redactor).
- [x] Test preflight before adapter calls, retries/errors/usage/latency, complete accounting, no usable cases, two independent stages, all repetitions, holdout release/reveal, and durable failure artifacts.
- [x] Implement fail-closed lifecycle and stage-provided summaries; re-run full package tests.

### Task 4: Comparison, CLI, schemas, documentation
Files: `comparison.py`, `cli.py`, `example.py`, schema JSON, example configuration, README, docs/prompt-evaluation.md.
Interfaces: compare(envelopes, varying), `prompt-eval run|verify|compare|release|schema`.
- [x] Test differing repetition/revision rejection and CLI subprocess development, verification, invalid configuration, two stages, and release workflow.
- [x] Implement commands and synthetic opaque adapter, export versioned schema; document official/development commands, adapter hooks, registry ownership/retention, and platform retention limits.

### Task 5: Verification and consumer handoff
- [x] Run full package tests and Ruff; exercise actual local MLflow REST server in addition to fake transport tests.
- [x] Run synthetic RHOAI canary/artifact round-trip if refreshed operator credentials become available; otherwise record exact authentication blocker and runnable command.
- [x] Fresh reviewer checks whole change; fix material findings with regression tests.
- [x] Freeze interface 1.0.0 and publish schema/content fingerprint and commands in consumer handoff. Do not create issues or publish PRs.

## Execution ledger
- Base: 93d8bed (merged PR #187), isolated branch feat/prompt-eval-infrastructure.
- Ruling: separately installable shared utility avoids adding MLflow to production cpg-contracts consumers.
- Ruling: campaign registry is durable SQLite owned by one campaign coordinator; remote artifacts are audit/export storage, not a distributed lock service. Operators must preserve the registry across invocations.
- Live verification currently blocked: OpenShift login expired; user notified asynchronously.

- Tasks 1–4 complete: transport, redaction, storage, registry, capture, runner, comparison, retrieval/export, CLI, schemas, and documentation.
- Ruling: real RHOAI upload/download replaces the proposed extra local REST-server smoke; offline transport tests plus the real server exercise both failure guards and the actual artifact protocol without duplicate setup.
- Fresh read-only reviewer found three Important issues: raw-string credential leakage, zeroed rejected accounting, and whole-set holdout reuse. Each received failing regression tests and was fixed. No Critical or Minor findings.
- Review exclusions accepted: distributed/malicious registry tampering, arbitrary trusted-adapter telemetry/access, and clinical scoring remain outside infrastructure ownership. Incorrect assumptions here could permit cross-host races or external leaks; the documented coordinator/adapter trust boundaries are required.
- Live RHOAI artifacts verified twice: 29 then 33 artifacts; trace destination configuration was corrected after the first smoke. Final committed-revision smoke passed: 33 verified artifacts, MLflow run `0f439ab469874047a6e11649f6fc16c9`, source revision `28cb003f40b2a7066f3f588bc9f7d3e442b4bf62`.

- Final checks: 56/56 package tests, Ruff clean, installed CLI run/verify/export/offline verify, schema fingerprint checks; global rh-pre-commit passed. Specs remain uncommitted. Worktree preserved in `~/repos/cpg-to-acp-worktrees/prompt-eval-infrastructure`.
