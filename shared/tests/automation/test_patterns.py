"""Bounded pattern matching and grounding integration tests."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from cpg_contracts.automation.instances import ParameterBinding
from cpg_contracts.automation.ir import Constraints, Parameter, ProcessIR, SourceRef
from cpg_contracts.automation.validators import grounding
from cpg_contracts.automation.validators.patterns import (
    COMPILE_CACHE_SIZE,
    MAX_PATTERN_LENGTH,
    MAX_SUBJECT_LENGTH,
    PatternError,
    _compile_pattern,
    match_pattern,
    validate_pattern,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "automation"


def fixture_ir() -> ProcessIR:
    return ProcessIR.model_validate_json(
        (FIXTURES / "home-bp-monitoring.ir.json").read_text(encoding="utf-8")
    )


def test_fullmatch_preserves_match_and_nonmatch_semantics() -> None:
    assert match_pattern(r"[A-Z]{2}[0-9]+", "AB123")
    assert not match_pattern(r"[A-Z]{2}[0-9]+", "prefix-AB123")


def test_invalid_pattern_has_fixed_safe_error() -> None:
    with pytest.raises(PatternError) as raised:
        validate_pattern("(")

    assert raised.value.code == "constraint-pattern-invalid"
    assert str(raised.value) == "constraint pattern is invalid"
    assert "(" not in str(raised.value)


def test_pattern_length_boundary_is_accepted_and_one_over_is_rejected(
    monkeypatch,
) -> None:
    pattern = "a" * MAX_PATTERN_LENGTH
    assert match_pattern(pattern, pattern)

    monkeypatch.setattr(
        "cpg_contracts.automation.validators.patterns._compile_pattern",
        lambda _pattern: pytest.fail("oversized pattern must not compile"),
    )
    with pytest.raises(PatternError) as raised:
        match_pattern("a" * (MAX_PATTERN_LENGTH + 1), "")

    assert raised.value.code == "constraint-pattern-limit"


def test_subject_length_boundary_is_accepted_and_one_over_is_rejected(
    monkeypatch,
) -> None:
    assert match_pattern("a*", "a" * MAX_SUBJECT_LENGTH)

    monkeypatch.setattr(
        "cpg_contracts.automation.validators.patterns._compile_pattern",
        lambda _pattern: pytest.fail("oversized subject must not compile"),
    )
    with pytest.raises(PatternError) as raised:
        match_pattern("a*", "a" * (MAX_SUBJECT_LENGTH + 1))

    assert raised.value.code == "constraint-pattern-limit"


def test_compile_cache_is_bounded() -> None:
    assert _compile_pattern.cache_info().maxsize == COMPILE_CACHE_SIZE == 128


def test_timeout_is_converted_to_typed_safe_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "cpg_contracts.automation.validators.patterns._fullmatch",
        lambda _compiled, _subject: (_ for _ in ()).throw(TimeoutError()),
    )

    with pytest.raises(PatternError) as raised:
        match_pattern("secret-pattern", "secret-subject")

    assert raised.value.code == "constraint-pattern-timeout"
    assert "secret-pattern" not in str(raised.value)
    assert "secret-subject" not in str(raised.value)


def test_nested_quantifier_times_out_in_isolated_process() -> None:
    script = """
from cpg_contracts.automation.validators.patterns import PatternError, match_pattern
try:
    match_pattern(r"^(a|aa)+$", "a" * 4095 + "!")
except PatternError as error:
    if error.code != "constraint-pattern-timeout":
        raise SystemExit(error.code)
    print(error.code)
else:
    raise SystemExit("adversarial pattern was not bounded")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=3,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "constraint-pattern-timeout"


def test_grounding_maps_pattern_errors_to_safe_findings(monkeypatch) -> None:
    ir = fixture_ir()
    ir.process.acp.parameters.append(
        Parameter(
            name="grounding_pattern",
            type="string",
            required=True,
            default="secret-subject",
            source=SourceRef(kind="authored"),
            constraints=Constraints(pattern="secret-pattern"),
        )
    )

    def timeout(_pattern: str, _subject: str) -> bool:
        raise PatternError("constraint-pattern-timeout")

    monkeypatch.setattr(grounding, "match_pattern", timeout)
    findings = grounding.validate_template_structure(ir)
    finding = next(
        item for item in findings if item.code == "constraint-pattern-timeout"
    )

    assert finding.rung == "L5a"
    assert finding.element_id == "grounding_pattern"
    assert "secret-pattern" not in finding.message
    assert "secret-subject" not in finding.message


def test_instance_grounding_maps_pattern_errors_to_safe_findings(monkeypatch) -> None:
    ir = fixture_ir()
    ir.process.acp.parameters.append(
        Parameter(
            name="instance_pattern",
            type="string",
            required=True,
            constraints=Constraints(pattern="secret-pattern"),
        )
    )
    context = grounding.InstanceContext(
        bindings=[
            ParameterBinding(
                name="instance_pattern",
                value="secret-subject",
                source="clinician",
            )
        ]
    )

    def timeout(_pattern: str, _subject: str) -> bool:
        raise PatternError("constraint-pattern-timeout")

    monkeypatch.setattr(grounding, "match_pattern", timeout)
    findings = grounding._check_bindings_and_invariants(ir.process, context)
    finding = next(
        item for item in findings if item.code == "constraint-pattern-timeout"
    )

    assert finding.rung == "L5a"
    assert finding.element_id == "instance_pattern"
    assert "secret-pattern" not in finding.message
    assert "secret-subject" not in finding.message
