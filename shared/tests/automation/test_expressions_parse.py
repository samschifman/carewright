"""Parser and static type checker tests for automation expressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from cpg_contracts.automation.expressions import (
    BinaryNode,
    ExpressionError,
    IdentifierNode,
    LiteralNode,
    TypeInfo,
    UnaryNode,
    identifiers,
    literals,
    parse,
    typecheck,
)
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


def test_fixture_conditions_and_invariant_parse_and_typecheck() -> None:
    ir = fixture_ir()
    env = fixture_env()
    by_id = {
        element.id: element
        for element in ir.process.flowElements
        if isinstance(element, SequenceFlow)
    }
    condition_ids = ("f_some", "f_escalate", "f_high")
    for element_id in condition_ids:
        condition = by_id[element_id].conditionExpression
        assert condition is not None
        assert typecheck(parse(condition), env) == "boolean"

    invariant = ir.process.acp.invariants[0]
    assert typecheck(parse(invariant), env) == "boolean"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("reading_count == 2", "boolean"),
        ("reading_count != 2", "boolean"),
        ("reading_count < 2", "boolean"),
        ("reading_count <= 2", "boolean"),
        ("reading_count > 2", "boolean"),
        ("reading_count >= 2", "boolean"),
        ("true and false", "boolean"),
        ("true or false", "boolean"),
        ("not false", "boolean"),
        ("defined(reading_count)", "boolean"),
        ("reading_count + 1", "integer"),
        ("reporting_interval * 2", "duration"),
        ("2 * reporting_interval", "duration"),
        ("reporting_interval + reporting_interval", "duration"),
        ("avg_systolic >= notify_systolic_threshold", "boolean"),
        ("kind == 'reminder'", "boolean"),
    ],
)
def test_operator_and_type_table(source: str, expected: str) -> None:
    env = fixture_env()
    env["kind"] = TypeInfo("enum")
    assert typecheck(parse(source), env) == expected


@pytest.mark.parametrize("source", ["P1W", "P7D", "PT12H", "P1DT2H3M4S", "P1M"])
def test_iso_duration_literals(source: str) -> None:
    expression = parse(source)
    assert literals(expression) == [(source, "duration")]
    assert typecheck(expression, fixture_env()) == "duration"


def test_duration_literal_compares_with_duration_parameter() -> None:
    assert typecheck(parse("P1W <= reporting_interval"), fixture_env()) == "boolean"


def test_enum_string_literal_is_allowed_only_for_equality() -> None:
    env = fixture_env()
    env["kind"] = TypeInfo("enum")
    assert typecheck(parse('kind == "reminder"'), env) == "boolean"
    assert typecheck(parse("'reminder' != kind"), env) == "boolean"
    with pytest.raises(ExpressionError, match="operator '<'.*enum.*string"):
        typecheck(parse("kind < 'reminder'"), env)
    with pytest.raises(ExpressionError, match="enum-typed"):
        typecheck(parse("instructions_text == 'message'"), env)


def test_quantity_comparisons_require_matching_units() -> None:
    env = fixture_env()
    env["glucose"] = TypeInfo("quantity", "mmol/L")
    with pytest.raises(ExpressionError, match=r"quantity.*mm\[Hg\].*mmol/L"):
        typecheck(parse("avg_systolic >= glucose"), env)


@pytest.mark.parametrize(
    ("source", "fragments"),
    [
        ("reading_count + reporting_interval", ("+", "integer", "duration")),
        ("reading_count * reading_count", ("*", "integer", "integer")),
        ("reporting_interval * reporting_interval", ("*", "duration", "duration")),
        ("reading_count and true", ("and", "integer", "boolean")),
        ("not reading_count", ("not", "integer")),
        ("reading_count < true", ("<", "integer", "boolean")),
    ],
)
def test_invalid_operator_combinations_report_operator_and_types(
    source: str, fragments: tuple[str, ...]
) -> None:
    with pytest.raises(ExpressionError) as exc_info:
        typecheck(parse(source), fixture_env())
    for fragment in fragments:
        assert fragment in str(exc_info.value)


def test_unknown_identifiers_are_rejected_even_inside_defined() -> None:
    with pytest.raises(ExpressionError, match="unknown identifier 'missing_value'"):
        typecheck(parse("defined(missing_value)"), fixture_env())


def test_precedence_and_parentheses_build_expected_tree() -> None:
    expression = parse("not reading_count > 0 and true or false")
    assert isinstance(expression, BinaryNode)
    assert expression.operator == "or"
    assert isinstance(expression.left, BinaryNode)
    assert expression.left.operator == "and"
    assert isinstance(expression.left.left, UnaryNode)
    assert expression.left.left.operator == "not"
    assert isinstance(expression.left.left.operand, BinaryNode)
    assert expression.left.left.operand.operator == ">"

    grouped = parse("(true or false) and true")
    assert isinstance(grouped, BinaryNode)
    assert grouped.operator == "and"
    assert isinstance(grouped.left, BinaryNode)
    assert grouped.left.operator == "or"


def test_ast_children_identifiers_and_literals() -> None:
    expression = parse("defined(reading_count) and reporting_interval >= P1W")
    assert {node for node in identifiers(expression)} == {
        "reading_count",
        "reporting_interval",
    }
    assert literals(expression) == [("P1W", "duration")]
    assert len(expression.children()) == 2

    leaf = parse("7")
    assert isinstance(leaf, LiteralNode)
    assert leaf.children() == ()
    variable = parse("reading_count")
    assert isinstance(variable, IdentifierNode)
    assert variable.children() == ()


@pytest.mark.parametrize(
    "source",
    [
        "reading_count.value > 0",
        "Reading_count > 0",
        "_reading_count > 0",
        "reading_count >",
        "reading_count > 0 > 1",
        "@reading_count",
        "defined(reading_count, avg_systolic)",
        "defined(1)",
    ],
)
def test_invalid_syntax_is_rejected(source: str) -> None:
    with pytest.raises(ExpressionError):
        parse(source)


def test_prefixed_identifier_is_parsed_verbatim() -> None:
    expression = parse("confirm__reading_count >= 2")
    assert identifiers(expression) == {"confirm__reading_count"}
    assert typecheck(
        expression,
        {"confirm__reading_count": TypeInfo("integer")},
    ) == "boolean"


def test_type_info_requires_no_pydantic_conversion() -> None:
    assert TypeInfo(type="quantity", unit="mm[Hg]") == TypeInfo("quantity", "mm[Hg]")
