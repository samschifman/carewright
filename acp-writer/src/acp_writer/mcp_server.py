"""MCP server exposing acp-writer tools via the Model Context Protocol."""

import json
import os

import mlflow
from mcp.server.fastmcp import FastMCP

from cpg_contracts import (
    CPGMetadata,
    Recommendation,
    RecommendationBundle,
    RecommendationSearchRequest,
)

from acp_writer.api import (
    _automation_registry,
    _automation_validation_result,
    _dynamic_models,
    _evaluate_jit,
    _guidelines_store,
    _parse_dmn_metadata,
    _vector_store,
)

mcp = FastMCP("acp-writer")

KOGITO_URL = os.environ.get("KOGITO_URL", "http://localhost:8081")


@mcp.tool()
@mlflow.trace(name="mcp_deploy_decision_model")
def deploy_decision_model(dmn_xml: str) -> str:
    """Deploy a DMN decision model to the care plan writer's internal decision engine."""
    summary = _parse_dmn_metadata(dmn_xml)
    _dynamic_models[summary.id] = {"summary": summary, "dmn_xml": dmn_xml}
    return json.dumps(summary.model_dump(mode="json"))


@mcp.tool()
@mlflow.trace(name="mcp_list_decision_models")
def list_decision_models() -> str:
    """List all decision models currently deployed."""
    models = [m["summary"].model_dump(mode="json") for m in _dynamic_models.values()]
    return json.dumps(models)


@mcp.tool()
@mlflow.trace(name="mcp_evaluate_decision")
def evaluate_decision(model_id: str, inputs: dict) -> str:
    """Evaluate a deployed DMN decision model with the given inputs."""
    model = _dynamic_models.get(model_id)
    if not model:
        return json.dumps({"error": f"Model '{model_id}' not found"})
    result = _evaluate_jit(model["dmn_xml"], inputs)
    return json.dumps(result)


@mcp.tool()
@mlflow.trace(name="mcp_generate_careplan")
def generate_careplan(ips_bundle: dict) -> str:
    """Generate a patient-specific FHIR CarePlan from a FHIR IPS Bundle.

    Runs the full LangGraph care plan composition pipeline.
    """
    from acp_writer.pipeline import build_pipeline

    from acp_writer import api

    graph = build_pipeline(
        automation_registry=_automation_registry,
        automation_validator=_automation_validation_result,
        guidelines_store=_guidelines_store,
        vector_store=_vector_store,
        decision_models_provider=lambda: [
            entry["summary"].model_dump(mode="json")
            for entry in api._dynamic_models.values()
        ],
    )
    compiled = graph.compile()

    result = compiled.invoke({
        "ips_bundle": ips_bundle,
        "litellm_url": os.environ.get("LITELLM_URL", "http://localhost:4000"),
        "llm_model": os.environ.get("LLM_MODEL", "default"),
    })

    return json.dumps(result.get("fhir_bundle", {}))


@mcp.tool()
@mlflow.trace(name="mcp_register_guideline")
def register_guideline(metadata: dict) -> str:
    """Register a CPG guideline's metadata."""
    cpg = CPGMetadata.model_validate(metadata)
    result = _guidelines_store.register(cpg)
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
@mlflow.trace(name="mcp_ingest_recommendation")
def ingest_recommendation(recommendation: dict) -> str:
    """Ingest a single recommendation into the vector store."""
    rec = Recommendation.model_validate(recommendation)
    _vector_store.add(rec)
    return json.dumps({"id": rec.id, "status": "ingested"})


@mcp.tool()
@mlflow.trace(name="mcp_ingest_recommendation_batch")
def ingest_recommendation_batch(bundle: dict) -> str:
    """Ingest a batch of recommendations from a RecommendationBundle."""
    rb = RecommendationBundle.model_validate(bundle)
    _vector_store.add_batch(rb.recommendations)
    return json.dumps({
        "source_cpg": rb.source_cpg,
        "count": len(rb.recommendations),
        "status": "ingested",
    })


@mcp.tool()
@mlflow.trace(name="mcp_search_recommendations")
def search_recommendations(query: str, top_k: int = 5, source_cpg: str | None = None) -> str:
    """Search recommendations by semantic similarity with optional filters."""
    req = RecommendationSearchRequest(query=query, top_k=top_k, source_cpg=source_cpg)
    result = _vector_store.search(req)
    return json.dumps(result.model_dump(mode="json"))
