"""Decision Engine pod service — thin Kogito wrapper.

Model management (deploy, list) and DMN evaluation with pre-resolved inputs.
The DMN engine is an implementation detail behind the /api/v1/evaluate contract;
swapping Kogito for another evaluator should not affect callers.

Security profile: Kogito runtime + MinIO only (no LLM, no MaaS).
"""

import logging

import mlflow
from acp_writer.api import (
    _automation_dmn_namespaces,
    _automation_dmn_summaries,
    _automation_registry,
    _automation_validation_result,
    _dynamic_models,
    _evaluate_jit,
    _parse_dmn_metadata,
    _validate_dmn_with_engine,
    _validation_failure_response,
    validate_bpmn,
)
from acp_writer.services.automation_registry import router as automation_registry_router
from acp_writer.tools.dmn_evaluation import DmnEngineError
from cpg_contracts.decisions import normalize_decision_model_summary
from cpg_contracts.service_auth import require_service_token
from cpg_contracts.tracing import install_phi_span_processor
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

install_phi_span_processor()

app = FastAPI(title="acp-writer-decision-engine", version="0.2.0")


@app.get("/health")
def health():
    return {"status": "UP", "service": "decision-engine"}


app.state.automation_registry = _automation_registry
app.state.bpmn_validator = validate_bpmn
app.state.dmn_namespaces = _automation_dmn_namespaces
app.state.dmn_summaries = _automation_dmn_summaries
app.state.automation_validation_result = _automation_validation_result
app.include_router(automation_registry_router)


# --- DMN model management ---


@app.post("/api/v1/decisions/models", status_code=201, dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_DECISION_ENGINE'))])
async def deploy_decision_model(
    request: Request,
    source_cpg: str | None = None,
    validate_only: bool = False,
    replace: bool = False,
):
    body = await request.body()
    dmn_xml = body.decode("utf-8")
    summary = normalize_decision_model_summary(_parse_dmn_metadata(dmn_xml))
    validation = _validate_dmn_with_engine(dmn_xml)
    if validation is not None and not validation.get("valid", False):
        return _validation_failure_response(validation)
    if validate_only:
        if validation is None:
            raise HTTPException(status_code=503, detail="Decision engine validation unavailable")
        return JSONResponse(status_code=200, content=validation)
    if source_cpg:
        summary.source_cpg = source_cpg
    existing = _dynamic_models.get(summary.id)
    if (existing and source_cpg and existing["summary"].source_cpg
            and existing["summary"].source_cpg != source_cpg and not replace):
        logger.warning("Rejecting model id collision: %s (%s vs %s)", summary.id,
                       existing["summary"].source_cpg, source_cpg)
        return JSONResponse(status_code=409, content={
            "error": "Decision model id already belongs to another source CPG",
            "model_id": summary.id,
            "existing_source_cpg": existing["summary"].source_cpg,
            "source_cpg": source_cpg,
        })
    _dynamic_models[summary.id] = {"summary": summary, "dmn_xml": dmn_xml}
    return summary.model_dump(mode="json")


@app.get("/api/v1/decisions/models")
def list_decision_models():
    return [m["summary"].model_dump(mode="json") for m in _dynamic_models.values()]


# --- DMN evaluation (thin: pre-resolved inputs only) ---


@app.post("/api/v1/evaluate", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_DECISION_ENGINE'))])
async def evaluate(request: Request):
    """Evaluate a DMN model with pre-resolved inputs.

    No bundle, no concept resolution, no LLM — pure evaluate-and-return.
    The caller (LLM-reasoning pod) handles input resolution.
    """
    data = await request.json()
    model_id = data.get("model_id")
    inputs = data.get("inputs", {})

    if not model_id:
        raise HTTPException(status_code=400, detail="model_id is required")

    deployed = _dynamic_models.get(model_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not deployed")

    try:
        outputs = _evaluate_jit(deployed["dmn_xml"], inputs)
        return {"outputs": outputs}
    except DmnEngineError as exc:
        logger.warning('DMN engine rejected model_id=%s status=%s', model_id, exc.status_code)
        return JSONResponse(status_code=exc.status_code, content={
            "error": exc.error, "messages": exc.messages,
        })
    except Exception as exc:
        logger.error('DMN evaluation failed model_id=%s exception_type=%s', model_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail=str(exc))


# --- Deprecated: bundle-accepting execution (transition period) ---


@app.post("/api/v1/execute", dependencies=[Depends(require_service_token('SERVICE_TOKEN_ACP_DECISION_ENGINE'))])
async def execute_deprecated(request: Request):
    """Execute DMN models against patient data.

    DEPRECATED: use /api/v1/evaluate with pre-resolved inputs instead.
    This endpoint receives the full patient bundle and runs resolution
    locally. It exists for backward compatibility during the transition
    to the LLM-reasoning pod hosting the resolution loop.
    """
    from acp_writer.nodes.dmn_executor import dmn_executor
    from cpg_contracts import get_phi_store, resolve_ref

    data = await request.json()
    phi_store = get_phi_store()
    ips_bundle = resolve_ref(data, "ips_bundle", phi_store)
    state = {
        "ips_bundle": ips_bundle,
        "applicable_dmn_models": data.get("applicable_dmn_models", []),
        "dmn_dependency_graph": data.get("dmn_dependency_graph", []),
    }
    result = dmn_executor(state)
    return {"dmn_results": result.get("dmn_results", [])}
