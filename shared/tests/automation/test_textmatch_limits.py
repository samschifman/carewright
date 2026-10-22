"""Deterministic work-bound checks for text grounding."""

from __future__ import annotations

from cpg_contracts.automation.validators import textmatch


class _CountingSequenceMatcher:
    calls = 0
    result = 0.0

    def __init__(self, *_args, **_kwargs) -> None:
        type(self).calls += 1

    def ratio(self) -> float:
        return type(self).result


def _install_counting_matcher(monkeypatch, result: float = 1.0) -> None:
    _CountingSequenceMatcher.calls = 0
    _CountingSequenceMatcher.result = result
    monkeypatch.setattr(textmatch, "SequenceMatcher", _CountingSequenceMatcher)


def test_citation_above_token_limit_skips_fuzzy_matching(monkeypatch) -> None:
    _install_counting_matcher(monkeypatch)
    citation = "citation " * (textmatch.MAX_FUZZY_CITATION_TOKENS + 1)
    section = "unrelated " + "citation " * textmatch.MAX_FUZZY_CITATION_TOKENS

    assert not textmatch.contains(section, citation)
    assert _CountingSequenceMatcher.calls == 0


def test_section_above_token_limit_skips_fuzzy_matching(monkeypatch) -> None:
    _install_counting_matcher(monkeypatch)
    section = "unrelated " * (textmatch.MAX_FUZZY_SECTION_TOKENS + 1)

    assert not textmatch.contains(section, "a citation that does not occur")
    assert _CountingSequenceMatcher.calls == 0


def test_proposed_comparisons_above_budget_skip_fuzzy_matching(monkeypatch) -> None:
    _install_counting_matcher(monkeypatch)
    citation = "citation " * textmatch.MAX_FUZZY_CITATION_TOKENS
    section = "unrelated " * textmatch.MAX_FUZZY_SECTION_TOKENS

    assert not textmatch.contains(section, citation)
    assert _CountingSequenceMatcher.calls == 0


def test_fuzzy_comparisons_within_budget_are_bounded(monkeypatch) -> None:
    _install_counting_matcher(monkeypatch, result=0.0)
    citation_tokens = ["citation"] * 20
    section_tokens = ["unrelated"] * 1000
    tolerance = max(1, round(len(citation_tokens) * (1 - 0.9)))
    min_width = max(1, len(citation_tokens) - tolerance)
    max_width = min(len(section_tokens), len(citation_tokens) + tolerance)
    expected_comparisons = sum(
        len(section_tokens) - width + 1
        for width in range(min_width, max_width + 1)
    )

    assert expected_comparisons <= textmatch.MAX_FUZZY_WINDOW_COMPARISONS
    assert not textmatch.contains(
        " ".join(section_tokens), " ".join(citation_tokens)
    )
    assert _CountingSequenceMatcher.calls == expected_comparisons
    assert _CountingSequenceMatcher.calls <= textmatch.MAX_FUZZY_WINDOW_COMPARISONS


def test_matching_does_not_mutate_abbreviations() -> None:
    abbreviations = {"BP": "blood pressure"}
    original = abbreviations.copy()

    assert textmatch.contains(
        "Measure BP every day.",
        "Measure blood pressure every day.",
        abbreviations=abbreviations,
    )
    assert abbreviations == original
