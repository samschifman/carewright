"""Production extractor integration; experiment lifecycle belongs to prompt_eval."""

import importlib
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from cpg_contracts import content_to_text
from mlflow.tracing.provider import is_tracing_enabled, trace_disabled
from prompt_eval.models import CaseResult, ModelSpec, OperationalError, StageSummary
from prompt_eval.security import traced
from prompt_eval.storage import PersistenceError

from .dataset import load_case
from .scoring import LIMITATIONS, ExtractionScore, score


@traced
def normalize_extractor_rows(case, rows):
    """Mirror the assembly boundary's documented TBD source substitution only."""
    return [
        {**row, "source_cpg": case.source_cpg}
        if isinstance(row, dict) and row.get("source_cpg") == "TBD"
        else row
        for row in rows
    ]


@traced
def group_metrics(records):
    scores = [ExtractionScore.model_validate(r.findings["score"]) for r in records]
    result = {
        "expected_count": sum(r.expected_count for r in records),
        "produced_count": sum(r.produced_count for r in records),
    }
    for name in {key for s in scores for key in s.ratios}:
        num = sum(s.ratios[name][0] for s in scores)
        den = sum(s.ratios[name][1] for s in scores)
        if den:
            result[name] = num / den
    for name in {
        key
        for s in scores
        for key in s.metrics
        if key.endswith("_count")
        or key in ("missed_recommendations", "extra_recommendations")
    } - {"expected_count", "produced_count"}:
        result[name] = sum(s.metrics.get(name, 0) for s in scores)
    return result


@traced
def make_provider(model: ModelSpec, timeout: float):
    from langchain_openai import ChatOpenAI

    # Disable SDK retries: hidden wire retries would escape per-attempt capture.
    return ChatOpenAI(
        base_url=model.endpoint.rstrip("/") + "/v1",
        model=model.model,
        api_key=os.environ["OPENAI_API_KEY"],
        use_responses_api=True,
        max_retries=0,
        request_timeout=timeout,
        service_tier=model.service_tier,
        **model.inference_parameters,
    )


class CapturedProvider:
    def __init__(self, provider, case, capture):
        self.provider, self.case, self.capture = provider, case, capture
        self.rows = None
        self.parse_error = None
        self.extraction_score = None

    @traced
    def invoke(self, messages):
        from cpg_ingester.nodes.structure_analyzer import _parse_llm_json

        with self.capture.attempt({"messages": messages}) as attempt:
            response = self.provider.invoke(messages)
            attempt.response(
                {
                    "content": response.content,
                    "response_metadata": response.response_metadata,
                    "usage_metadata": response.usage_metadata,
                }
            )
            usage = response.usage_metadata or {}
            attempt.usage(
                **{
                    k: v
                    for k, v in usage.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                }
            )
            try:
                parsed = _parse_llm_json(content_to_text(response.content))
                if not isinstance(parsed, dict) or not isinstance(
                    parsed.get("recommendations"), list
                ):
                    raise TypeError("Response must contain a recommendations array")
                self.rows = parsed["recommendations"]
                self.extraction_score = score(
                    self.case, normalize_extractor_rows(self.case, self.rows)
                )
                if self.extraction_score.validation_errors:
                    attempt.validation_error(self.extraction_score.validation_errors)
            except (ValueError, TypeError) as exc:
                self.parse_error = str(exc)
                attempt.validation_error({"error": self.parse_error})
            return response


@trace_disabled
def _invoke_without_raw_traces(node, state):
    """Single-process scoped suppression; outer safe harness span remains active."""
    # MLflow's scoped helper restores the same provider. disable()+enable()
    # shuts down the processor of an already-active parent harness span.
    if is_tracing_enabled():
        raise RuntimeError("Could not suppress raw production traces")
    return node(state)


class ExtractorAdapter:
    def __init__(
        self, stage, model: ModelSpec, *, require_reviewed: bool, provider_factory=None
    ):
        self.stage, self.model = stage, model
        if set(stage.parameters) != {"case_root", "timeout_seconds"}:
            raise ValueError(
                "Stage parameters require only case_root and timeout_seconds"
            )
        self.case_root = Path(stage.parameters["case_root"])
        self.timeout = stage.parameters["timeout_seconds"]
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        endpoint = urlsplit(model.endpoint)
        if (
            endpoint.scheme not in ("http", "https")
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.path not in ("", "/")
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError(
                "Endpoint must be a credential-free bare origin, without /v1"
            )
        allowed = {"temperature", "top_p", "max_tokens", "reasoning_effort", "seed"}
        if not set(model.inference_parameters) <= allowed:
            raise ValueError(
                "Unsupported inference parameters; cannot override endpoint/retries/credentials"
            )
        self.require_reviewed = require_reviewed
        self.provider_factory = provider_factory or (
            lambda spec: make_provider(spec, self.timeout)
        )

    @traced
    def execute(self, ref, repetition, capture) -> CaseResult:
        expected = 0
        wrapped = None
        case = None
        artifacts = {}
        try:
            case = load_case(
                ref, self.case_root, require_reviewed=self.require_reviewed
            )
            expected = len(case.golden.recommendations)
            if any(
                os.environ.get(k, "").lower() in ("true", "1")
                for k in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING")
            ):
                raise ValueError(
                    "Disable independent LangSmith tracing before restricted inputs"
                )
            module = importlib.import_module("cpg_ingester.nodes.rec_extractor")
            wrapped = CapturedProvider(self.provider_factory(self.model), case, capture)
            with TemporaryDirectory(prefix="rec-eval-") as out:
                state = {
                    "items": list(case.items),
                    "source_pages": case.source_text,
                    "grading_definitions": case.grading_definitions,
                    "abbreviations": case.abbreviations,
                    "output_dir": out,
                }
                with patch.object(module, "get_llm", return_value=wrapped):
                    try:
                        result = _invoke_without_raw_traces(module.rec_extractor, state)
                    finally:
                        artifacts["node-artifacts.json"] = {
                            p.name: json.loads(p.read_text(encoding="utf-8"))
                            for p in Path(out).glob("*.json")
                        }
                artifacts["node-result.json"] = result
            if wrapped.parse_error or wrapped.rows is None:
                raise ValueError(
                    wrapped.parse_error
                    or "Extractor produced no parseable recommendations"
                )
            # Score outside suppressed node tracing to emit a safe transformation span.
            normalized = normalize_extractor_rows(case, wrapped.rows)
            scored = score(case, normalized)
            errors = tuple(capture.errors)
            return CaseResult(
                case_id=ref.case_id,
                expected_count=expected,
                produced_count=scored.produced_count,
                accounted_count=scored.accounted_count,
                metrics=scored.metrics,
                findings={
                    "corpus": case.corpus,
                    "source_cpg": case.source_cpg,
                    "score": scored.model_dump(mode="json"),
                },
                artifacts={
                    **artifacts,
                    "raw-rows.json": wrapped.rows,
                    "normalized-rows.json": normalized,
                },
                errors=errors,
                known_limitations=LIMITATIONS,
            )
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 -- adapter boundary retains operational failures
            if wrapped and wrapped.parse_error:
                kind = "parse/contract"
            elif (
                isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError"
            ):
                kind = "timeout"
            else:
                kind = "invocation" if wrapped else "configuration"
            rows = wrapped.rows if wrapped else None
            produced = len(rows) if rows is not None else 0
            error = OperationalError(
                kind=kind,
                message=str(exc),
                stage=self.stage.name,
                case_id=ref.case_id,
                repetition=repetition,
                exception_type=type(exc).__name__,
            )
            return CaseResult(
                case_id=ref.case_id,
                usable=False,
                expected_count=expected,
                produced_count=produced,
                accounted_count=produced,
                accounting_known=rows is not None,
                errors=(error,),
                artifacts=artifacts,
                known_limitations=LIMITATIONS,
            )

    @traced
    def validate(self, record: CaseResult) -> bool:
        if not record.usable:
            return bool(record.errors)
        try:
            value = ExtractionScore.model_validate(record.findings["score"])
            return (
                record.accounting_known
                and record.produced_count == record.accounted_count
                and (
                    record.expected_count,
                    record.produced_count,
                    record.accounted_count,
                )
                == (value.expected_count, value.produced_count, value.accounted_count)
                and value.accounted_count == value.matched_count + value.extra_count
                and value.expected_count == value.matched_count + value.missing_count
                and value.produced_count
                == value.valid_contract_count + value.schema_error_count
                and record.metrics == value.metrics
                and (not value.schema_error_count or bool(record.errors))
            )
        except (ValueError, TypeError, KeyError):
            return False

    @traced
    def summarize(self, records) -> StageSummary:
        usable = [r for r in records if r.usable]
        metrics = {
            "usable_cases": len(usable),
            "unusable_cases": len(records) - len(usable),
            "operational_error_count": sum(len(r.errors) for r in records),
            "expected_count": sum(r.expected_count for r in records),
            "produced_count": sum(r.produced_count for r in records),
            "accounted_count": sum(r.accounted_count for r in records),
        }
        scores = [ExtractionScore.model_validate(r.findings["score"]) for r in usable]
        ratio_names = {name for s in scores for name in s.ratios}
        for name in ratio_names:
            numerator = sum(s.ratios[name][0] for s in scores)
            denominator = sum(s.ratios[name][1] for s in scores)
            if denominator:
                metrics[name] = numerator / denominator
        for key in {
            key
            for s in scores
            for key in s.metrics
            if key.endswith("_count")
            or key in ("missed_recommendations", "extra_recommendations")
        } - {"expected_count", "produced_count"}:
            metrics[key] = sum(s.metrics.get(key, 0) for s in scores)
        macro, by_corpus, by_cpg = {}, {}, {}
        for name in ratio_names:
            values = [s.metrics[name] for s in scores if name in s.metrics]
            if values:
                macro[name] = {
                    "mean": sum(values) / len(values),
                    "min": min(values),
                    "max": max(values),
                    "n": len(values),
                }
        for record in usable:
            summary = by_corpus.setdefault(
                record.findings["corpus"],
                {
                    "case_count": 0,
                    "expected_count": 0,
                    "produced_count": 0,
                    "matched_count": 0,
                },
            )
            summary["case_count"] += 1
            summary["expected_count"] += record.expected_count
            summary["produced_count"] += record.produced_count
            summary["matched_count"] += record.metrics["matched_count"]
        for field, grouping in (("corpus", by_corpus), ("source_cpg", by_cpg)):
            for key in {r.findings[field] for r in usable}:
                selected = [r for r in usable if r.findings[field] == key]
                grouping.setdefault(key, {"case_count": len(selected)})["metrics"] = (
                    group_metrics(selected)
                )
        return StageSummary(
            metrics=metrics,
            findings={
                "macro_ranges": macro,
                "by_corpus": by_corpus,
                "by_cpg": by_cpg,
                "excluded_cases": [r.case_id for r in records if not r.usable],
            },
            known_limitations=LIMITATIONS,
        )
