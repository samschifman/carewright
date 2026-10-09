"""Java condition generation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cpg_contracts.automation.expressions import ExpressionError, TypeInfo, parse, to_java
from cpg_contracts.automation.ir import ProcessIR, SequenceFlow


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


def test_fixture_conditions_match_appendix_a_exactly() -> None:
    ir = fixture_ir()
    flows = {
        element.id: element
        for element in ir.process.flowElements
        if isinstance(element, SequenceFlow)
    }
    expected = {
        "f_some": "return reading_count != null && reading_count > 0;",
        "f_escalate": (
            "return reminder_attempts != null && max_reminder_attempts != null "
            "&& reminder_attempts >= max_reminder_attempts;"
        ),
        "f_high": (
            "return (avg_systolic != null && notify_systolic_threshold != null "
            "&& avg_systolic >= notify_systolic_threshold) || "
            "(avg_diastolic != null && notify_diastolic_threshold != null "
            "&& avg_diastolic >= notify_diastolic_threshold);"
        ),
    }
    for flow_id, java in expected.items():
        condition = flows[flow_id].conditionExpression
        assert condition is not None
        assert to_java(parse(condition), fixture_env()) == java


def test_prefixed_identifiers_are_emitted_verbatim() -> None:
    assert to_java(
        "confirm__reading_count >= 2",
        {"confirm__reading_count": TypeInfo("integer")},
    ) == "return confirm__reading_count != null && confirm__reading_count >= 2;"


def test_boolean_and_defined_identifiers_have_null_guards() -> None:
    env = {"enabled": TypeInfo("boolean"), "reading_count": TypeInfo("integer")}
    assert to_java("enabled", env) == (
        "return enabled != null && Boolean.TRUE.equals(enabled);"
    )
    assert to_java("enabled == true", env) == (
        "return enabled != null && Boolean.TRUE.equals(enabled);"
    )
    assert to_java("not enabled", env) == (
        "return enabled != null && !Boolean.TRUE.equals(enabled);"
    )
    assert to_java("defined(reading_count)", env) == "return reading_count != null;"
    assert to_java("not defined(reading_count)", env) == (
        "return !(reading_count != null);"
    )
    assert to_java("defined(reading_count) == true", env) == (
        "return (reading_count != null) == (true);"
    )


def test_enum_comparisons_use_string_equals() -> None:
    env = {"kind": TypeInfo("enum")}
    assert to_java("kind == 'routine'", env) == (
        'return kind != null && "routine".equals(kind);'
    )
    assert to_java("kind != 'routine'", env) == (
        'return kind != null && !("routine".equals(kind));'
    )


@pytest.mark.parametrize("source", ["P7D > P1D", "reporting_interval >= P7D"])
def test_duration_expressions_never_reach_java(source: str) -> None:
    with pytest.raises(ExpressionError, match="duration"):
        to_java(source, {"reporting_interval": TypeInfo("duration")})


def test_arithmetic_is_rejected_in_java_conditions() -> None:
    with pytest.raises(ExpressionError, match="arithmetic"):
        to_java("reading_count + 1 > 2", {"reading_count": TypeInfo("integer")})
