"""Count every output and preserve clinically significant content differences."""

import copy

import pytest
from rec_eval_fixtures import example_case


def inputs():
    from benchmarks.recommendations.models import EvaluationCase

    data = example_case()
    return EvaluationCase.model_validate(data), copy.deepcopy(
        data["golden"]["recommendations"][0]
    )


def test_exact_and_empty_results():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    exact = score(case, [row])
    assert exact.metrics["recommendation_recall"] == 1
    assert exact.metrics["content_f1"] == 1
    assert exact.metrics["certainty_exactness"] == 1
    empty = score(case, [])
    assert empty.metrics["recommendation_recall"] == 0
    assert empty.missing_count == 1
    assert "recommendation_precision" not in empty.metrics
    assert "recommendation_precision" in empty.undefined_metrics


def test_wrong_id_requires_source_and_action():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row["id"] = "wrong"
    result = score(case, [row])
    assert result.matched_count == 1
    assert result.metrics["identifier_exactness"] == 0
    row["source_location"] = None
    assert score(case, [row]).matched_count == 0


def test_duplicate_and_ambiguous_are_accounted():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    result = score(case, [row, row, {"id": "invalid"}])
    assert result.produced_count == result.accounted_count == 3
    assert result.duplicate_count == 1
    assert result.extra_count == 3
    assert result.ambiguous_count == 2
    assert result.schema_error_count == 1
    assert result.manual_review_required


@pytest.mark.parametrize(
    "content",
    [
        "Adults should walk >=250 minutes/week unless unable to exercise.",
        "Adults should walk <=150 minutes/week unless unable to exercise.",
        "Children should walk >=150 minutes/week.",
        "Adults should not walk.",
        "Adults should take invented drug 50 mg daily.",
    ],
)
def test_same_id_does_not_make_changed_content_faithful(content):
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row["content"] = content
    result = score(case, [row])
    assert result.metrics["content_f1"] < 1
    assert result.manual_review_required
    assert (
        result.details[0].missing_atoms
        or result.details[0].unsupported_atoms
        or result.details[0].missing_qualifications
    )


def test_markdown_unicode_normalization_preserves_clinical_atoms():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row["content"] = (
        "**Adults** should walk ≥150 minutes/week unless unable to exercise."
    )
    result = score(case, [row])
    assert result.metrics["content_f1"] == 1
    assert result.metrics["required_atom_preservation"] == 1


def test_certainty_and_type_are_independent():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row["certainty"]["strength"] = "strong-for"
    row["recommendation_type"] = "treatment"
    result = score(case, [row])
    assert result.metrics["certainty_strength_error_count"] == 1
    assert result.metrics["certainty_evidence_quality_error_count"] == 0
    assert result.metrics["type_exactness"] == 0
    row["certainty"] = None
    assert score(case, [row]).metrics["certainty_exactness"] == 0


def test_positions_and_similarity_never_match():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row.update(id="other", source_location=None)
    assert score(case, [row]).matched_count == 0
    row.update(source_cpg="another-cpg")
    assert score(case, [row]).matched_count == 0


def test_reordering_rows_preserves_scores():
    from benchmarks.recommendations.models import EvaluationCase
    from benchmarks.recommendations.scoring import score

    data = example_case()
    other = copy.deepcopy(data["golden"]["recommendations"][0])
    other.update(id="diet", title="Diet", content="Adults should eat vegetables.")
    other["source_location"]["source_text"] = other["content"]
    data["source_text"] += " " + other["content"]
    data["golden"]["recommendations"].append(other)
    data["items"].append({"id": "diet", "section": "1"})
    data["annotations"].append(
        {
            "recommendation_id": "diet",
            "source_quote": other["content"],
            "action_aliases": ["eat"],
            "normalization_rationale": "Exact.",
        }
    )
    case = EvaluationCase.model_validate(data)
    rows = data["golden"]["recommendations"]
    assert score(case, rows).metrics == score(case, rows[::-1]).metrics


def test_unsupported_rationale_numbers_are_not_ignored():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row["rationale"] = "Take 75 mg daily as well."
    result = score(case, [row])
    assert "75" in result.details[0].unsupported_atoms


def test_punctuation_only_content_cannot_crash_or_pass():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row["content"] = "!!!"
    result = score(case, [row])
    assert result.schema_error_count == 1
    assert result.extra_count == 1


def test_wrong_grade_and_type_need_review():
    from benchmarks.recommendations.scoring import score

    case, row = inputs()
    row["certainty"]["strength"] = "strong-for"
    row["recommendation_type"] = "treatment"
    assert score(case, [row]).manual_review_required


def test_not_equal_comparator_is_not_normalized_to_equal():
    from benchmarks.recommendations.text import tokens

    assert tokens("!=150") != tokens("=150")


@pytest.mark.parametrize(
    "original,changed,atom",
    [
        ("Take .5 mg daily.", "Take 5 mg daily.", ".5"),
        ("Take 5 mIU daily.", "Take 5 MIU daily.", "mIU"),
        ("Use 5 mIU/mL daily.", "Use 5 MIU/mL daily.", "mIU/mL"),
    ],
)
def test_dose_and_case_sensitive_unit_changes_fail_fidelity(original, changed, atom):
    from benchmarks.recommendations.models import EvaluationCase
    from benchmarks.recommendations.scoring import score

    data = example_case()
    data["source_text"] = original
    data["golden"]["recommendations"][0]["content"] = original
    data["golden"]["recommendations"][0]["source_location"]["source_text"] = original
    data["annotations"][0].update(
        source_quote=original,
        required_atoms=[atom],
        allowed_atoms=[atom],
        population_phrases=[],
        qualification_phrases=[],
    )
    case = EvaluationCase.model_validate(data)
    row = copy.deepcopy(data["golden"]["recommendations"][0])
    row["content"] = changed
    result = score(case, [row])
    assert result.metrics["content_f1"] < 1
    assert result.metrics["required_atom_preservation"] == 0
    assert result.manual_review_required
