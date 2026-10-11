"""LangGraph state definition for the care plan composition pipeline."""

from typing import Any, TypedDict


class CarePlanComposerState(TypedDict, total=False):
    """Top-level state for the acp-writer care plan pipeline.

    Phase 1 (Clinical Reasoning) populates conditions, guidelines, DMN results,
    recommendations, and the planning brief.
    Phase 2 (FHIR Generation) produces and validates the FHIR bundle.
    """

    # Run metadata
    run_id: str
    output_dir: str
    litellm_url: str
    llm_model: str
    llm_api_key: str

    # Input
    ips_bundle: dict[str, Any]

    # Phase 1: Condition Scanner outputs
    patient_reference: str
    patient_demographics: dict[str, Any]
    condition_codes: list[dict[str, str]]
    medication_codes: list[dict[str, str]]
    allergy_codes: list[dict[str, str]]

    # Phase 1: Guideline Resolver outputs
    applicable_cpgs: list[dict[str, Any]]
    applicable_dmn_models: list[dict[str, Any]]
    dmn_dependency_graph: list[list[str]]

    # Phase 1: DMN Executor outputs
    dmn_results: list[dict[str, Any]]

    # Phase 1: Recommendation Retriever outputs
    recommendations: list[dict[str, Any]]

    # Phase 1: Plan Composer inputs (request-changes loop)
    # The prior planning brief on a request-changes recomposition. Its goals +
    # activities are the authoritative base the composer revises in place (F17a);
    # its conflicts seed the analyst's revision continuity (F17c). Absent on a
    # first pass and in the one-shot monolith path → composer authors from scratch.
    prior_planning_brief: dict[str, Any]
    # Accumulated clinician review history, oldest-first (F17b). Each entry is a
    # review round {decision, comment, reviewer, ...}; the newest is the round to
    # act on now, earlier rounds are standing context/constraints.
    careplan_review_history: list[dict[str, Any]]
    # Raw latest clinician instruction on a request-changes loop. Rendered into
    # the composer's Clinician-directed changes section every iteration (F18a);
    # the conflict analyst reads it to judge which conflicts were directed (F17c).
    careplan_feedback: str
    # F18c enforcement retry: message listing directed resolutions the previous
    # composer attempt failed to apply. Set only by the compose pipeline's
    # enforcement step; rendered inside the Clinician-directed changes section.
    directive_enforcement_note: str

    # Phase 1: Plan Composer outputs
    planning_brief: dict[str, Any]
    plan_composer_prompt: str  # rendered user prompt, captured for AI-InputPrompt (WS3)
    automation_prompts: dict[str, str]

    # Phase 1: Brief Reviewer
    brief_review_count: int
    brief_review_feedback: str

    # Phase 1: Conflict Analyst outputs
    conflict_prompt: str  # rendered user prompt, captured for AI-InputPrompt (WS3)
    # Ids of prior conflicts the clinician directed resolved that are STILL
    # detected after a revision — the F18c enforcement signal. Always set by the
    # analyst on a revision pass ([] when everything directed was applied).
    unapplied_directed_conflicts: list[str]

    # Phase 2: FHIR Bundle Generator outputs
    fhir_bundle: dict[str, Any]

    # Phase 2: Terminology Validator outputs
    terminology_issues: list[dict[str, str]]

    # Phase 2: FHIR Syntax Validator outputs
    syntax_errors: list[str]

    # Phase 2: FHIR Semantic Reviewer
    fhir_review_count: int
    fhir_review_feedback: str

    # Phase 2: FHIR Server Writer inputs/outputs
    approved: bool
    reviewer: dict[str, Any]  # approving clinician (ReviewerContext dict form)
    fhir_server_response: dict[str, Any]
    careplan_id: str
    delivery_status: str
