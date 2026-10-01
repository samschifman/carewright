"""Fail-closed lifecycle. All semantic scoring is delegated to stage adapters."""

from time import perf_counter
from typing import Protocol

from pydantic import ValidationError

from .capture import Capture
from .models import (
    CaseRef,
    CaseResult,
    OperationalError,
    Receipt,
    RepetitionRecord,
    RunConfig,
    RunEnvelope,
    StageEnvelope,
    StageSummary,
    now,
)
from .registry import RegistryError
from .security import digest, traced, tracing_session
from .storage import MlflowStore, PersistenceError


class Adapter(Protocol):
    def execute(self, case: CaseRef, repetition: int, capture: Capture) -> CaseResult: ...
    def validate(self, record: CaseResult) -> bool: ...
    def summarize(self, records: tuple[CaseResult, ...]) -> StageSummary: ...


class RunFailed(RuntimeError):
    def __init__(self, error: OperationalError, receipt: Receipt | None = None):
        self.error = error
        self.receipt = receipt
        super().__init__(error.message)


@traced
def execute_case(adapter, case, repetition, capture, store):
    """Record raw adapter return BEFORE validating the stage's contract."""
    context = {"stage": capture.stage, "case_id": case.case_id, "repetition": repetition}
    try:
        result = adapter.execute(case, repetition, capture)
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 -- adapter boundary preserves all operational failures
        error = OperationalError(
            kind="timeout" if isinstance(exc, TimeoutError) else "invocation",
            message=str(exc),
            exception_type=type(exc).__name__,
            **context,
        )
        return CaseResult(
            case_id=case.case_id, usable=False, accounting_known=False, errors=(error,)
        )
    if result is None:
        return CaseResult(
            case_id=case.case_id,
            usable=False,
            accounting_known=False,
            errors=(
                OperationalError(
                    kind="empty-result", message="Adapter returned no case", **context
                ),
            ),
        )
    record = None
    try:
        store.put(f"{capture.prefix}/adapter-return.json", result)
        record = CaseResult.model_validate(
            result.model_dump() if isinstance(result, CaseResult) else result
        )
        if record.case_id != case.case_id:
            raise ValueError("Adapter returned a different case ID")
        if adapter.validate(record) is not True:
            raise ValueError("Stage validation rejected case record")
        return record
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 -- adapter boundary preserves all operational failures
        error = OperationalError(
            kind="parse/contract", message=str(exc), exception_type=type(exc).__name__, **context
        )
        store.put(f"{capture.prefix}/validation-error.json", error)
        raw = result.model_dump() if isinstance(result, CaseResult) else result
        counts = {}
        if isinstance(raw, dict):
            for name in ("expected_count", "produced_count", "accounted_count"):
                value = raw.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    counts[name] = value
        return CaseResult(
            case_id=case.case_id,
            usable=False,
            errors=(error,),
            accounting_known=len(counts) == 3,
            **counts,
        )


@traced
def summarize(adapter, records, errors, stage, repetition=None):
    try:
        summary = adapter.summarize(tuple(records))
        return StageSummary.model_validate(
            summary.model_dump() if isinstance(summary, StageSummary) else summary
        )
    except Exception as exc:  # noqa: BLE001 -- adapter boundary preserves all operational failures
        errors.append(
            OperationalError(
                kind="internal",
                message=str(exc),
                stage=stage,
                repetition=repetition,
                exception_type=type(exc).__name__,
            )
        )
        return StageSummary(metrics={}, known_limitations=("Stage aggregation failed",))


def run(config, adapters: dict[str, Adapter], store, registry, *, release=None):
    with tracing_session(store):
        return _run(config, adapters, store, registry, release=release)


@traced
def _run(config, adapters: dict[str, Adapter], store, registry, *, release=None):
    started = now()
    stages = []
    errors = []
    revealed = None
    reserved = False
    receipt = None
    try:
        config = RunConfig.model_validate(
            config.model_dump() if isinstance(config, RunConfig) else config
        )
        if store.redactor.clean(config) != config.model_dump(mode="json"):
            raise ValueError(
                "Configuration contains credentials; pass credentials through the environment"
            )
        if set(adapters) != {s.name for s in config.stages}:
            raise ValueError("Provide exactly one adapter for every declared stage")
        if config.variant != "development" and not (
            isinstance(store, MlflowStore) and store.official
        ):
            raise ValueError("Official variants require RHOAI MLflow persistence")
        if config.split == "holdout" and release is None:
            raise ValueError("Holdout requires a frozen release record")
        registry.reserve(store.run_id, config)
        reserved = True
        store.preflight()  # Every invocation, before ANY adapter invocation.
        store.put("config.json", config)
        if config.split == "holdout":
            store.put("holdout/release.json", release)
            revealed = registry.reveal(config, release, store.run_id)
            store.put(
                "holdout/reveal.json",
                {
                    "release_digest": digest(release),
                    "revealed_at": revealed,
                    "run_id": store.run_id,
                },
            )
        cases = [c for c in config.cases if c.split == config.split]
        for stage in config.stages:
            adapter = adapters[stage.name]
            repetitions = []
            records = []
            stage_errors = []
            attempt_errors = []
            attempts = 0
            for rep in range(1, config.repetitions + 1):
                batch = []
                for case in cases:
                    capture = Capture(store, stage.name, case.case_id, rep)
                    case_started = perf_counter()
                    record = execute_case(adapter, case.model_copy(deep=True), rep, capture, store)
                    store.put(
                        f"{capture.prefix}/execution.json",
                        {
                            "latency_ms": (perf_counter() - case_started) * 1000,
                            "completed_at": now(),
                        },
                    )
                    store.put(f"{capture.prefix}/record.json", record)
                    batch.append(record)
                    stage_errors.extend(record.errors)
                    attempt_errors.extend(capture.errors)
                    attempts += capture.attempts
                summary = summarize(adapter, batch, errors, stage.name, rep)
                repetition = RepetitionRecord(repetition=rep, cases=tuple(batch), summary=summary)
                store.put(f"stages/{stage.name}/repetition-{rep}/summary.json", repetition)
                repetitions.append(repetition)
                records.extend(batch)
                if not any(r.usable for r in batch):
                    errors.append(
                        OperationalError(
                            kind="empty-result",
                            message="No usable cases in repetition",
                            stage=stage.name,
                            repetition=rep,
                        )
                    )
            aggregate = summarize(adapter, records, errors, stage.name)
            envelope = StageEnvelope(
                stage=stage.name,
                configuration=stage,
                repetitions=tuple(repetitions),
                aggregate=aggregate,
                operational_errors=tuple(stage_errors + attempt_errors),
                denominator={
                    "declared_cases": len(cases) * config.repetitions,
                    "usable_cases": sum(r.usable for r in records),
                    "unusable_cases": sum(not r.usable for r in records),
                    "expected_outputs": sum(r.expected_count for r in records),
                    "produced_outputs": sum(r.produced_count for r in records),
                    "accounted_outputs": sum(r.accounted_count for r in records),
                    "unaccounted_outputs": sum(
                        max(r.produced_count - r.accounted_count, 0) for r in records
                    ),
                    "unknown_accounting_cases": sum(not r.accounting_known for r in records),
                    "attempts": attempts,
                    "failed_attempts": len(attempt_errors),
                },
            )
            store.put(f"stages/{stage.name}/summary.json", envelope)
            stages.append(envelope)
        env = RunEnvelope(
            run_id=store.run_id,
            started_at=started,
            completed_at=now(),
            status="failed" if errors else "complete",
            config=config,
            config_digest=digest(config),
            stages=tuple(stages),
            artifacts=tuple(store.refs),
            operational_errors=tuple(errors),
            mlflow_run_id=store.mlflow_run_id,
            artifact_uri=store.artifact_uri,
            holdout_release_digest=digest(release) if release else None,
            holdout_revealed_at=revealed,
        )
        ref = store.put("envelope.json", env)
        store.finish(not errors)
        receipt = Receipt(run_id=store.run_id, envelope=ref, mlflow_run_id=store.mlflow_run_id)
        if errors:
            raise RunFailed(errors[0], receipt)
        return receipt
    except RunFailed:
        raise
    except Exception as exc:
        if isinstance(exc, PersistenceError):
            kind = "persistence"
        elif isinstance(exc, (ValueError, ValidationError, RegistryError)):
            kind = "configuration"
        else:
            kind = "internal"
        error = OperationalError(
            kind=kind, message=store.redactor.text(str(exc)), exception_type=type(exc).__name__
        )
        try:
            store.put(
                "failure.json",
                {
                    "run_id": store.run_id,
                    "error": error.model_dump(mode="json"),
                    "artifacts": [a.model_dump(mode="json") for a in store.refs],
                },
            )
            store.finish(False)
        except Exception as persistence_exc:  # noqa: BLE001 -- failure reporting must not hide original error
            error = error.model_copy(
                update={
                    "message": error.message
                    + "; failure artifact/finalization unavailable ("
                    + type(persistence_exc).__name__
                    + ")"
                }
            )
        raise RunFailed(error, receipt) from exc
    finally:
        if reserved:
            registry.finish(store.run_id)
