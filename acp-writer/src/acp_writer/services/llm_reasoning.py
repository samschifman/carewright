"""LLM Reasoning pod service — Guideline Resolver, Recommendation Retriever,
Plan Composer with Brief Reviewer loop, FHIR Semantic Reviewer.

Also handles CPG artifact management (guidelines, recommendations) since
the vector store and guidelines registry live in this pod's process.

Security profile: LLM inference + vector store, no FHIR server access.
"""

import asyncio
import logging
import os
from uuid import uuid4

import requests as http_requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from cpg_contracts import (
    CPGMetadata,
    Recommendation,
    RecommendationBundle,
    RecommendationSearchRequest,
    get_artifact_store,
    get_phi_store,
    post_callback,
    resolve_ref,
    store_artifact,
)
# Reference the stores via the `api` module (NOT `from acp_writer.api import
# _vector_store`): a from-import binds the name to whatever object exists at
# import time, so a later init_stores() swap (e.g. to a real embedding provider)
# would leave this service pointing at the stale store while the RAG nodes —
# which read api._vector_store at call time — use the new one. Module-attribute
# access keeps ingestion and retrieval on the same store.
from acp_writer import api
from acp_writer.nodes.guideline_resolver import guideline_resolver
from acp_writer.nodes.recommendation_retriever import recommendation_retriever
from acp_writer.nodes.plan_composer import plan_composer
from acp_writer.nodes.brief_reviewer import brief_reviewer
from acp_writer.nodes.conflict_analyst import conflict_analyst
from acp_writer.nodes.automation_synthesizer import synthesize_automations_for_brief
from acp_writer.nodes.fhir_semantic_reviewer import fhir_semantic_reviewer
from acp_writer.pipeline import MAX_BRIEF_REVIEWS
from acp_writer.services.template_client import TemplateClient

logger = logging.getLogger(__name__)

app = FastAPI(title="acp-writer-llm-reasoning", version="0.1.0")
_store = get_artifact_store()
_phi_store = get_phi_store()

LITELLM_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
LLM_MODEL = os.environ.get("LLM_MODEL", "default")


@app.get("/health")
def health():
    return {"status": "UP", "service": "llm-reasoning"}


DECISION_ENGINE_URL = os.environ.get("DECISION_ENGINE_URL", "http://acp-decision-engine:8080")


def _in_process_decision_models() -> list[dict]:
    return [
        entry["summary"].model_dump(mode="json")
        for entry in api._dynamic_models.values()
    ]


def _resolve_guidelines(state: dict) -> dict:
    return guideline_resolver(
        state,
        guidelines_store=api._guidelines_store,
        decision_models_provider=_in_process_decision_models,
    )


def _retrieve_recommendations(state: dict) -> dict:
    return recommendation_retriever(state, vector_store=api._vector_store)


@app.get("/api/v1/status")
def status():
    de_status = "unavailable"
    models_deployed = 0
    try:
        resp = http_requests.get(f"{DECISION_ENGINE_URL}/api/v1/decisions/models", timeout=5)
        if resp.status_code == 200:
            models_deployed = len(resp.json())
            de_status = "healthy"
    except Exception:
        pass
    return {
        "decision_engine": {
            "status": de_status,
            "models_deployed": models_deployed,
        },
        "knowledge_base": {
            "status": "healthy" if api._guidelines_store.count() > 0 else "empty",
            "guidelines_registered": api._guidelines_store.count(),
            "recommendations_ingested": api._vector_store.count(),
        },
    }


# --- Read endpoints (consumed by BFF /api/v1/status) ---


@app.get("/api/v1/decisions/models")
def list_decision_models():
    try:
        resp = http_requests.get(f"{DECISION_ENGINE_URL}/api/v1/decisions/models", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


@app.get("/api/v1/guidelines")
def list_guidelines():
    return [g.model_dump(mode="json") for g in api._guidelines_store.list_all()]


# --- CPG artifact management (used by cpg-ingester Delivery) ---


@app.post("/api/v1/guidelines", status_code=201)
async def register_guideline(request: Request):
    data = await request.json()
    metadata = CPGMetadata.model_validate(data)
    result = api._guidelines_store.register(metadata)
    return result.model_dump(mode="json")


@app.post("/api/v1/knowledge/recommendations/batch", status_code=201)
async def ingest_recommendation_batch(request: Request):
    data = await request.json()
    bundle = RecommendationBundle.model_validate(data)
    api._vector_store.add_batch(bundle.recommendations)
    return {
        "source_cpg": bundle.source_cpg,
        "count": len(bundle.recommendations),
        "status": "ingested",
    }


@app.post("/api/v1/knowledge/search")
async def search_knowledge(request: Request):
    data = await request.json()
    search_req = RecommendationSearchRequest.model_validate(data)
    result = api._vector_store.search(search_req)
    return result.model_dump(mode="json")


# --- DMN execution (resolution + evaluation loop) ---


@app.post("/api/v1/execute-dmn")
async def execute_dmn(request: Request):
    """Execute DMN models with concept-resolution pipeline.

    Resolves DMN inputs using the concept pipeline (cache → terminology
    → display text → LLM inventory match), then evaluates each model
    via the decision-engine service. Chained models' outputs feed
    later models' inputs.
    """
    data = await request.json()
    ips_bundle = resolve_ref(data, "ips_bundle", _phi_store)
    state = {
        "ips_bundle": ips_bundle,
        "applicable_dmn_models": data.get("applicable_dmn_models", []),
        "dmn_dependency_graph": data.get("dmn_dependency_graph", []),
        "litellm_url": LITELLM_URL,
        "llm_model": LLM_MODEL,
    }

    import os
    decision_url = os.environ.get("DECISION_ENGINE_URL", "http://acp-decision-engine:8080")
    state["decision_engine_url"] = decision_url

    from acp_writer.nodes.dmn_executor import dmn_executor
    result = dmn_executor(state)
    return {"dmn_results": result.get("dmn_results", [])}


@app.post("/api/v1/execute-dmn-async")
async def execute_dmn_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run DMN execution in background, POST callback."""
    data = await request.json()
    callback_url = data.pop("callback_url", "")
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(_run_execute_dmn_background, data, callback_url, process_instance_id)
    return {"status": "accepted"}


def _run_execute_dmn_background(data: dict, callback_url: str, process_instance_id: str):
    try:
        ips_bundle = resolve_ref(data, "ips_bundle", _phi_store)
        state = {
            "ips_bundle": ips_bundle,
            "applicable_dmn_models": data.get("applicable_dmn_models", []),
            "dmn_dependency_graph": data.get("dmn_dependency_graph", []),
            "litellm_url": LITELLM_URL,
            "llm_model": LLM_MODEL,
        }

        import os
        decision_url = os.environ.get("DECISION_ENGINE_URL", "http://acp-decision-engine:8080")
        state["decision_engine_url"] = decision_url

        from acp_writer.nodes.dmn_executor import dmn_executor
        result = dmn_executor(state)
        result = {"dmn_results": result.get("dmn_results", [])}
    except Exception as e:
        logger.error("Execute-DMN background task failed: %s", e)
        result = {"error": str(e), "dmn_results": []}

    post_callback(callback_url, process_instance_id, "execute-dmn-done", result)


# --- Pipeline execution endpoints ---


@app.post("/api/v1/resolve")
async def resolve(request: Request):
    """Resolve applicable guidelines for patient conditions."""
    data = await request.json()
    state = {
        "condition_codes": data.get("condition_codes", []),
        "litellm_url": LITELLM_URL,
        "llm_model": LLM_MODEL,
    }
    result = _resolve_guidelines(state)
    return {
        "applicable_cpgs": result.get("applicable_cpgs", []),
        "applicable_dmn_models": result.get("applicable_dmn_models", []),
        "dmn_dependency_graph": result.get("dmn_dependency_graph", []),
    }


@app.post("/api/v1/resolve-async")
async def resolve_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run resolve in background, POST callback."""
    data = await request.json()
    callback_url = data.pop("callback_url", "")
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(_run_resolve_background, data, callback_url, process_instance_id)
    return {"status": "accepted"}


def _run_resolve_background(data: dict, callback_url: str, process_instance_id: str):
    try:
        state = {
            "condition_codes": data.get("condition_codes", []),
            "litellm_url": LITELLM_URL,
            "llm_model": LLM_MODEL,
        }
        result = _resolve_guidelines(state)
        result = {
            "applicable_cpgs": result.get("applicable_cpgs", []),
            "applicable_dmn_models": result.get("applicable_dmn_models", []),
            "dmn_dependency_graph": result.get("dmn_dependency_graph", []),
        }
    except Exception as e:
        logger.error("Resolve background task failed: %s", e)
        result = {"error": str(e)}

    post_callback(callback_url, process_instance_id, "resolve-done", result)


@app.post("/api/v1/retrieve")
async def retrieve(request: Request):
    """Retrieve recommendations from vector store."""
    data = await request.json()
    state = {
        "condition_codes": data.get("condition_codes", []),
        "dmn_results": data.get("dmn_results", []),
        "applicable_cpgs": data.get("applicable_cpgs", []),
    }
    result = _retrieve_recommendations(state)
    recs = result.get("recommendations", [])

    _, ref = store_artifact(_store, f"{uuid4()}/recommendations.json", recs)
    if ref:
        return {"recommendations_ref": ref}
    return {"recommendations": recs}


@app.post("/api/v1/retrieve-async")
async def retrieve_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run retrieve in background, POST callback."""
    data = await request.json()
    callback_url = data.pop("callback_url", "")
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(_run_retrieve_background, data, callback_url, process_instance_id)
    return {"status": "accepted"}


def _run_retrieve_background(data: dict, callback_url: str, process_instance_id: str):
    try:
        state = {
            "condition_codes": data.get("condition_codes", []),
            "dmn_results": data.get("dmn_results", []),
            "applicable_cpgs": data.get("applicable_cpgs", []),
        }
        result = _retrieve_recommendations(state)
        recs = result.get("recommendations", [])

        _, ref = store_artifact(_store, f"{uuid4()}/recommendations.json", recs)
        if ref:
            result = {"recommendations_ref": ref}
        else:
            result = {"recommendations": recs}
    except Exception as e:
        logger.error("Retrieve background task failed: %s", e)
        result = {"error": str(e)}

    post_callback(callback_url, process_instance_id, "retrieve-done", result)


def _prior_brief(data: dict) -> dict:
    """Resolve the prior planning brief for a request-changes loop (F16a/F17a).

    ``prior_brief_ref`` is empty ("") on the first pass — guard for that before
    touching the store. An inline ``prior_brief`` dict is also accepted (used by
    the local revision-flow harness, which drives this pipeline without an
    artifact store). The brief's goals/activities are the composer's revision
    base (F17a/F18b) and its conflicts feed the clinician-directives section +
    analyst continuity (F18a/F17c). Returns ``{}`` when there is no prior brief
    (first pass) so callers stay in authoring mode.
    """
    if not data.get("prior_brief_ref") and not isinstance(data.get("prior_brief"), dict):
        return {}
    prior_brief = resolve_ref(data, "prior_brief", _phi_store)
    return prior_brief if isinstance(prior_brief, dict) else {}


def _render_enforcement_note(unapplied_ids: list[str], conflicts: list[dict]) -> str:
    """Render the F18c enforcement message for directed resolutions the composer
    failed to apply, quoting each conflict's description and suggestion so the
    retry has the full referent inline."""
    by_id = {c.get("id"): c for c in conflicts if isinstance(c, dict)}
    lines = [
        "Your previous revision did NOT apply these clinician-directed "
        "conflict resolutions. Apply each one now (or, ONLY if clinically "
        "unsafe, leave it and it will be flagged to the clinician):",
    ]
    for cid in unapplied_ids:
        c = by_id.get(cid) or {}
        lines.append(f"- [{cid}] {c.get('description', '')}")
        if c.get("suggested_resolution"):
            lines.append(f"  Suggested: {c['suggested_resolution']}")
    return "\n".join(lines)


def _prompt_artifact(state: dict) -> dict:
    """Capture rendered prompts from composer state for AI-InputPrompt DocRefs.

    The monolith keeps the composer, conflict, and automation prompts in state so
    ``fhir_bundle_generator`` can emit AI-InputPrompt DocumentReferences. The
    split path must ferry them across the compose→generate-bundle service
    boundary or the deployed bundle silently loses that prompt traceability
    (issue #169 F2). Rendered prompts embed patient data, so they go to the PHI
    store. Returns ``{}`` when no prompts were captured (e.g. capture disabled).
    """
    prompts = {
        k: state[k]
        for k in ("plan_composer_prompt", "conflict_prompt", "automation_prompts")
        if state.get(k)
    }
    if not prompts:
        return {}
    _, ref = store_artifact(_phi_store, f"{uuid4()}/prompts.json", prompts)
    return {"prompts_ref": ref} if ref else {"prompts": prompts}


def _compose_pipeline(data: dict, strict_store: bool = False) -> dict:
    """The compose pipeline: build state, run the brief-review loop, run the
    conflict analyst, then enforce clinician-directed resolutions (F18c). Used
    by BOTH the sync ``/compose`` handler (in a worker thread, C9) and the
    async background task — one implementation so the two entry points cannot
    drift (the drift caused the original conflict_analyst split-path miss).

    ``strict_store``: when True and an artifact store is configured, failing to
    store the brief raises instead of falling back to an inline brief (the
    async/callback path can't ship a large inline brief through SonataFlow).
    """
    recommendations = resolve_ref(data, "recommendations", _store)
    prior_brief = _prior_brief(data)
    state = {
        "patient_reference": data.get("patient_reference", ""),
        "patient_demographics": data.get("patient_demographics", {}),
        "condition_codes": data.get("condition_codes", []),
        "medication_codes": data.get("medication_codes", []),
        "allergy_codes": data.get("allergy_codes", []),
        "dmn_results": data.get("dmn_results", []),
        "recommendations": recommendations if isinstance(recommendations, list) else [],
        "applicable_cpgs": data.get("applicable_cpgs", []),
        "litellm_url": LITELLM_URL,
        "llm_model": LLM_MODEL,
        "brief_review_count": 0,
        # Prior brief drives plan_composer revision mode (F17a) — its
        # goals/activities are the revision base and its conflicts feed the
        # durable clinician-directives section — plus analyst continuity (F17c).
        "prior_planning_brief": prior_brief,
        "careplan_review_history": data.get("careplan_review_history", []),
        # Latest clinician instruction. plan_composer renders it in the
        # Clinician-directed changes section on EVERY iteration (F18a), and the
        # analyst reads it to judge which conflicts the clinician directed.
        # NEVER seed it into brief_review_feedback: the internal reviewer
        # overwrites that channel each iteration, which silently discarded the
        # clinician's directives after iteration 1 (the F18 bug).
        "careplan_feedback": data.get("careplan_feedback", ""),
        "brief_review_feedback": "",
    }

    for _ in range(MAX_BRIEF_REVIEWS + 1):
        state.update(plan_composer(state))
        state.update(brief_reviewer(state))
        if not state.get("brief_review_feedback"):
            break
        if state.get("brief_review_count", 0) >= MAX_BRIEF_REVIEWS:
            break

    template_client = TemplateClient()
    state.update(synthesize_automations_for_brief(
        state,
        registry=template_client,
        validator=template_client.validate,
    ))

    # Detect plan-level conflicts on the converged brief (annotates the brief in
    # place — never edits goals/activities). Mirrors the monolith pipeline order
    # (brief-review loop → conflict_analyst → FHIR generation); the brief carries
    # the conflicts downstream to fhir_bundle_generator → conflict Provenances.
    state.update(conflict_analyst(state))

    # Enforcement (F18c): if the analyst reports clinician-directed resolutions
    # that the revision did not apply, retry the composer ONCE with an explicit
    # enforcement note (the clinician-directives section re-renders with it),
    # re-run the analyst, and if directives are STILL unapplied, flag the brief
    # naming them — an honest "could not apply" in the review UI instead of
    # silently re-presenting the same conflicts.
    unapplied = state.get("unapplied_directed_conflicts") or []
    if unapplied:
        brief_conflicts = (state.get("planning_brief") or {}).get("conflicts") or []
        state["directive_enforcement_note"] = _render_enforcement_note(unapplied, brief_conflicts)
        state["brief_review_feedback"] = ""
        logger.warning("Enforcing %d unapplied clinician directive(s) — composer retry", len(unapplied))
        state.update(plan_composer(state))
        state.update(conflict_analyst(state))
        state.update(synthesize_automations_for_brief(
            state,
            registry=template_client,
            validator=template_client.validate,
        ))
        unapplied = state.get("unapplied_directed_conflicts") or []
        if unapplied:
            brief = dict(state.get("planning_brief") or {})
            names = ", ".join(unapplied)
            note = (
                f"Clinician-directed resolutions could not be applied: {names}. "
                "The affected conflicts remain in the plan for clinician review."
            )
            existing = brief.get("review_feedback")
            brief["review_status"] = "flagged"
            brief["review_feedback"] = f"{existing}\n{note}" if existing else note
            state["planning_brief"] = brief
            logger.error("Unapplied clinician directives after enforcement retry: %s", names)

    brief = state.get("planning_brief", {})
    result = _prompt_artifact(state)
    if state.get("automation_synthesis_warnings"):
        result["automation_synthesis_warnings"] = state["automation_synthesis_warnings"]
    _, ref = store_artifact(_phi_store, f"{uuid4()}/planning_brief.json", brief)
    if ref:
        result["planning_brief_ref"] = ref
    elif strict_store and _phi_store:
        raise RuntimeError("Artifact store available but failed to store planning brief")
    else:
        result["planning_brief"] = brief
    return result


@app.post("/api/v1/compose")
async def compose(request: Request):
    """Run Plan Composer with Brief Reviewer loop."""
    data = await request.json()
    return await asyncio.to_thread(_compose_pipeline, data)


@app.post("/api/v1/compose-async")
async def compose_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run compose in background, POST callback."""
    data = await request.json()
    callback_url = data.pop("callback_url", "")
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(_run_compose_background, data, callback_url, process_instance_id)
    return {"status": "accepted"}


def _run_compose_background(data: dict, callback_url: str, process_instance_id: str):
    try:
        result = _compose_pipeline(data, strict_store=True)
    except Exception as e:
        logger.error("Compose background task failed: %s", e)
        result = {"error": str(e)}

    post_callback(callback_url, process_instance_id, "compose-done", result)


@app.post("/api/v1/review-fhir")
async def review_fhir(request: Request):
    """Run FHIR Semantic Reviewer."""
    data = await request.json()
    fhir_bundle = resolve_ref(data, "fhir_bundle", _phi_store)
    state = {
        "fhir_bundle": fhir_bundle,
        "terminology_issues": data.get("terminology_issues", []),
        "syntax_errors": data.get("syntax_errors", []),
        "fhir_review_count": data.get("fhir_review_count", 0),
        "litellm_url": LITELLM_URL,
        "llm_model": LLM_MODEL,
    }
    result = fhir_semantic_reviewer(state)
    return {
        "fhir_review_feedback": result.get("fhir_review_feedback", ""),
        "fhir_review_count": result.get("fhir_review_count", 0),
    }


@app.post("/api/v1/review-fhir-async")
async def review_fhir_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run review in background, POST callback."""
    data = await request.json()
    callback_url = data.pop("callback_url", "")
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(_run_review_fhir_background, data, callback_url, process_instance_id)
    return {"status": "accepted"}


def _run_review_fhir_background(data: dict, callback_url: str, process_instance_id: str):
    try:
        fhir_bundle = resolve_ref(data, "fhir_bundle", _phi_store)
        state = {
            "fhir_bundle": fhir_bundle,
            "terminology_issues": data.get("terminology_issues", []),
            "syntax_errors": data.get("syntax_errors", []),
            "fhir_review_count": data.get("fhir_review_count", 0),
            "litellm_url": LITELLM_URL,
            "llm_model": LLM_MODEL,
        }
        result_state = fhir_semantic_reviewer(state)
        result = {
            "fhir_review_feedback": result_state.get("fhir_review_feedback", ""),
            "fhir_review_count": result_state.get("fhir_review_count", 0),
        }
    except Exception as e:
        logger.error("Review-FHIR background task failed: %s", e)
        result = {"error": str(e), "fhir_review_feedback": "", "fhir_review_count": 0}

    post_callback(callback_url, process_instance_id, "review-done", result)
