"""LangGraph state definitions for the cpg-ingester pipeline."""

from typing import TypedDict


class CPGIngesterState(TypedDict, total=False):
    """Top-level state for the cpg-ingester pipeline."""

    # Run metadata
    run_id: str
    output_dir: str
    pdf_path: str
    litellm_url: str
    llm_model: str

    # Phase 1 outputs
    markdown: str
    source_markdown: str
    docling_json: dict
    figures: list[dict]  # figure index: id, page, bbox, class, image_ref
    figure_images: dict[str, str]  # figure id -> base64 PNG (consumed by figure_interpreter)
    section_map: list[dict]
    abbreviations: dict[str, str]
    grading_definitions: str
    archetype: str
    item_manifest: list[dict]
    cpg_metadata: dict
    classification_review_count: int
    classification_review_feedback: str

    # Phase 2 outputs
    dmn_results: list[dict]
    recommendation_results: list[dict]
    bpmn_results: list[dict]
    recommendation_escalations: list[dict]
    escalated_items: list[dict]
    assembly_report: dict
    delivery_status: dict


class DMNPipelineState(TypedDict, total=False):
    """State for the DMN creation/review subgraph."""

    item: dict
    source_pages: str
    abbreviations: dict[str, str]
    litellm_url: str
    llm_model: str
    output_dir: str
    review_feedback: str
    review_comment: str
    dmn_xml: str
    cpg_metadata: dict
    syntax_errors: list[str]
    syntax_warnings: list[str]
    semantic_discrepancies: list[str]
    engine_errors: list[str]
    engine_validation_warnings: list[str]
    syntax_retry_count: int
    semantic_retry_count: int
    escalated: bool
    escalation_reason: str
    escalation_errors: list[str]
    force_escalate: bool


class RecPipelineState(TypedDict, total=False):
    """State for the recommendation extraction/review subgraph."""

    items: list[dict]
    source_pages: str
    grading_definitions: str
    abbreviations: dict[str, str]
    litellm_url: str
    llm_model: str
    output_dir: str
    review_feedback: str
    review_comment: str
    recommendations: list[dict]
    schema_errors: list[str]
    semantic_discrepancies: list[str]
    review_count: int
    escalated: bool
    escalation_reason: str
    escalation_errors: list[str]
    force_escalate: bool


class BPMNPipelineState(TypedDict, total=False):
    """State for Process IR creation, validation, review, and preflight."""

    item: dict
    sibling_items: list[dict]
    source_pages: str
    figures: list[dict]
    abbreviations: dict[str, str]
    litellm_url: str
    llm_model: str
    output_dir: str
    cpg_metadata: dict
    dmn_summaries: dict[str, dict]
    linked_recs: list[dict]
    human_feedback: str
    process_ir: dict
    join_normalizer_fired: bool
    bpmn_xml: str
    template_summary: dict
    static_findings: list[dict]
    static_rungs_passed: list[str]
    deferred_invariants: list[str]
    static_retry_count: int
    semantic_discrepancies: list[str]
    semantic_retry_count: int
    engine_errors: list[str]
    engine_validation_warnings: list[str]
    escalated: bool
    escalation_reason: str
    escalation_errors: list[str]
    escalation_warnings: list[str]
    force_escalate: bool
