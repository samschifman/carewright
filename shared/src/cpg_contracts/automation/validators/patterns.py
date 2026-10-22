"""Timeout-bounded full-string matching for automation constraints."""

from __future__ import annotations

from functools import lru_cache

import regex


MAX_PATTERN_LENGTH = 256
MAX_SUBJECT_LENGTH = 4096
MATCH_TIMEOUT_SECONDS = 0.05
COMPILE_CACHE_SIZE = 128

_ERROR_MESSAGES = {
    "constraint-pattern-invalid": "constraint pattern is invalid",
    "constraint-pattern-limit": "constraint pattern exceeds a configured size limit",
    "constraint-pattern-timeout": "constraint pattern matching exceeded its time limit",
}


class PatternError(ValueError):
    """A fixed, safe error raised while validating or matching a pattern."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@lru_cache(maxsize=COMPILE_CACHE_SIZE)
def _compile_pattern(pattern: str) -> regex.Pattern:
    try:
        return regex.compile(pattern, regex.VERSION0)
    except regex.error:
        raise PatternError("constraint-pattern-invalid") from None


def _check_pattern_size(pattern: str) -> None:
    if not isinstance(pattern, str):
        raise PatternError("constraint-pattern-invalid")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError("constraint-pattern-limit")


def validate_pattern(pattern: str) -> None:
    """Compile a declared pattern without requiring a parameter default."""
    _check_pattern_size(pattern)
    _compile_pattern(pattern)


def _fullmatch(compiled: regex.Pattern, subject: str) -> regex.Match | None:
    return compiled.fullmatch(subject, timeout=MATCH_TIMEOUT_SECONDS)


def match_pattern(pattern: str, subject: str) -> bool:
    """Perform a full-string match within fixed input and time limits."""
    _check_pattern_size(pattern)
    if len(subject) > MAX_SUBJECT_LENGTH:
        raise PatternError("constraint-pattern-limit")
    compiled = _compile_pattern(pattern)
    try:
        return _fullmatch(compiled, subject) is not None
    except TimeoutError:
        raise PatternError("constraint-pattern-timeout") from None
