"""Suite loader, executor, and aggregator for the benchmark harness."""

import json
import logging
from pathlib import Path
from typing import Any

from acp_writer.benchmark.models import (
    BenchmarkCase,
    CaseScore,
    CategoryResult,
    SuiteResult,
)
from acp_writer.benchmark.protocol import QABackend
from acp_writer.benchmark.scoring import score_case
from cpg_contracts.tracing import trace_safe

logger = logging.getLogger(__name__)

BENCHMARKS_DIR = Path(__file__).parent.parent.parent.parent / "benchmarks"


def load_suite(suite_name: str) -> list[BenchmarkCase]:
    """Load test cases from a suite JSON file."""
    suite_path = BENCHMARKS_DIR / "suites" / f"{suite_name}.json"
    if not suite_path.exists():
        raise FileNotFoundError(f"Suite not found: {suite_path}")

    with open(suite_path) as f:
        raw = json.load(f)

    return [BenchmarkCase.from_dict(tc) for tc in raw]


def load_bundle(bundle_path: str) -> dict[str, Any]:
    """Load a FHIR bundle from the benchmarks/bundles directory."""
    full_path = BENCHMARKS_DIR / bundle_path
    if not full_path.exists():
        raise FileNotFoundError(f"Bundle not found: {full_path}")

    with open(full_path) as f:
        return json.load(f)


def available_suites() -> list[str]:
    """List available suite names."""
    suites_dir = BENCHMARKS_DIR / "suites"
    if not suites_dir.exists():
        return []
    return sorted(p.stem for p in suites_dir.glob("*.json"))


@trace_safe(name="benchmark_run_case")
def run_case(
    test_case: BenchmarkCase,
    backend: QABackend,
    bundle: dict[str, Any],
    numeric_tolerance: float = 0.0,
) -> CaseScore:
    """Execute a single test case and score the result."""
    try:
        answer = backend.answer(
            question=test_case.question,
            bundle=bundle,
            reference_date=test_case.reference_date,
            structured_intent=test_case.structured_intent,
        )
    except Exception as exc:
        from acp_writer.benchmark.models import QAAnswer

        answer = QAAnswer(
            value=None,
            kind="insufficient_data",
            insufficient_data=True,
            error=f"Backend exception: {exc}",
        )

    return score_case(test_case, answer, numeric_tolerance)


@trace_safe(name="benchmark_run_suite")
def run_suite(
    suite_name: str,
    backend: QABackend,
    numeric_tolerance: float = 0.0,
) -> SuiteResult:
    """Run all test cases in a suite against a backend."""
    test_cases = load_suite(suite_name)

    bundle_cache: dict[str, dict] = {}
    case_scores: list[CaseScore] = []

    for tc in test_cases:
        if tc.bundle not in bundle_cache:
            bundle_cache[tc.bundle] = load_bundle(tc.bundle)

        cs = run_case(tc, backend, bundle_cache[tc.bundle], numeric_tolerance)
        case_scores.append(cs)
        logger.debug(
            "%s: correct=%s hallucination=%s expected=%s actual=%s",
            cs.test_id, cs.correct, cs.hallucination, cs.expected, cs.actual,
        )

    return _aggregate(suite_name, backend.name, test_cases, case_scores)


def run_suite_instrumented(
    suite_name: str,
    backend: QABackend,
    dump_path: Path | None = None,
    numeric_tolerance: float = 0.0,
) -> SuiteResult:
    """Run suite with per-case instrumented dump for investigation.

    Instrumentation added for hallucination investigation (2026-08-13).
    Saves per-case JSON with answered_by, resolution_basis, and full answer.
    """
    test_cases = load_suite(suite_name)
    bundle_cache: dict[str, dict] = {}
    case_scores: list[CaseScore] = []
    case_details: list[dict] = []

    for tc in test_cases:
        if tc.bundle not in bundle_cache:
            bundle_cache[tc.bundle] = load_bundle(tc.bundle)

        bundle = bundle_cache[tc.bundle]
        try:
            answer = backend.answer(
                question=tc.question,
                bundle=bundle,
                reference_date=tc.reference_date,
                structured_intent=tc.structured_intent,
            )
        except Exception as exc:
            from acp_writer.benchmark.models import QAAnswer
            answer = QAAnswer(
                value=None, kind="insufficient_data",
                insufficient_data=True, error=f"Backend exception: {exc}",
            )

        cs = score_case(tc, answer, numeric_tolerance)
        case_scores.append(cs)

        case_details.append({
            "id": tc.id,
            "category": tc.category,
            "level": tc.level,
            "question": tc.question,
            "expected": tc.expected,
            "actual_value": answer.value,
            "actual_kind": answer.kind,
            "correct": cs.correct,
            "hallucination": cs.hallucination,
            "provenance_correct": cs.provenance_correct,
            "provenance": answer.provenance,
            "insufficient_data": answer.insufficient_data,
            "error": answer.error,
            "answered_by": answer.answered_by,
            "resolution_basis": answer.resolution_basis,
        })

    if dump_path:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dump_path, "w") as f:
            for detail in case_details:
                f.write(json.dumps(detail, default=str) + "\n")
        logger.info("Instrumented dump written to %s", dump_path)

    return _aggregate(suite_name, backend.name, test_cases, case_scores)


def _aggregate(
    suite_name: str,
    backend_name: str,
    test_cases: list[BenchmarkCase],
    case_scores: list[CaseScore],
) -> SuiteResult:
    by_category: dict[str, CategoryResult] = {}
    by_level: dict[int, CategoryResult] = {}

    for tc, cs in zip(test_cases, case_scores):
        for key, bucket in [(tc.category, by_category), (tc.level, by_level)]:
            if key not in bucket:
                bucket[key] = CategoryResult()
            cat = bucket[key]
            cat.total += 1
            if cs.correct:
                cat.correct += 1
            if cs.provenance_correct:
                cat.provenance_correct += 1
            if cs.hallucination:
                cat.hallucinations += 1

    return SuiteResult(
        suite_name=suite_name,
        backend_name=backend_name,
        total=len(case_scores),
        correct=sum(1 for cs in case_scores if cs.correct),
        provenance_correct=sum(1 for cs in case_scores if cs.provenance_correct),
        hallucinations=sum(1 for cs in case_scores if cs.hallucination),
        errors=sum(1 for cs in case_scores if cs.error),
        by_category=by_category,
        by_level=by_level,
        case_scores=case_scores,
    )
