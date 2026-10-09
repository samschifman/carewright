"""Parser and static type checker for the automation expression language."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import timedelta
import json
import re
from typing import Any, TypeAlias

from cpg_contracts.automation._tracing import trace
from cpg_contracts.automation.validators.results import Finding


class ExpressionError(ValueError):
    """Raised when an expression is malformed or cannot be typed."""


class UnboundIdentifier(ExpressionError):
    """Raised when evaluation references a name absent from the runtime values."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unbound identifier {name!r}")


class Expr:
    """Base class for immutable expression syntax nodes."""

    def children(self) -> tuple[Expr, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class LiteralNode(Expr):
    value: bool | int | float | str
    literal_type: str


@dataclass(frozen=True, slots=True)
class IdentifierNode(Expr):
    name: str


@dataclass(frozen=True, slots=True)
class DefinedNode(Expr):
    identifier: IdentifierNode

    def children(self) -> tuple[Expr, ...]:
        return (self.identifier,)


@dataclass(frozen=True, slots=True)
class UnaryNode(Expr):
    operator: str
    operand: Expr

    def children(self) -> tuple[Expr, ...]:
        return (self.operand,)


@dataclass(frozen=True, slots=True)
class BinaryNode(Expr):
    operator: str
    left: Expr
    right: Expr

    def children(self) -> tuple[Expr, ...]:
        return (self.left, self.right)


Expression: TypeAlias = Expr


@dataclass(frozen=True, slots=True)
class TypeInfo:
    type: str
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str
    position: int


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_STRING_RE = re.compile(r'''(?:"(?:\\.|[^"\\\n\r])*"|'(?:\\.|[^'\\\n\r])*')''')
_DURATION_NUMBER = r"\d+(?:\.\d+)?"
_DURATION_TIME = (
    rf"T(?:{_DURATION_NUMBER}H(?:{_DURATION_NUMBER}M)?(?:{_DURATION_NUMBER}S)?"
    rf"|{_DURATION_NUMBER}M(?:{_DURATION_NUMBER}S)?|{_DURATION_NUMBER}S)"
)
_DURATION_DATE = (
    rf"(?:{_DURATION_NUMBER}Y(?:{_DURATION_NUMBER}M)?(?:{_DURATION_NUMBER}D)?"
    rf"|{_DURATION_NUMBER}M(?:{_DURATION_NUMBER}D)?|{_DURATION_NUMBER}D)"
)
_DURATION_RE = re.compile(
    rf"P(?:{_DURATION_NUMBER}W|{_DURATION_DATE}(?:{_DURATION_TIME})?|{_DURATION_TIME})"
)
_DURATION_VALUE_RE = re.compile(
    r"^P(?:(?P<weeks>\d+(?:\.\d+)?)W|"
    r"(?:(?P<years>\d+(?:\.\d+)?)Y)?"
    r"(?:(?P<months>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?)$"
)
_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]*")
_COMPARISON_OPERATORS = frozenset({"==", "!=", "<", "<=", ">", ">="})


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(source):
        if source[position].isspace():
            position += 1
            continue

        duration_match = _DURATION_RE.match(source, position)
        if duration_match is not None:
            value = duration_match.group(0)
            tokens.append(_Token("duration", value, position))
            position = duration_match.end()
            continue

        string_match = _STRING_RE.match(source, position)
        if string_match is not None:
            value = string_match.group(0)
            tokens.append(_Token("string", value, position))
            position = string_match.end()
            continue

        number_match = _NUMBER_RE.match(source, position)
        if number_match is not None:
            value = number_match.group(0)
            tokens.append(_Token("number", value, position))
            position = number_match.end()
            continue

        identifier_match = _IDENTIFIER_RE.match(source, position)
        if identifier_match is not None:
            value = identifier_match.group(0)
            tokens.append(_Token("identifier", value, position))
            position = identifier_match.end()
            continue

        operator = next(
            (
                candidate
                for candidate in ("==", "!=", "<=", ">=", "<", ">", "+", "*", "(", ")")
                if source.startswith(candidate, position)
            ),
            None,
        )
        if operator is not None:
            tokens.append(_Token("operator", operator, position))
            position += len(operator)
            continue

        raise ExpressionError(
            f"unexpected character {source[position]!r} at position {position}"
        )

    tokens.append(_Token("eof", "<end>", len(source)))
    return tokens


class _Parser:
    def __init__(self, source: str) -> None:
        self.tokens = _tokenize(source)
        self.index = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def _advance(self) -> _Token:
        token = self.current
        if token.kind != "eof":
            self.index += 1
        return token

    def _accept_value(self, value: str) -> bool:
        if self.current.value == value:
            self._advance()
            return True
        return False

    def _expect_value(self, value: str) -> _Token:
        if self.current.value != value:
            raise ExpressionError(
                f"expected {value!r} at position {self.current.position}, "
                f"found {self.current.value!r}"
            )
        return self._advance()

    def parse(self) -> Expr:
        if self.current.kind == "eof":
            raise ExpressionError("expression cannot be empty")
        expression = self._parse_or()
        if self.current.kind != "eof":
            raise ExpressionError(
                f"unexpected token {self.current.value!r} at position {self.current.position}"
            )
        return expression

    def _parse_or(self) -> Expr:
        expression = self._parse_and()
        while self._accept_value("or"):
            expression = BinaryNode("or", expression, self._parse_and())
        return expression

    def _parse_and(self) -> Expr:
        expression = self._parse_not()
        while self._accept_value("and"):
            expression = BinaryNode("and", expression, self._parse_not())
        return expression

    def _parse_not(self) -> Expr:
        if self._accept_value("not"):
            return UnaryNode("not", self._parse_not())
        return self._parse_comparison()

    def _parse_comparison(self) -> Expr:
        expression = self._parse_additive()
        if self.current.value in _COMPARISON_OPERATORS:
            operator = self._advance().value
            expression = BinaryNode(operator, expression, self._parse_additive())
            if self.current.value in _COMPARISON_OPERATORS:
                raise ExpressionError(
                    f"chained comparisons are not supported at position {self.current.position}"
                )
        return expression

    def _parse_additive(self) -> Expr:
        expression = self._parse_multiplicative()
        while self._accept_value("+"):
            expression = BinaryNode("+", expression, self._parse_multiplicative())
        return expression

    def _parse_multiplicative(self) -> Expr:
        expression = self._parse_primary()
        while self._accept_value("*"):
            expression = BinaryNode("*", expression, self._parse_primary())
        return expression

    def _parse_primary(self) -> Expr:
        token = self.current
        if self._accept_value("("):
            expression = self._parse_or()
            self._expect_value(")")
            return expression

        if token.kind == "duration":
            self._advance()
            return LiteralNode(token.value, "duration")

        if token.kind == "string":
            self._advance()
            try:
                value = ast.literal_eval(token.value)
            except (SyntaxError, ValueError) as exc:
                raise ExpressionError(
                    f"invalid string literal at position {token.position}"
                ) from exc
            return LiteralNode(value, "string")

        if token.kind == "number":
            self._advance()
            if any(char in token.value for char in ".eE"):
                return LiteralNode(float(token.value), "decimal")
            return LiteralNode(int(token.value), "integer")

        if token.kind == "identifier":
            self._advance()
            if token.value == "true":
                return LiteralNode(True, "boolean")
            if token.value == "false":
                return LiteralNode(False, "boolean")
            if token.value == "defined":
                self._expect_value("(")
                if self.current.kind != "identifier" or self.current.value in {
                    "and",
                    "or",
                    "not",
                    "true",
                    "false",
                    "defined",
                }:
                    raise ExpressionError(
                        f"defined() requires an identifier at position {self.current.position}"
                    )
                identifier = IdentifierNode(self._advance().value)
                self._expect_value(")")
                return DefinedNode(identifier)
            if token.value in {"and", "or", "not"}:
                raise ExpressionError(
                    f"operator {token.value!r} has no left operand at position {token.position}"
                )
            return IdentifierNode(token.value)

        raise ExpressionError(
            f"expected a literal, identifier, or parenthesized expression at position {token.position}"
        )


def _as_expression(expression: Expr | str) -> Expr:
    if isinstance(expression, str):
        return parse(expression)
    if not isinstance(expression, Expr):
        raise ExpressionError("expression must be source text or an expression node")
    return expression


@trace
def parse(source: str) -> Expr:
    """Parse one expression into an immutable syntax tree."""
    if not isinstance(source, str):
        raise ExpressionError("expression source must be a string")
    return _Parser(source).parse()


@trace
def identifiers(expression: Expr | str) -> set[str]:
    """Return the distinct identifiers referenced by an expression."""
    root = _as_expression(expression)
    found: set[str] = set()
    pending = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, IdentifierNode):
            found.add(node.name)
        pending.extend(reversed(node.children()))
    return found


@trace
def literals(expression: Expr | str) -> list[tuple[bool | int | float | str, str]]:
    """Return expression literals in left-to-right source order with their types."""
    root = _as_expression(expression)
    found: list[tuple[bool | int | float | str, str]] = []
    pending = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, LiteralNode):
            found.append((node.value, node.literal_type))
        pending.extend(reversed(node.children()))
    return found


def _operator_error(operator: str, left: TypeInfo, right: TypeInfo) -> ExpressionError:
    return ExpressionError(
        f"operator {operator!r} does not support operand types "
        f"{left.type!r} and {right.type!r}"
    )


def _infer(expression: Expr, env: dict[str, TypeInfo]) -> TypeInfo:
    if isinstance(expression, LiteralNode):
        if expression.literal_type == "string":
            raise ExpressionError(
                "quoted strings are allowed only in == or != against an enum-typed operand"
            )
        return TypeInfo(expression.literal_type)

    if isinstance(expression, IdentifierNode):
        try:
            return env[expression.name]
        except KeyError as exc:
            raise ExpressionError(f"unknown identifier {expression.name!r}") from exc

    if isinstance(expression, DefinedNode):
        if expression.identifier.name not in env:
            raise ExpressionError(
                f"unknown identifier {expression.identifier.name!r} in defined()"
            )
        return TypeInfo("boolean")

    if isinstance(expression, UnaryNode):
        operand = _infer(expression.operand, env)
        if expression.operator == "not" and operand.type == "boolean":
            return TypeInfo("boolean")
        raise ExpressionError(
            f"operator {expression.operator!r} requires a boolean operand, got {operand.type!r}"
        )

    if isinstance(expression, BinaryNode):
        operator = expression.operator
        if operator in {"and", "or"}:
            left = _infer(expression.left, env)
            right = _infer(expression.right, env)
            if left.type == right.type == "boolean":
                return TypeInfo("boolean")
            raise _operator_error(operator, left, right)

        if operator in {"+", "*"}:
            left = _infer(expression.left, env)
            right = _infer(expression.right, env)
            if operator == "+" and left.type == right.type == "integer":
                return TypeInfo("integer")
            if operator == "+" and left.type == right.type == "duration":
                return TypeInfo("duration")
            if operator == "*" and (left.type, right.type) in {
                ("integer", "duration"),
                ("duration", "integer"),
            }:
                return TypeInfo("duration")
            raise _operator_error(operator, left, right)

        if operator in _COMPARISON_OPERATORS:
            left_is_string_literal = (
                isinstance(expression.left, LiteralNode)
                and expression.left.literal_type == "string"
            )
            right_is_string_literal = (
                isinstance(expression.right, LiteralNode)
                and expression.right.literal_type == "string"
            )
            left = (
                TypeInfo("string")
                if left_is_string_literal
                else _infer(expression.left, env)
            )
            right = (
                TypeInfo("string")
                if right_is_string_literal
                else _infer(expression.right, env)
            )
            if left_is_string_literal or right_is_string_literal:
                enum_comparison = (
                    left_is_string_literal and right.type == "enum"
                ) or (right_is_string_literal and left.type == "enum")
                if operator not in {"==", "!="} or not enum_comparison:
                    raise ExpressionError(
                        "quoted strings require == or != against an enum-typed operand; "
                        f"operator {operator!r} has types {left.type!r} and {right.type!r}"
                    )
                return TypeInfo("boolean")
            if left.type != right.type:
                raise _operator_error(operator, left, right)
            if left.type == "quantity" and (
                left.unit is None or right.unit is None or left.unit != right.unit
            ):
                raise ExpressionError(
                    f"operator {operator!r} requires matching declared quantity units; "
                    f"got {left.unit!r} and {right.unit!r}"
                )
            return TypeInfo("boolean")

        raise ExpressionError(f"unsupported operator {operator!r}")

    raise ExpressionError(f"unsupported expression node {type(expression).__name__}")


@trace
def typecheck(expression: Expr | str, env: dict[str, TypeInfo]) -> str:
    """Return the expression type or raise with the offending operator and types."""
    return _infer(_as_expression(expression), env).type


@trace
def parse_duration(iso: str) -> timedelta:
    """Convert supported ISO-8601 day/time durations to ``timedelta``."""
    if not isinstance(iso, str):
        raise ExpressionError("duration must be an ISO-8601 string")
    match = _DURATION_VALUE_RE.fullmatch(iso)
    if match is None:
        raise ExpressionError(f"invalid ISO-8601 duration {iso!r}")
    parts = match.groupdict()
    if parts["years"] is not None or parts["months"] is not None:
        raise ExpressionError("duration months and years are not supported")
    numeric_parts = [value for value in parts.values() if value is not None]
    if not numeric_parts:
        raise ExpressionError(f"duration {iso!r} must include a component")
    if "T" in iso and not any(
        parts[name] is not None for name in ("hours", "minutes", "seconds")
    ):
        raise ExpressionError(f"duration {iso!r} has an empty time component")
    try:
        return timedelta(
            weeks=float(parts["weeks"] or 0),
            days=float(parts["days"] or 0),
            hours=float(parts["hours"] or 0),
            minutes=float(parts["minutes"] or 0),
            seconds=float(parts["seconds"] or 0),
        )
    except (OverflowError, ValueError) as exc:
        raise ExpressionError(f"duration {iso!r} is outside the supported range") from exc


def _evaluate(expression: Expr, values: dict[str, Any]) -> Any:
    if isinstance(expression, LiteralNode):
        if expression.literal_type == "duration":
            return parse_duration(str(expression.value))
        return expression.value

    if isinstance(expression, IdentifierNode):
        if expression.name not in values:
            raise UnboundIdentifier(expression.name)
        return values[expression.name]

    if isinstance(expression, DefinedNode):
        name = expression.identifier.name
        if name not in values:
            raise UnboundIdentifier(name)
        return values[name] is not None

    if isinstance(expression, UnaryNode):
        if expression.operator == "not":
            return not _evaluate(expression.operand, values)
        raise ExpressionError(f"unsupported unary operator {expression.operator!r}")

    if isinstance(expression, BinaryNode):
        if expression.operator == "and":
            return _evaluate(expression.left, values) and _evaluate(expression.right, values)
        if expression.operator == "or":
            return _evaluate(expression.left, values) or _evaluate(expression.right, values)

        left = _evaluate(expression.left, values)
        right = _evaluate(expression.right, values)
        if expression.operator == "+":
            return left + right
        if expression.operator == "*":
            return left * right
        comparisons = {
            "==": lambda: left == right,
            "!=": lambda: left != right,
            "<": lambda: left < right,
            "<=": lambda: left <= right,
            ">": lambda: left > right,
            ">=": lambda: left >= right,
        }
        try:
            return comparisons[expression.operator]()
        except KeyError as exc:
            raise ExpressionError(f"unsupported binary operator {expression.operator!r}") from exc

    raise ExpressionError(f"unsupported expression node {type(expression).__name__}")


@trace
def evaluate(expression: Expr | str, values: dict[str, Any]) -> Any:
    """Evaluate an expression with Python operator semantics."""
    return _evaluate(_as_expression(expression), values)


@trace
def check_invariants(
    invariants: list[str], env: dict[str, TypeInfo], values: dict[str, Any]
) -> tuple[list[Finding], list[str]]:
    """Return violated invariant findings and expressions deferred by missing values."""
    findings: list[Finding] = []
    deferred: list[str] = []
    for invariant in invariants:
        expression = parse(invariant)
        result_type = typecheck(expression, env)
        if result_type != "boolean":
            raise ExpressionError(
                f"invariant must have boolean type, got {result_type!r}: {invariant}"
            )
        if identifiers(expression).difference(values):
            deferred.append(invariant)
            continue
        if not evaluate(expression, values):
            findings.append(
                Finding(
                    rung="L0",
                    severity="ERROR",
                    code="invariant-violation",
                    message=f"Invariant violated: {invariant}",
                )
            )
    return findings, deferred


def _required_values(expression: Expr) -> list[str]:
    names: list[str] = []

    def visit(node: Expr) -> None:
        if isinstance(node, IdentifierNode):
            names.append(node.name)
        elif isinstance(node, DefinedNode):
            # defined() is the explicit null check; guarding its identifier
            # would make `not defined(x)` false for both null and non-null x.
            return
        else:
            for child in node.children():
                visit(child)

    visit(expression)
    return list(dict.fromkeys(names))


def _java_literal(value: bool | int | float | str, literal_type: str) -> str:
    if literal_type == "string":
        raise ExpressionError("string literals cannot be emitted outside enum comparisons")
    if literal_type == "duration":
        raise ExpressionError("duration values cannot be emitted into Java conditions")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    raise ExpressionError(f"unsupported Java literal type {literal_type!r}")


def _java_operand(expression: Expr, env: dict[str, TypeInfo]) -> str:
    if isinstance(expression, IdentifierNode):
        if expression.name not in env:
            raise ExpressionError(f"unknown identifier {expression.name!r}")
        return expression.name
    if isinstance(expression, LiteralNode):
        if expression.literal_type == "string":
            raise ExpressionError("string literals are emitted through enum equality")
        return _java_literal(expression.value, expression.literal_type)
    raise ExpressionError("Java comparisons accept only identifiers and literals")


def _comparison_operand_type(expression: Expr, env: dict[str, TypeInfo]) -> TypeInfo:
    if isinstance(expression, LiteralNode) and expression.literal_type == "string":
        return TypeInfo("string")
    return _infer(expression, env)


def _comparison_java(expression: BinaryNode, env: dict[str, TypeInfo]) -> str:
    operator = expression.operator
    left = expression.left
    right = expression.right
    left_type = _comparison_operand_type(left, env)
    right_type = _comparison_operand_type(right, env)

    left_string = isinstance(left, LiteralNode) and left.literal_type == "string"
    right_string = isinstance(right, LiteralNode) and right.literal_type == "string"
    if left_string or right_string:
        if left_string:
            literal = json.dumps(left.value, ensure_ascii=False)
            enum_value = _java_operand(right, env)
        else:
            literal = json.dumps(right.value, ensure_ascii=False)
            enum_value = _java_operand(left, env)
        comparison = f"{literal}.equals({enum_value})"
        return comparison if operator == "==" else f"!({comparison})"

    if left_type.type == "boolean":
        if operator not in {"==", "!="}:
            raise ExpressionError(f"Java does not support boolean ordering with {operator!r}")
        if isinstance(left, IdentifierNode) and isinstance(right, LiteralNode):
            equals_true = f"Boolean.TRUE.equals({left.name})"
            want_true = (operator == "==") == bool(right.value)
            return equals_true if want_true else f"!{equals_true}"
        if isinstance(right, IdentifierNode) and isinstance(left, LiteralNode):
            equals_true = f"Boolean.TRUE.equals({right.name})"
            want_true = (operator == "==") == bool(left.value)
            return equals_true if want_true else f"!{equals_true}"
        if isinstance(left, IdentifierNode) and isinstance(right, IdentifierNode):
            return (
                f"Boolean.TRUE.equals({left.name}) {operator} "
                f"Boolean.TRUE.equals({right.name})"
            )
        left_boolean = _java_raw(left, env)
        right_boolean = _java_raw(right, env)
        return f"({left_boolean}) {operator} ({right_boolean})"

    left_value = _java_operand(left, env)
    right_value = _java_operand(right, env)
    if left_type.type in {"string", "enum", "code"}:
        if operator not in {"==", "!="}:
            raise ExpressionError(
                f"Java does not support ordered comparison for {left_type.type!r}"
            )
        comparison = f"{left_value}.equals({right_value})"
        return comparison if operator == "==" else f"!({comparison})"

    return f"{left_value} {operator} {right_value}"


def _java_raw(expression: Expr, env: dict[str, TypeInfo]) -> str:
    if isinstance(expression, LiteralNode):
        return _java_literal(expression.value, expression.literal_type)
    if isinstance(expression, IdentifierNode):
        type_info = env.get(expression.name)
        if type_info is None:
            raise ExpressionError(f"unknown identifier {expression.name!r}")
        if type_info.type != "boolean":
            raise ExpressionError(
                f"boolean condition identifier {expression.name!r} has type {type_info.type!r}"
            )
        return f"Boolean.TRUE.equals({expression.name})"
    if isinstance(expression, DefinedNode):
        return f"{expression.identifier.name} != null"
    if isinstance(expression, UnaryNode):
        operand = _java_raw(expression.operand, env)
        if isinstance(expression.operand, (IdentifierNode, LiteralNode)):
            return f"!{operand}"
        return f"!({operand})"
    if isinstance(expression, BinaryNode):
        if expression.operator in {"and", "or"}:
            joiner = "&&" if expression.operator == "and" else "||"
            return (
                f"({_java_raw(expression.left, env)}) {joiner} "
                f"({_java_raw(expression.right, env)})"
            )
        if expression.operator in _COMPARISON_OPERATORS:
            return _comparison_java(expression, env)
        raise ExpressionError("arithmetic is not supported in Java conditions")
    raise ExpressionError(f"unsupported expression node {type(expression).__name__}")


def _flatten_boolean(expression: Expr, operator: str) -> list[Expr]:
    if isinstance(expression, BinaryNode) and expression.operator == operator:
        return _flatten_boolean(expression.left, operator) + _flatten_boolean(
            expression.right, operator
        )
    return [expression]


def _java_boolean(expression: Expr, env: dict[str, TypeInfo]) -> str:
    if isinstance(expression, BinaryNode) and expression.operator == "or":
        left = _java_boolean(expression.left, env)
        right = _java_boolean(expression.right, env)
        return f"({left}) || ({right})"

    if isinstance(expression, BinaryNode) and expression.operator == "and":
        terms = _flatten_boolean(expression, "and")
        guard_names: list[str] = []
        defined_terms: list[DefinedNode] = []
        predicates: list[str] = []
        for term in terms:
            if isinstance(term, DefinedNode):
                defined_terms.append(term)
            elif isinstance(term, BinaryNode) and term.operator == "or":
                predicates.append(f"({_java_boolean(term, env)})")
            else:
                guard_names.extend(_required_values(term))
                predicates.append(_java_raw(term, env))
        unique_guards = list(dict.fromkeys(guard_names))
        guarded_set = set(unique_guards)
        for term in defined_terms:
            if term.identifier.name not in guarded_set:
                predicates.append(_java_raw(term, env))
        parts = [f"{name} != null" for name in unique_guards] + predicates
        return " && ".join(parts)

    guards = _required_values(expression)
    raw = _java_raw(expression, env)
    return " && ".join([*(f"{name} != null" for name in guards), raw])


@trace
def to_java(expression: Expr | str, env: dict[str, TypeInfo]) -> str:
    """Compile a boolean condition to a null-safe Java ``return`` statement."""
    root = _as_expression(expression)
    result_type = typecheck(root, env)
    if result_type != "boolean":
        raise ExpressionError(f"Java condition must be boolean, got {result_type!r}")

    pending = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, LiteralNode) and node.literal_type == "duration":
            raise ExpressionError("duration values cannot reach Java conditions")
        if isinstance(node, IdentifierNode) and env[node.name].type == "duration":
            raise ExpressionError("duration values cannot reach Java conditions")
        if isinstance(node, BinaryNode) and node.operator in {"+", "*"}:
            raise ExpressionError("arithmetic is not supported in Java conditions")
        pending.extend(node.children())

    return f"return {_java_boolean(root, env)};"
