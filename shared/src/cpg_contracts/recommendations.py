"""Contract types for the recommendation boundary between cpg-ingester and acp-writer.

Recommendations are non-computable clinical content extracted from CPGs and
stored in acp-writer's vector store. Computable population applicability
criteria belong in DMN decision inputs, not here.
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel

from cpg_contracts.guidelines import CONTRACT_VERSION, GradingSystem


class RecommendationStrength(str, Enum):
    STRONG_FOR = "strong-for"
    CONDITIONAL_FOR = "conditional-for"
    CONSENSUS = "consensus"
    NO_RECOMMENDATION = "no-recommendation"
    CONDITIONAL_AGAINST = "conditional-against"
    STRONG_AGAINST = "strong-against"


class EvidenceQuality(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    VERY_LOW = "very-low"
    UNGRADED = "ungraded"


class RecommendationType(str, Enum):
    TREATMENT = "treatment"
    DIAGNOSTIC = "diagnostic"
    MONITORING = "monitoring"
    LIFESTYLE = "lifestyle"
    EDUCATIONAL = "educational"
    REFERRAL = "referral"
    SCREENING = "screening"
    CONTRAINDICATION = "contraindication"
    PROCESS = "process"


class RecommendationProvenance(str, Enum):
    REVIEWED = "reviewed"
    NEW_ADDED = "new-added"
    AMENDED = "amended"
    NOT_CHANGED = "not-changed"
    REMOVED = "removed"


class CrossReferenceRelationship(str, Enum):
    PREREQUISITE = "prerequisite"
    ALTERNATIVE = "alternative"
    CONFLICTS_WITH = "conflicts-with"
    MODIFIES = "modifies"
    RELATED = "related"
    SUPERSEDES = "supersedes"
    OTHER = "other"


class SourceLocation(BaseModel):
    """Where in the source CPG document this item was extracted from.

    Populated from Docling ProvenanceItem data during ingestion.
    """

    page_start: int
    page_end: int | None = None
    bbox: list[float] | None = None
    source_text: str | None = None


class CertaintyGrade(BaseModel):
    """Normalized certainty across grading systems.

    If grading_system is None, inherit from CPGMetadata.grading_system.
    """

    strength: RecommendationStrength
    evidence_quality: EvidenceQuality
    grading_system: GradingSystem | None = None
    original_grade: str | None = None


class CrossReference(BaseModel):
    target_id: str
    relationship: CrossReferenceRelationship
    description: str | None = None


class Recommendation(BaseModel):
    id: str
    source_cpg: str
    section: str | None = None
    title: str
    content: str
    recommendation_type: RecommendationType
    certainty: CertaintyGrade | None = None
    scope_notes: str | None = None
    remarks: list[str] | None = None
    rationale: str | None = None
    cross_references: list[CrossReference] | None = None
    provenance: RecommendationProvenance | None = None
    evidence_review_date: date | None = None
    source_location: SourceLocation | None = None
    automation_template_ids: list[str] | None = None


class RecommendationSummary(BaseModel):
    id: str
    title: str
    source_cpg: str
    recommendation_type: RecommendationType
    certainty: CertaintyGrade | None = None


class RecommendationBundle(BaseModel):
    contract_version: str = CONTRACT_VERSION
    source_cpg: str
    recommendations: list[Recommendation]
