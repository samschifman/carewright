from cpg_contracts.guidelines import (
    CONTRACT_VERSION,
    CPGMetadata,
    GradingSystem,
)
from cpg_contracts.decisions import (
    COMPARATORS,
    DecisionCategory,
    DecisionEvaluationRequest,
    DecisionEvaluationResponse,
    DecisionModelSummary,
    DecisionVariable,
    Extraction,
    EXTRACTION_FUNCTIONS,
    REQUIRED_EXTRACTION_PARAMS,
    decision_model_id,
)
from cpg_contracts.recommendations import (
    CertaintyGrade,
    CrossReference,
    CrossReferenceRelationship,
    EvidenceQuality,
    Recommendation,
    RecommendationBundle,
    RecommendationProvenance,
    RecommendationStrength,
    RecommendationSummary,
    RecommendationType,
    SourceLocation,
)
from cpg_contracts.search import (
    RecommendationSearchRequest,
    RecommendationSearchResponse,
    RecommendationSearchResult,
)
from cpg_contracts.fhir import PatientSummary
from cpg_contracts.automation.instances import (
    ActivityAutomation,
    Evidence,
    ParameterBinding,
    PublicationPayload,
    PublishedAutomation,
)
from cpg_contracts.automation.templates import (
    AutomationTemplate,
    AutomationTemplateSummary,
    Automatability,
    PatternFamily,
    ValidationRecord,
)
from cpg_contracts.cloud_events import post_callback
from cpg_contracts.artifact_store import (  # noqa: F401 — lazy boto3 import
    ArtifactStore,
    get_artifact_store,
    get_phi_store,
    resolve_ref,
    store_artifact,
)
def __getattr__(name):
    if name == "get_llm":
        from cpg_contracts.llm import get_llm
        return get_llm
    if name == "content_to_text":
        from cpg_contracts.llm import content_to_text
        return content_to_text
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CONTRACT_VERSION",
    # Guidelines
    "CPGMetadata",
    "GradingSystem",
    # Decisions
    "DecisionCategory",
    "DecisionVariable",
    "Extraction",
    "EXTRACTION_FUNCTIONS",
    "COMPARATORS",
    "REQUIRED_EXTRACTION_PARAMS",
    "DecisionModelSummary",
    "decision_model_id",
    "DecisionEvaluationRequest",
    "DecisionEvaluationResponse",
    # Recommendations
    "CertaintyGrade",
    "CrossReference",
    "CrossReferenceRelationship",
    "EvidenceQuality",
    "Recommendation",
    "RecommendationBundle",
    "RecommendationProvenance",
    "RecommendationStrength",
    "RecommendationSummary",
    "RecommendationType",
    "SourceLocation",
    # Automation templates and instances
    "ActivityAutomation",
    "AutomationTemplate",
    "AutomationTemplateSummary",
    "Automatability",
    "Evidence",
    "ParameterBinding",
    "PatternFamily",
    "PublicationPayload",
    "PublishedAutomation",
    "ValidationRecord",
    # Search
    "RecommendationSearchRequest",
    "RecommendationSearchResponse",
    "RecommendationSearchResult",
    # FHIR
    "PatientSummary",
    # Cloud events
    "post_callback",
    # Artifact store
    "ArtifactStore",
    "get_artifact_store",
    "get_phi_store",
    "resolve_ref",
    "store_artifact",
    # LLM
    "get_llm",
    "content_to_text",
]
