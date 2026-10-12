"""Standalone MCP server for acp-writer — serves tools over Streamable HTTP.

Each tool either manages lightweight in-memory state (guidelines, decision models,
recommendations) or proxies to pod-split REST services for heavy operations
(decision evaluation, care plan generation).

Deployed as its own pod behind the MCP Gateway for governed tool access.
"""

import base64
import json
import logging
import os

import mlflow
import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from cpg_contracts import (
    CPGMetadata,
    DecisionModelSummary,
    Recommendation,
    RecommendationBundle,
    RecommendationSearchRequest,
)
from cpg_contracts.decisions import normalize_decision_model_summary
from acp_writer.store.embedding import FakeEmbeddingProvider
from acp_writer.store.guidelines_store import GuidelinesStore
from acp_writer.store.vector_store import InMemoryVectorStore

logger = logging.getLogger(__name__)

KOGITO_URL = os.environ.get("KOGITO_URL", "http://localhost:8081")
FHIR_SERVER_URL = os.environ.get("FHIR_SERVER_URL", "http://acp-fhir-server:8080")

_embedding_provider = FakeEmbeddingProvider(dimensions=8)
_vector_store = InMemoryVectorStore(_embedding_provider)
_guidelines_store = GuidelinesStore(_vector_store)
_dynamic_models: dict[str, dict] = {}

mcp = FastMCP(
    "acp-writer",
    host="0.0.0.0",
    port=8090,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _parse_dmn_metadata(dmn_xml: str) -> DecisionModelSummary:
    from acp_writer.api import _parse_dmn_metadata as _parse
    return _parse(dmn_xml)


@mcp.tool()
@mlflow.trace(name="mcp_deploy_decision_model")
def deploy_decision_model(dmn_xml: str) -> str:
    """Deploy a DMN decision model to the care plan writer's internal decision engine."""
    summary = normalize_decision_model_summary(_parse_dmn_metadata(dmn_xml))
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
    """Evaluate a deployed DMN decision model with the given inputs.

    Proxies to the Kogito decision engine via REST.
    """
    model = _dynamic_models.get(model_id)
    if not model:
        return json.dumps({"error": f"Model '{model_id}' not found"})
    dmn_b64 = base64.b64encode(model["dmn_xml"].encode()).decode()
    try:
        r = requests.post(
            f"{KOGITO_URL}/jit/dmn",
            json={"dmn_xml_base64": dmn_b64, "inputs": inputs},
            timeout=30,
        )
        r.raise_for_status()
        return json.dumps(r.json())
    except requests.RequestException as e:
        return json.dumps({"error": f"Decision evaluation failed: {e}"})


@mcp.tool()
@mlflow.trace(name="mcp_generate_careplan")
def generate_careplan(ips_bundle: dict) -> str:
    """Generate a patient-specific FHIR CarePlan from a FHIR IPS Bundle.

    Proxies to the acp-writer FHIR Server pod via REST.
    """
    try:
        r = requests.post(
            f"{FHIR_SERVER_URL}/api/v1/write",
            json={"fhir_bundle": ips_bundle},
            timeout=120,
        )
        r.raise_for_status()
        return json.dumps(r.json())
    except requests.RequestException as e:
        return json.dumps({"error": f"Care plan generation failed: {e}"})


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
    """Ingest a single recommendation into the knowledge base."""
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


# --- Starlette app: MCP server with health endpoint ---

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send


class AcceptHeaderMiddleware:
    """Ensure MCP requests include text/event-stream in the Accept header.

    The MCP Gateway (Kuadrant v0.6.0) sends Accept: application/json without
    text/event-stream, but the MCP SDK's Streamable HTTP transport requires both.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            accept = headers.get(b"accept", b"").decode()
            if "text/event-stream" not in accept:
                new_accept = f"{accept}, text/event-stream" if accept else "application/json, text/event-stream"
                scope["headers"] = [
                    (k, v) for k, v in scope["headers"] if k != b"accept"
                ] + [(b"accept", new_accept.encode())]
        await self.app(scope, receive, send)


_mcp_app = mcp.streamable_http_app()


async def health(request):
    return JSONResponse({"status": "UP", "service": "acp-writer-mcp"})


app = Starlette(
    routes=[Route("/health", health)],
    lifespan=_mcp_app.router.lifespan_context,
    middleware=[Middleware(AcceptHeaderMiddleware)],
)
app.mount("/", _mcp_app)
