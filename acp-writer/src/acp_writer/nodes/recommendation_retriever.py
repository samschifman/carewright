"""Recommendation Retriever — search vector store for applicable recommendations.

Queries by patient conditions and DMN outcomes, filters by
source_cpg, recommendation_type, and strength.
"""

import logging
from typing import Any

import mlflow

from cpg_contracts import RecommendationSearchRequest

from acp_writer.state import CarePlanComposerState

logger = logging.getLogger(__name__)


def _build_search_query(
    condition_codes: list[dict],
    dmn_results: list[dict],
) -> str:
    """Build a natural language search query from conditions and DMN outcomes."""
    parts = []

    for code in condition_codes:
        display = code.get("display", "")
        if display:
            parts.append(display)

    for result in dmn_results:
        outputs = result.get("outputs", {})
        for decision_name, decision_val in outputs.items():
            if isinstance(decision_val, dict):
                for field, val in decision_val.items():
                    if isinstance(val, str) and len(val) > 2:
                        parts.append(val)
            elif isinstance(decision_val, str) and len(decision_val) > 2:
                parts.append(decision_val)

    return " ".join(parts) if parts else "clinical recommendation"


@mlflow.trace(name="recommendation_retriever")
def recommendation_retriever(state: CarePlanComposerState, *, vector_store: Any) -> dict:
    """Retrieve applicable recommendations from the vector store."""
    logger.info("── Recommendation Retriever ──")
    condition_codes = state.get("condition_codes", [])
    dmn_results = state.get("dmn_results", [])
    applicable_cpgs = state.get("applicable_cpgs", [])

    if vector_store.count() == 0:
        logger.info("Vector store empty — no recommendations to retrieve")
        return {"recommendations": []}

    cpg_ids = [cpg.get("cpg_id") for cpg in applicable_cpgs if cpg.get("cpg_id")]

    query = _build_search_query(condition_codes, dmn_results)
    logger.info("Search query: %s", query[:100])

    all_recs: list[dict[str, Any]] = []

    for cpg_id in cpg_ids:
        request = RecommendationSearchRequest(
            query=query,
            top_k=20,
            source_cpg=cpg_id,
        )
        response = vector_store.search(request)
        for result in response.results:
            rec = vector_store.get(result.recommendation.id)
            if rec:
                all_recs.append(rec.model_dump(mode="json"))

    if not cpg_ids:
        request = RecommendationSearchRequest(query=query, top_k=20)
        response = vector_store.search(request)
        for result in response.results:
            rec = vector_store.get(result.recommendation.id)
            if rec:
                all_recs.append(rec.model_dump(mode="json"))

    seen_ids: set[str] = set()
    unique_recs = []
    for rec in all_recs:
        if rec["id"] not in seen_ids:
            seen_ids.add(rec["id"])
            unique_recs.append(rec)

    logger.info("Retrieved %d recommendations", len(unique_recs))
    return {"recommendations": unique_recs}
