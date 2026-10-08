"""Versioned clinical evaluation data, separate from production contracts."""

from typing import Literal

from cpg_contracts import CONTRACT_VERSION, RecommendationBundle
from prompt_eval.models import Digest, Model, Name, Nonempty
from pydantic import Field, model_validator

from .text import tokens


class GoldenAnnotation(Model):
    recommendation_id: Nonempty
    source_quote: Nonempty
    action_aliases: tuple[Nonempty, ...] = Field(min_length=1)
    required_atoms: tuple[Nonempty, ...] = ()
    allowed_atoms: tuple[Nonempty, ...] = ()
    population_phrases: tuple[Nonempty, ...] = ()
    qualification_phrases: tuple[Nonempty, ...] = ()
    normalization_rationale: Nonempty


class EvaluationCase(Model):
    schema_version: Literal["1.0.0"]
    dataset_version: Nonempty
    case_id: Name
    corpus: Name
    split: Literal["tuning", "holdout", "validity-only"]
    source_cpg: Nonempty
    section: Name
    source_text: Nonempty
    source_document_sha256: Digest
    transformation_sha256: Digest
    items: tuple[dict, ...] = Field(min_length=1)
    grading_definitions: str
    abbreviations: dict[str, str]
    golden: RecommendationBundle
    annotations: tuple[GoldenAnnotation, ...] = Field(min_length=1)
    derivation_status: Literal["draft", "reviewed"]
    assumptions: tuple[str, ...]
    unscored_fields: tuple[str, ...]

    @model_validator(mode="after")
    def consistent(self):
        if not self.source_text.strip():
            raise ValueError("Source must not be blank")
        rows = self.golden.recommendations
        ids = [r.id for r in rows]
        item_ids = [item.get("id") for item in self.items]
        if any(not isinstance(i, str) or not i.strip() for i in item_ids):
            raise ValueError("Item IDs must be nonblank strings")
        annotation_ids = [a.recommendation_id for a in self.annotations]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("Golden IDs must be nonempty and unique")
        if len(set(item_ids)) != len(item_ids) or set(item_ids) != set(ids):
            raise ValueError("Item IDs must uniquely cover the goldens")
        if len(set(annotation_ids)) != len(annotation_ids) or set(
            annotation_ids
        ) != set(ids):
            raise ValueError("Annotations must uniquely cover the goldens")
        if self.golden.contract_version != CONTRACT_VERSION:
            raise ValueError("Unsupported recommendation contract version")
        if self.golden.source_cpg != self.source_cpg:
            raise ValueError("Golden CPG differs from case")
        for row in rows:
            if row.source_cpg != self.source_cpg or row.section != self.section:
                raise ValueError("Golden CPG/section differs from case")
            if not all(
                v.strip() for v in (row.id, row.title, row.content)
            ) or not tokens(row.content):
                raise ValueError("Golden ID, title and content must not be blank")
        if any(item.get("section") != self.section for item in self.items):
            raise ValueError("Item section differs from case")
        for annotation in self.annotations:
            if (
                not annotation.source_quote.strip()
                or annotation.source_quote not in self.source_text
            ):
                raise ValueError("Annotation quote must occur in source")
            if any(
                not v.strip()
                for v in (
                    *annotation.action_aliases,
                    *annotation.required_atoms,
                    *annotation.allowed_atoms,
                    *annotation.population_phrases,
                    *annotation.qualification_phrases,
                )
            ):
                raise ValueError("Annotation phrases must not be blank")
            if not set(annotation.required_atoms) <= set(annotation.allowed_atoms):
                raise ValueError("Required atoms must also be allowed")
            if any(atom not in self.source_text for atom in annotation.allowed_atoms):
                raise ValueError("Allowed atoms must be supported by source")
        return self
