"""Evaluation, duration, and invariant semantics tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cpg_contracts.automation.expressions import (
    ExpressionError,
    TypeInfo,
    UnboundIdentifier,
    check_invariants,
    evaluate,
    parse,
    parse_duration,
)
from cpg_contracts.automation.ir import ProcessIR


FIXTURE = Path(__file__).parents[1] / "fixtures/automation/home-bp-monitoring.ir.json"


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(FIXTURE.read_text())


def fixture_env() -> dict[str, TypeInfo]:
    process = fixture_ir().process
    env = {
        prop.name: TypeInfo(type=prop.type, unit=prop.unit)
        for prop in process.properties
    }
    env.update(
        {
            parameter.name: TypeInfo(type=parameter.type, unit=parameter.unit)
            for parameter in process.acp.parameters
        }
    )
    return env


def runtime_values() -> dict[str, object]:
    return {
        "reading_count": 2,
        "avg_systolic": 140.0,
        "avg_diastolic": 80.0,
        "reminder_attempts": 3,
        "reporting_interval": timedelta(days=7),
        "max_reminder_attempts": 3,
        "max_duration": timedelta(days=28),
        "notify_systolic_threshold": 135.0,
        "notify_diastolic_threshold": 85.0,
        "kind": "reminder",
        "enabled": True,
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("5 + 2", 7),
        ("P7D * 4", timedelta(days=28)),
        ("4 * P7D", timedelta(days=28)),
        ("P7D + P7D", timedelta(days=14)),
        ("reading_count == 2", True),
        ("reading_count != 2", False),
        ("reading_count < 3", True),
        ("reading_count <= 2", True),
        ("reading_count > 1", True),
        ("reading_count >= 2", True),
        ("enabled and not false", True),
        ("enabled or false", True),
        ("avg_systolic >= notify_systolic_threshold", True),
        ("kind == 'reminder'", True),
        ("kind != 'reminder'", False),
    ],
)
def test_evaluate_operators_with_python_semantics(source: str, expected: object) -> None:
    assert evaluate(parse(source), runtime_values()) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("P7D", timedelta(days=7)),
        ("P1W", timedelta(days=7)),
        ("P2DT3H4M5S", timedelta(days=2, hours=3, minutes=4, seconds=5)),
        ("PT90M", timedelta(minutes=90)),
    ],
)
def test_parse_duration_supported_components(source: str, expected: timedelta) -> None:
    assert parse_duration(source) == expected


@pytest.mark.parametrize("source", ["P1M", "P1Y", "P1Y2D"])
def test_parse_duration_rejects_months_and_years(source: str) -> None:
    with pytest.raises(ExpressionError, match="months and years"):
        parse_duration(source)


@pytest.mark.parametrize("source", ["P", "PT", "P1WT1H", "7D", "P-1D"])
def test_parse_duration_rejects_malformed_values(source: str) -> None:
    with pytest.raises(ExpressionError):
        parse_duration(source)


def test_defined_checks_null_but_missing_identifiers_remain_unbound() -> None:
    assert evaluate(parse("defined(reading_count)"), {"reading_count": 2}) is True
    assert evaluate(parse("defined(reading_count)"), {"reading_count": None}) is False
    with pytest.raises(UnboundIdentifier) as exc_info:
        evaluate(parse("defined(reading_count)"), {})
    assert exc_info.value.name == "reading_count"


def test_unbound_identifier_has_name_and_clear_message() -> None:
    with pytest.raises(UnboundIdentifier, match="reading_count") as exc_info:
        evaluate(parse("reading_count > 0"), {})
    assert exc_info.value.name == "reading_count"


def test_fixture_invariant_is_deferred_until_max_duration_is_bound() -> None:
    invariant = fixture_ir().process.acp.invariants[0]
    values = runtime_values()
    values.pop("max_duration")
    findings, deferred = check_invariants([invariant], fixture_env(), values)
    assert findings == []
    assert deferred == [invariant]


def test_fixture_invariant_passes_when_duration_is_long_enough() -> None:
    invariant = fixture_ir().process.acp.invariants[0]
    findings, deferred = check_invariants(
        [invariant], fixture_env(), runtime_values()
    )
    assert findings == []
    assert deferred == []


def test_invariant_violation_returns_finding_with_the_expression() -> None:
    invariant = fixture_ir().process.acp.invariants[0]
    values = runtime_values()
    values["max_duration"] = timedelta(days=20)
    findings, deferred = check_invariants([invariant], fixture_env(), values)
    assert deferred == []
    assert len(findings) == 1
    assert findings[0].severity == "ERROR"
    assert invariant in findings[0].message


def test_only_invariants_with_missing_identifiers_are_deferred() -> None:
    values = runtime_values()
    values.pop("max_duration")
    findings, deferred = check_invariants(
        [
            "max_duration >= reporting_interval",
            "max_reminder_attempts < 1",
            "max_reminder_attempts >= 1",
        ],
        fixture_env(),
        values,
    )
    assert len(findings) == 1
    assert "max_reminder_attempts < 1" in findings[0].message
    assert deferred == ["max_duration >= reporting_interval"]
