"""Deterministic reference comparisons, with explicit counts and limitations."""

import re
from collections import Counter
from typing import Literal

from cpg_contracts import Recommendation
from prompt_eval.models import Model
from prompt_eval.security import traced
from pydantic import FiniteFloat, ValidationError

from .matching import match
from .models import EvaluationCase
from .text import contains, tokens

LIMITATIONS = (
    "Identity recall/precision are not proof of clinical correctness; see independent fidelity findings.",
    "Unmatched/ambiguous outputs are conservative extras, not automatically proven hallucinations.",
    "Lexical fidelity may penalize valid paraphrases; curated atoms cannot detect all fabricated claims.",
    "No clinical baseline or reviewed campaign corpus is supplied by unit fixtures.",
)


class PairScore(Model):
    golden_id: str
    output_index: int
    match_evidence: str
    token_precision: FiniteFloat
    token_recall: FiniteFloat
    token_f1: FiniteFloat
    missing_atoms: tuple[str, ...]
    unsupported_atoms: tuple[str, ...]
    missing_population: tuple[str, ...]
    missing_qualifications: tuple[str, ...]


class ExtractionScore(Model):
    schema_version: Literal["1.0.0"] = "1.0.0"
    expected_count: int
    produced_count: int
    accounted_count: int
    valid_contract_count: int
    matched_count: int
    missing_count: int
    extra_count: int
    duplicate_count: int
    ambiguous_count: int
    schema_error_count: int
    metrics: dict[str, FiniteFloat]
    ratios: dict[str, tuple[FiniteFloat, FiniteFloat]]
    undefined_metrics: tuple[str, ...]
    missing_ids: tuple[str, ...]
    extra_indices: tuple[int, ...]
    duplicate_indices: tuple[int, ...]
    ambiguous: dict[int, tuple[str, ...]]
    validation_errors: dict[int, list[dict]]
    details: tuple[PairScore, ...]
    manual_review_required: bool


@traced
def score(case: EvaluationCase, raw_rows: list[object]) -> ExtractionScore:
    valid, original_indices, errors = [], [], {}
    for i, row in enumerate(raw_rows):
        try:
            parsed = Recommendation.model_validate(row)
            if not all(
                v.strip() for v in (parsed.id, parsed.title, parsed.content)
            ) or not tokens(parsed.content):
                raise ValueError("ID, title and content must not be blank")
            valid.append(parsed)
            original_indices.append(i)
        except ValidationError as exc:
            errors[i] = exc.errors(
                include_url=False, include_input=False, include_context=False
            )
        except ValueError as exc:
            errors[i] = [{"type": "blank-field", "msg": str(exc)}]
    alignment = match(case, valid)
    expected, produced, matched = (
        len(case.golden.recommendations),
        len(raw_rows),
        len(alignment.pairs),
    )
    extras = tuple(sorted((*errors, *(original_indices[i] for i in alignment.extras))))
    metrics = {
        "expected_count": expected,
        "produced_count": produced,
        "valid_contract_count": len(valid),
        "matched_count": matched,
        "missed_recommendations": len(alignment.missing),
        "extra_recommendations": len(extras),
        "duplicate_count": len(alignment.duplicates),
        "ambiguous_count": len(alignment.ambiguous),
        "schema_error_count": len(errors),
    }
    ratios = {
        "recommendation_recall": (matched, expected),
        "recommendation_precision": (matched, produced),
        "miss_rate": (len(alignment.missing), expected),
        "extra_rate": (len(extras), produced),
        "duplicate_rate": (len(alignment.duplicates), produced),
        "contract_validity": (len(valid), produced),
    }
    totals = Counter()
    details = []
    annotations = {a.recommendation_id: a for a in case.annotations}
    for golden_index, output_index in alignment.pairs:
        golden, row = case.golden.recommendations[golden_index], valid[output_index]
        annotation = annotations[golden.id]
        truth, output = Counter(tokens(golden.content)), Counter(tokens(row.content))
        overlap = sum((truth & output).values())
        p, r = overlap / sum(output.values()), overlap / sum(truth.values())
        f1 = 2 * p * r / (p + r) if p + r else 0
        clinical_text = " ".join(
            [
                row.content,
                row.scope_notes or "",
                row.rationale or "",
                *(row.remarks or []),
            ]
        )
        missing_atoms = tuple(
            a for a in annotation.required_atoms if not contains(clinical_text, a)
        )
        allowed_numeric = {
            t
            for atom in annotation.allowed_atoms
            for t in tokens(atom)
            if re.search(r"\d", t)
        }
        unsupported = tuple(
            sorted(
                {t for t in tokens(clinical_text) if re.search(r"\d", t)}
                - allowed_numeric
            )
        )
        population = tuple(
            a for a in annotation.population_phrases if not contains(clinical_text, a)
        )
        qualifications = tuple(
            a
            for a in annotation.qualification_phrases
            if not contains(clinical_text, a)
        )
        totals.update(
            identifier_exactness=int(row.id == golden.id),
            type_exactness=int(row.recommendation_type == golden.recommendation_type),
            source_exactness=int(row.source_location == golden.source_location),
            cpg_section_exactness=int(
                (row.source_cpg, row.section) == (golden.source_cpg, golden.section)
            ),
            certainty_exactness=int(row.certainty == golden.certainty),
            content_precision=p,
            content_recall=r,
            content_f1=f1,
            required_atom_preservation=len(annotation.required_atoms)
            - len(missing_atoms),
            required_atom_count=len(annotation.required_atoms),
            population_preservation=len(annotation.population_phrases)
            - len(population),
            population_count=len(annotation.population_phrases),
            qualification_preservation=len(annotation.qualification_phrases)
            - len(qualifications),
            qualification_count=len(annotation.qualification_phrases),
            unsupported_atom_count=len(unsupported),
        )
        for field in (
            "strength",
            "evidence_quality",
            "grading_system",
            "original_grade",
        ):
            unequal = (row.certainty is None) != (golden.certainty is None) or (
                row.certainty is not None
                and golden.certainty is not None
                and getattr(row.certainty, field) != getattr(golden.certainty, field)
            )
            totals[f"certainty_{field}_error_count"] += int(unequal)
        details.append(
            PairScore(
                golden_id=golden.id,
                output_index=original_indices[output_index],
                match_evidence=alignment.evidence[output_index],
                token_precision=p,
                token_recall=r,
                token_f1=f1,
                missing_atoms=missing_atoms,
                unsupported_atoms=unsupported,
                missing_population=population,
                missing_qualifications=qualifications,
            )
        )
    for name in (
        "identifier_exactness",
        "type_exactness",
        "source_exactness",
        "cpg_section_exactness",
        "certainty_exactness",
        "content_precision",
        "content_recall",
        "content_f1",
    ):
        ratios[name] = (totals[name], matched)
    for name in ("required_atom", "population", "qualification"):
        ratios[f"{name}_preservation"] = (
            totals[f"{name}_preservation"],
            totals[f"{name}_count"],
        )
    metrics.update(
        {
            f"certainty_{f}_error_count": totals[f"certainty_{f}_error_count"]
            for f in (
                "strength",
                "evidence_quality",
                "grading_system",
                "original_grade",
            )
        }
    )
    metrics["unsupported_atom_count"] = totals["unsupported_atom_count"]
    undefined = tuple(name for name, (_, den) in ratios.items() if not den)
    metrics.update({name: num / den for name, (num, den) in ratios.items() if den})
    return ExtractionScore(
        expected_count=expected,
        produced_count=produced,
        accounted_count=matched + len(extras),
        valid_contract_count=len(valid),
        matched_count=matched,
        missing_count=len(alignment.missing),
        extra_count=len(extras),
        duplicate_count=len(alignment.duplicates),
        ambiguous_count=len(alignment.ambiguous),
        schema_error_count=len(errors),
        metrics=metrics,
        ratios=ratios,
        undefined_metrics=undefined,
        missing_ids=tuple(case.golden.recommendations[j].id for j in alignment.missing),
        extra_indices=extras,
        duplicate_indices=tuple(original_indices[i] for i in alignment.duplicates),
        ambiguous={
            original_indices[i]: tuple(case.golden.recommendations[j].id for j in edges)
            for i, edges in alignment.ambiguous.items()
        },
        validation_errors=errors,
        details=tuple(details),
        manual_review_required=bool(
            errors
            or extras
            or alignment.missing
            or any(
                totals[name] < matched
                for name in (
                    "identifier_exactness",
                    "type_exactness",
                    "source_exactness",
                    "certainty_exactness",
                )
            )
            or any(
                d.token_f1 < 1
                or d.missing_atoms
                or d.unsupported_atoms
                or d.missing_population
                or d.missing_qualifications
                for d in details
            )
        ),
    )
