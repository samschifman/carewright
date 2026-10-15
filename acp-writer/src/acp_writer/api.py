"""FastAPI REST API for the acp-writer service."""

import base64
import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import mlflow
import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from cpg_contracts import (
    CPGMetadata,
    DecisionModelSummary,
    DecisionVariable,
    decision_model_id,
    Recommendation,
    RecommendationBundle,
    RecommendationSearchRequest,
)
from cpg_contracts.decisions import normalize_decision_model_summary
from cpg_contracts.automation.catalog import load_catalog
from cpg_contracts.automation.ir import ProcessIR
from cpg_contracts.automation.instances import Evidence, ParameterBinding
from cpg_contracts.automation.validators.grounding import InstanceContext
from cpg_contracts.automation.validators.ladder import run_static_ladder
from cpg_contracts.automation.compiler import (
    ParseError,
    compile as compile_bpmn,
    parse as parse_bpmn,
)

from acp_writer.store.embedding import (
    EmbeddingProvider,
    make_embedding_provider,
)
from acp_writer.store.guidelines_store import GuidelinesStore
from acp_writer.store.vector_store import InMemoryVectorStore, VectorStore
from acp_writer.tools.dmn_evaluation import DmnEngineError
from acp_writer.services.automation_registry import AutomationRegistry, router as automation_registry_router

try:
    mlflow.fastapi.autolog()
except AttributeError:
    pass

logger = logging.getLogger(__name__)

KOGITO_URL = os.environ.get("KOGITO_URL", "http://localhost:8081")

# DMN metadata is parsed with namespace-wildcard matches ("{*}tag") so it works
# across DMN language versions (1.3, 1.4, …) without pinning a MODEL namespace.

_dynamic_models: dict[str, dict] = {}
_automation_registry = AutomationRegistry()

# --- Store initialization ---
# The provider is chosen by the EMBEDDING_PROVIDER env switch
# (make_embedding_provider): "openai" for a real OpenAI-compatible endpoint,
# otherwise a FakeEmbeddingProvider (the default — no network/downloads on
# import, so tests stay hermetic). Call init_stores() to swap providers.

_embedding_provider: EmbeddingProvider = make_embedding_provider()
_vector_store: VectorStore = InMemoryVectorStore(_embedding_provider)
_guidelines_store: GuidelinesStore = GuidelinesStore(_vector_store)


def init_stores(embedding_provider: EmbeddingProvider | None = None) -> None:
    """Re-initialize stores with a specific embedding provider."""
    global _embedding_provider, _vector_store, _guidelines_store
    if embedding_provider:
        _embedding_provider = embedding_provider
    _vector_store = InMemoryVectorStore(_embedding_provider)
    _guidelines_store = GuidelinesStore(_vector_store)


app = FastAPI(
    title="ACP Writer API",
    version="0.2.0",
    description="Composes patient-specific, FHIR-compliant care plans.",
)



@app.on_event("startup")
async def startup():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    from acp_writer.services.automation_publisher import get_publisher

    get_publisher().reconcile_on_start()


def _check_kogito() -> bool:
    try:
        r = requests.get(f"{KOGITO_URL}/q/health/ready", timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


import re as _re

_CODE_TOKEN_RE = _re.compile(r"(https?://[^\s|]+)\|(\S+)")


def _extract_codes(input_data_el: ET.Element) -> list[str]:
    """Extract clinical codes from a DMN inputData element.

    Looks for codes in two places (in order):
    1. extensionElements with code annotations (future cpg-ingester output)
    2. Structured code tokens in the description element text

    Code format: "system-url|code" (e.g. "http://loinc.org|8480-6").
    """
    codes: list[str] = []

    ext = input_data_el.find("{*}extensionElements")
    if ext is not None:
        for child in ext:
            if child.tag.rsplit("}", 1)[-1] == "extraction":
                continue
            system = child.get("system", "")
            code = child.get("code", "")
            if system and code:
                codes.append(f"{system}|{code}")
            elif child.text:
                codes.extend(f"{system}|{code}" for system, code in _CODE_TOKEN_RE.findall(child.text))

    if not codes:
        desc_el = input_data_el.find("{*}description")
        if desc_el is not None and desc_el.text:
            for system, code in _CODE_TOKEN_RE.findall(desc_el.text):
                codes.append(f"{system}|{code}")

    return codes


def _extract_description(input_data_el: ET.Element) -> str | None:
    """Extract description text from a DMN inputData element."""
    desc_el = input_data_el.find("{*}description")
    if desc_el is not None and desc_el.text:
        return desc_el.text.strip()
    return None


def _extract_extraction(input_data_el: ET.Element) -> dict | None:
    ext = input_data_el.find("{*}extensionElements")
    if ext is None:
        return None
    for child in ext:
        if child.tag.rsplit("}", 1)[-1] != "extraction":
            continue
        try:
            payload = json.loads("".join(child.itertext()).strip())
        except (TypeError, json.JSONDecodeError):
            logger.warning("Ignoring malformed DMN extraction annotation")
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _parse_dmn_metadata(dmn_xml: str) -> DecisionModelSummary:
    """Extract model name, inputs, and outputs from DMN XML."""
    root = ET.fromstring(dmn_xml)

    model_name = root.get("name", "unknown")
    stable_id = decision_model_id(model_name)
    root_id = root.get("id")
    model_id = root_id if root_id == stable_id else stable_id

    inputs = []
    for input_data in root.findall("{*}inputData"):
        var = input_data.find("{*}variable")
        if var is not None:
            codes = _extract_codes(input_data)
            desc = _extract_description(input_data)
            extraction = _extract_extraction(input_data)
            inputs.append(DecisionVariable(
                name=var.get("name", ""),
                type=var.get("typeRef", "string"),
                codes=codes or None,
                description=desc,
                extraction=extraction,
            ))

    if not inputs:
        seen_names: set[str] = set()
        for decision in root.findall("{*}decision"):
            dt = decision.find("{*}decisionTable")
            if dt is not None:
                for inp in dt.findall("{*}input"):
                    input_expr = inp.find("{*}inputExpression")
                    if input_expr is not None:
                        text_el = input_expr.find("{*}text")
                        var_name = text_el.text.strip() if text_el is not None and text_el.text else inp.get("label", "")
                        if var_name and var_name not in seen_names:
                            seen_names.add(var_name)
                            inputs.append(DecisionVariable(
                                name=var_name,
                                type=input_expr.get("typeRef", "string"),
                            ))

    outputs = []
    for decision in root.findall("{*}decision"):
        dt = decision.find("{*}decisionTable")
        if dt is not None:
            for output in dt.findall("{*}output"):
                outputs.append(DecisionVariable(
                    name=output.get("name", ""),
                    type=output.get("typeRef", "string"),
                ))

    return DecisionModelSummary(
        id=model_id,
        name=model_name,
        inputs=inputs,
        outputs=outputs,
        namespace=root.get("namespace"),
        deployed_at=datetime.now(timezone.utc),
    )


@mlflow.trace(span_type="TOOL", name="evaluate_jit_dmn")
def _evaluate_jit(dmn_xml: str, inputs: dict) -> dict:
    """Evaluate DMN via the JIT endpoint on the decision-service."""
    dmn_b64 = base64.b64encode(dmn_xml.encode()).decode()
    r = requests.post(
        f"{KOGITO_URL}/jit/dmn",
        json={"dmn_xml_base64": dmn_b64, "inputs": inputs},
        timeout=30,
    )
    if 400 <= r.status_code < 500:
        raise DmnEngineError.from_response(r)
    r.raise_for_status()
    return r.json()


@mlflow.trace(span_type="TOOL", name="validate_dmn_with_engine")
def _validate_dmn_with_engine(dmn_xml: str) -> dict | None:
    """Validate DMN with KIE, returning ``None`` when the engine is unavailable.

    Deployment remains available when the optional decision engine is down. The
    caller distinguishes that fail-open path from an engine response with
    ``valid: false`` and rejects only the latter.
    """
    dmn_b64 = base64.b64encode(dmn_xml.encode()).decode()
    try:
        response = requests.post(
            f"{KOGITO_URL}/jit/dmn/validate",
            json={"dmn_xml_base64": dmn_b64},
            timeout=30,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        logger.warning("DMN engine validation unavailable; accepting model provisionally: %s", exc)
        return None
    except requests.RequestException as exc:
        logger.error("DMN engine validation request failed: %s", exc)
        return {"valid": False, "messages": [{
            "severity": "ERROR",
            "text": f"decision engine validation request failed: {exc}",
        }]}
    if response.status_code >= 400:
        body = getattr(response, "text", "")[:500]
        logger.error("DMN engine validation returned HTTP %s: %s", response.status_code, body)
        return {"valid": False, "messages": [{
            "severity": "ERROR",
            "text": f"decision engine validation returned HTTP {response.status_code}: {body}",
        }]}
    try:
        result = response.json()
    except ValueError:
        result = None
    if not isinstance(result, dict) or "valid" not in result:
        body = getattr(response, "text", "")[:500]
        logger.error("Decision engine returned invalid validation response: %s", body)
        return {"valid": False, "messages": [{
            "severity": "ERROR",
            "text": f"decision engine validation returned invalid JSON: {body}",
        }]}
    return result


@mlflow.trace(span_type="TOOL", name="validate_bpmn_with_engine")
def validate_bpmn(bpmn_xml: str) -> dict | None:
    """Validate BPMN with Kogito, returning None when the engine is unavailable."""
    bpmn_b64 = base64.b64encode(bpmn_xml.encode()).decode()
    try:
        response = requests.post(
            f"{KOGITO_URL}/jit/bpmn/validate",
            json={"bpmn_xml_base64": bpmn_b64},
            timeout=30,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        logger.warning("BPMN engine validation unavailable: %s", exc)
        return None
    except requests.RequestException as exc:
        logger.error("BPMN engine validation request failed: %s", exc)
        return {"valid": False, "messages": [{
            "severity": "ERROR",
            "text": f"decision engine validation request failed: {exc}",
        }]}
    if response.status_code >= 400:
        body = getattr(response, "text", "")[:500]
        return {"valid": False, "messages": [{
            "severity": "ERROR",
            "text": f"decision engine validation returned HTTP {response.status_code}: {body}",
        }]}
    try:
        result = response.json()
    except ValueError:
        result = None
    if not isinstance(result, dict) or "valid" not in result:
        body = getattr(response, "text", "")[:500]
        return {"valid": False, "messages": [{
            "severity": "ERROR",
            "text": f"decision engine returned invalid validation JSON: {body}",
        }]}
    return result


def _automation_dmn_namespaces() -> dict[str, str]:
    namespaces: dict[str, str] = {}
    for model_id, model in _dynamic_models.items():
        summary = normalize_decision_model_summary(model["summary"])
        if summary.namespace is not None:
            namespaces[model_id] = summary.namespace
    return namespaces


def _automation_dmn_summaries() -> dict[str, DecisionModelSummary]:
    return {
        model_id: normalize_decision_model_summary(model["summary"])
        for model_id, model in _dynamic_models.items()
    }


@mlflow.trace(name="automation.validate")
def _automation_validation_result(payload: dict) -> dict:
    """Run local ladder checks and Kogito for an IR or BPMN request."""
    if ("ir" in payload) == ("bpmn_xml" in payload):
        raise HTTPException(status_code=400, detail="provide exactly one of 'ir' or 'bpmn_xml'")
    try:
        if "bpmn_xml" in payload:
            xml = payload["bpmn_xml"]
            if not isinstance(xml, str):
                raise ValueError("bpmn_xml must be a string")
            process_ir = parse_bpmn(xml)
        else:
            process_ir = ProcessIR.model_validate(payload["ir"])
            xml = None
    except (ParseError, ValueError, TypeError) as exc:
        return {"valid": False, "rungs_passed": [], "findings": [], "errors": [str(exc)]}

    validation_xml = xml
    if validation_xml is None:
        try:
            validation_xml = compile_bpmn(
                process_ir, dmn_namespaces=_automation_dmn_namespaces()
            )
        except (KeyError, TypeError, ValueError) as exc:
            return {
                "valid": False,
                "rungs_passed": [],
                "findings": [],
                "errors": [str(exc)],
            }

    stage = "template" if process_ir.process.kind == "template" else "instance"
    templates = [ProcessIR.model_validate(item) for item in payload.get("templates", [])]
    context_data = payload.get("context", {})
    context = None
    if stage == "instance":
        context = InstanceContext(
            bindings=[
                ParameterBinding.model_validate(item)
                for item in context_data.get("bindings", [])
            ],
            evidence={
                key: Evidence.model_validate(item)
                for key, item in context_data.get("evidence", {}).items()
            },
            dmn_audit_trail=context_data.get("dmn_audit_trail", []),
            careplan_feedback=context_data.get("careplan_feedback", ""),
        )
    result = run_static_ladder(
        xml if xml is not None else process_ir,
        catalog=load_catalog(),
        stage=stage,
        templates=templates if stage == "instance" else None,
        context=context,
        dmn_summaries=_automation_dmn_summaries(),
        phi_known_values=payload.get("phi_known_values", ()),
    )
    if not result.ok:
        return {
            "valid": False,
            "bpmn_xml": validation_xml,
            "rungs_passed": result.rungs_passed,
            "findings": [finding.__dict__ for finding in result.findings],
            "deferred": result.deferred,
        }
    engine = validate_bpmn(validation_xml)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Decision engine BPMN validation unavailable",
                "bpmn_xml": validation_xml,
            },
        )
    return {
        "valid": bool(engine.get("valid")),
        "bpmn_xml": validation_xml,
        "rungs_passed": [*result.rungs_passed, "L6"] if engine.get("valid") else result.rungs_passed,
        "findings": [finding.__dict__ for finding in result.findings],
        "messages": engine.get("messages", []),
        "deferred": result.deferred,
    }


def _validation_failure_response(validation: dict) -> JSONResponse:
    messages = validation.get("messages", [])
    return JSONResponse(
        status_code=422,
        content={
            "error": "DMN engine validation failed",
            "messages": messages,
        },
    )


# --- Health ---


@app.get("/health")
def health():
    return {"status": "UP"}


@app.get("/health/ready")
def readiness():
    if _check_kogito():
        return {"status": "UP"}
    raise HTTPException(status_code=503, detail="Decision engine not available")


@app.get("/api/v1/status")
def status():
    kogito_healthy = _check_kogito()
    total_models = len(_dynamic_models)
    return {
        "version": "0.2.0",
        "decision_engine": {
            "status": "healthy" if kogito_healthy else "unavailable",
            "models_deployed": total_models,
        },
        "knowledge_base": {
            "status": "available",
            "guidelines_registered": _guidelines_store.count(),
            "recommendations_ingested": _vector_store.count(),
        },
    }


# --- Care Plans ---


@app.post("/api/v1/careplans", status_code=201)
async def generate_careplan(request: Request):
    bundle = await request.json()

    if bundle.get("resourceType") != "Bundle":
        raise HTTPException(status_code=400, detail="Request body must be a FHIR Bundle")

    import uuid

    from acp_writer.checkpointer import get_checkpointer
    from acp_writer.pipeline import MAX_BRIEF_REVIEWS, build_pipeline

    litellm_url = os.environ.get("LITELLM_URL", "http://localhost:4000")
    llm_model = os.environ.get("LLM_MODEL", "default")

    run_id = str(uuid.uuid4())
    careplan_id = uuid.uuid4().hex
    output_root = os.environ.get("ACP_RUN_ARTIFACT_DIR", "")
    output_dir = os.path.join(output_root, careplan_id) if output_root else ""
    graph = build_pipeline(
        automation_registry=_automation_registry,
        automation_validator=_automation_validation_result,
        guidelines_store=_guidelines_store,
        vector_store=_vector_store,
        decision_models_provider=lambda: [
            entry["summary"].model_dump(mode="json")
            for entry in _dynamic_models.values()
        ],
    )
    checkpointer = get_checkpointer()
    compiled = graph.compile(checkpointer=checkpointer)

    try:
        result = compiled.invoke(
            {
                "ips_bundle": bundle,
                "run_id": run_id,
                "careplan_id": careplan_id,
                "output_dir": output_dir,
                "approved": False,
                "litellm_url": litellm_url,
                "llm_model": llm_model,
            },
            config={"configurable": {"thread_id": run_id}},
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Care plan generation failed: {e}")

    brief = result.get("planning_brief") or {}
    if (
        result.get("brief_review_count", 0) >= MAX_BRIEF_REVIEWS
        and not brief.get("goals")
        and not brief.get("activities")
    ):
        feedback = result.get("brief_review_feedback") or brief.get("review_feedback")
        raise HTTPException(
            status_code=422,
            detail=feedback or "Care plan generation exhausted its brief review budget without a usable brief.",
        )

    fhir_bundle = result.get("fhir_bundle", {})
    return Response(
        content=json.dumps(fhir_bundle),
        media_type="application/fhir+json",
        status_code=201,
    )


@app.get("/api/v1/careplans")
def list_careplans(patient: str | None = None, status: str | None = None):
    from acp_writer.nodes.fhir_server_writer import list_care_plans
    return list_care_plans(patient=patient, status=status)


@app.get("/api/v1/careplans/{careplan_id}")
def get_careplan(careplan_id: str):
    from acp_writer.nodes.fhir_server_writer import get_care_plan
    cp = get_care_plan(careplan_id)
    if not cp:
        raise HTTPException(status_code=404, detail=f"Care plan '{careplan_id}' not found")
    return cp


@app.put("/api/v1/careplans/{careplan_id}/status")
async def update_careplan_status(careplan_id: str, request: Request):
    from acp_writer.nodes.fhir_server_writer import approve_care_plan, reject_care_plan
    from acp_writer.services.reviewer import reviewer_from_payload
    data = await request.json()
    new_status = data.get("status")
    if new_status == "active":
        reviewer = reviewer_from_payload(data.get("reviewer"), clinician=data.get("clinician"))
        result = approve_care_plan(careplan_id, reviewer=reviewer)
        if not result:
            raise HTTPException(status_code=404, detail=f"Care plan '{careplan_id}' not found")
        return result
    elif new_status == "entered-in-error":
        reason = data.get("reason", "No reason provided")
        result = reject_care_plan(careplan_id, reason=reason)
        if not result:
            raise HTTPException(status_code=404, detail=f"Care plan '{careplan_id}' not found")
        return result
    else:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}. Use 'active' or 'entered-in-error'.")


@app.post("/api/v1/careplans/{careplan_id}/automations/publish")
def retry_automation_publication(careplan_id: str):
    from acp_writer.services.automation_publisher import get_publisher
    result = get_publisher().retry(careplan_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No retryable publication job found")
    return result


# --- Decision Models ---


app.state.automation_registry = _automation_registry
app.state.bpmn_validator = validate_bpmn
app.state.dmn_namespaces = _automation_dmn_namespaces
app.state.dmn_summaries = _automation_dmn_summaries
app.state.automation_validation_result = _automation_validation_result
app.include_router(automation_registry_router)


# --- Decision Models ---


@app.post("/api/v1/decisions/models", status_code=201)
async def deploy_decision_model(
    request: Request,
    source_cpg: str | None = None,
    validate_only: bool = False,
    replace: bool = False,
):
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    dmn_xml = body.decode("utf-8")

    try:
        summary = normalize_decision_model_summary(_parse_dmn_metadata(dmn_xml))
    except ET.ParseError as e:
        raise HTTPException(status_code=400, detail=f"Invalid DMN XML: {e}")

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

    _dynamic_models[summary.id] = {
        "summary": summary,
        "dmn_xml": dmn_xml,
    }

    logger.info("Deployed decision model: %s (%s, source_cpg=%s)", summary.name, summary.id, source_cpg)
    return summary.model_dump(mode="json")


@app.get("/api/v1/decisions/models")
def list_decision_models():
    return [m["summary"].model_dump(mode="json") for m in _dynamic_models.values()]


@app.get("/api/v1/decisions/models/{model_id}")
def get_decision_model(model_id: str):
    model = _dynamic_models.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    result = model["summary"].model_dump(mode="json")
    result["dmn_xml"] = model["dmn_xml"]
    return result


@app.delete("/api/v1/decisions/models/{model_id}", status_code=204)
def remove_decision_model(model_id: str):
    if model_id not in _dynamic_models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    del _dynamic_models[model_id]
    logger.info("Removed decision model: %s", model_id)


@app.post("/api/v1/decisions/evaluate/{model_id}")
async def evaluate_decision(model_id: str, request: Request):
    model = _dynamic_models.get(model_id)
    if not model:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    inputs = await request.json()
    try:
        result = _evaluate_jit(model["dmn_xml"], inputs)
        return result
    except DmnEngineError as exc:
        logger.warning("DMN engine rejected evaluation for %s: %s", model_id, exc.error)
        return JSONResponse(status_code=exc.status_code, content={
            "error": exc.error, "messages": exc.messages,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Decision evaluation failed: {e}")


# --- Guidelines ---


@app.post("/api/v1/guidelines", status_code=201)
async def register_guideline(request: Request):
    data = await request.json()
    try:
        metadata = CPGMetadata.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid CPG metadata: {e}")
    result = _guidelines_store.register(metadata)
    return result.model_dump(mode="json")


@app.get("/api/v1/guidelines")
def list_guidelines():
    return [g.model_dump(mode="json") for g in _guidelines_store.list_all()]


@app.get("/api/v1/guidelines/{cpg_id}")
def get_guideline(cpg_id: str):
    g = _guidelines_store.get(cpg_id)
    if not g:
        raise HTTPException(status_code=404, detail=f"Guideline '{cpg_id}' not found")
    return g.model_dump(mode="json")


@app.delete("/api/v1/guidelines/{cpg_id}", status_code=204)
def delete_guideline(cpg_id: str):
    if not _guidelines_store.delete(cpg_id):
        raise HTTPException(status_code=404, detail=f"Guideline '{cpg_id}' not found")


# --- Knowledge / Recommendations ---


@app.post("/api/v1/knowledge/recommendations", status_code=201)
async def ingest_recommendation(request: Request):
    data = await request.json()
    try:
        rec = Recommendation.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid recommendation: {e}")
    _vector_store.add(rec)
    return {"id": rec.id, "status": "ingested"}


@app.post("/api/v1/knowledge/recommendations/batch", status_code=201)
async def ingest_recommendation_batch(request: Request):
    data = await request.json()
    try:
        bundle = RecommendationBundle.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid recommendation bundle: {e}")
    _vector_store.add_batch(bundle.recommendations)
    return {
        "source_cpg": bundle.source_cpg,
        "count": len(bundle.recommendations),
        "status": "ingested",
    }


@app.get("/api/v1/knowledge/recommendations")
def list_recommendations(source_cpg: str | None = None):
    recs = _vector_store.list_all(source_cpg=source_cpg)
    return [r.model_dump(mode="json") for r in recs]


@app.get("/api/v1/knowledge/recommendations/{recommendation_id}")
def get_recommendation(recommendation_id: str):
    rec = _vector_store.get(recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Recommendation '{recommendation_id}' not found")
    return rec.model_dump(mode="json")


@app.post("/api/v1/knowledge/search")
async def search_knowledge(request: Request):
    data = await request.json()
    try:
        search_req = RecommendationSearchRequest.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid search request: {e}")
    result = _vector_store.search(search_req)
    return result.model_dump(mode="json")
