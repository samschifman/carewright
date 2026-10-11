"""LLM Analysis pod service — structure analysis and artifact generation.

Two async endpoints:
  /api/v1/analyze-async  — classify sections, build manifest, extract metadata
  /api/v1/generate-async — generate DMN, recommendations, and BPMN templates

Split so human review gates can approve the manifest before spending LLM
tokens on generation.

Consumes: parse_result_ref (analyze) or analysis_result_ref (generate).
Produces: analysis_result_ref or generate_result_ref.
Security profile: LLM inference endpoint only (via MaaS on OpenShift).
"""

import logging
import os
import tempfile
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Request

from cpg_contracts import get_artifact_store, post_callback, resolve_ref, store_artifact
from cpg_ingester.nodes.content_filter import content_filter
from cpg_ingester.nodes.figure_interpreter import figure_interpreter
from cpg_ingester.nodes.item_identifier import item_identifier
from cpg_ingester.nodes.classification_reviewer import classification_reviewer
from cpg_ingester.nodes.metadata_extractor import metadata_extractor
from cpg_ingester.nodes.structure_analyzer import structure_analyzer

logger = logging.getLogger(__name__)

app = FastAPI(title="cpg-ingester-llm-analysis", version="0.1.0")
_store = get_artifact_store()

MAX_CLASSIFICATION_REVIEWS = 2

LITELLM_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
LLM_MODEL = os.environ.get("LLM_MODEL", "default")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "sk-change-me")


@app.get("/health")
def health():
    return {"status": "UP", "service": "llm-analysis"}


_PARSE_SUFFIX = "parse_result.json"
_ENRICHED_SUFFIX = "parse_result_enriched.json"


def _enriched_ref(parse_result_ref: str | None) -> str | None:
    """Derive the enriched-parse cache ref from the parse_result ref.

    Replaces the trailing ``parse_result.json`` with
    ``parse_result_enriched.json`` so the enriched output lands in the same
    ``{prefix}/`` directory that already holds ``figures/``. Returns ``None`` if
    the ref is absent or not the expected shape (nothing to cache against).
    """
    if not parse_result_ref or not parse_result_ref.endswith(_PARSE_SUFFIX):
        return None
    return parse_result_ref[: -len(_PARSE_SUFFIX)] + _ENRICHED_SUFFIX


def _ref_key(ref: str) -> str:
    """Object-key portion of a ``bucket:key`` ref (or the ref itself if plain)."""
    if ":" in ref and not ref.startswith("s3://"):
        return ref.split(":", 1)[1]
    return ref


def _do_analyze(data: dict) -> dict:
    """Run structure analysis: classify sections, build manifest, extract metadata.

    Stops before generation so the manifest can be reviewed first.
    """
    parse_result = resolve_ref(data, "parse_result", _store)
    if isinstance(parse_result, dict) and "markdown" in parse_result:
        markdown = parse_result["markdown"]
        docling_json = parse_result.get("docling_json", {})
        figures = parse_result.get("figures", []) or []
    else:
        markdown = data.get("markdown", "")
        docling_json = data.get("docling_json", {})
        figures = data.get("figures", []) or []

    review_feedback = data.get("review_feedback")
    review_comment = data.get("review_comment")
    review_iteration = data.get("review_iteration", 0)

    with tempfile.TemporaryDirectory() as output_dir:
        state = {
            "markdown": markdown,
            "docling_json": docling_json,
            "figures": figures,
            "output_dir": output_dir,
            "litellm_url": LITELLM_URL,
            "llm_model": LLM_MODEL,
            "llm_api_key": LLM_API_KEY,
        }

        if review_feedback and review_iteration:
            state["review_feedback"] = review_feedback
            state["review_comment"] = review_comment

        # Interpret figures first so the enriched markdown (Mermaid + figure
        # descriptions inlined at each figure's location) flows into structure
        # analysis and everything downstream. Kept as a decoupled agent so it
        # can move to its own pod later.
        #
        # Review rounds must make ZERO vision calls: the enriched parse
        # is cached next to the parse result under a ref derived from
        # parse_result_ref. On a cache hit we reuse it verbatim and skip the
        # interpreter entirely, so round-2+ markdown is byte-identical to round
        # 1's — the interpretation never re-runs nondeterministically. A miss is
        # the normal first-round case (silent-cheap). Re-uploading the CPG mints
        # a fresh per-run prefix → fresh ref → cache miss → fresh interpretation,
        # so the "re-upload to fix" remediation still works.
        enriched_ref = _enriched_ref(data.get("parse_result_ref")) if _store else None
        cached = None
        if enriched_ref:
            try:
                cached = _store.get(enriched_ref)
            except Exception:  # noqa: BLE001 — a miss is the normal first round
                cached = None

        if isinstance(cached, dict) and "markdown" in cached:
            logger.info("Using cached figure interpretations from %s", enriched_ref)
            state["markdown"] = cached["markdown"]
            state["figures"] = cached.get("figures", figures)
        else:
            updates = figure_interpreter(state)
            state.update(updates)
            if enriched_ref:
                try:
                    store_artifact(
                        _store,
                        _ref_key(enriched_ref),
                        {"markdown": state["markdown"], "figures": state.get("figures", [])},
                    )
                except Exception as e:  # noqa: BLE001 — caching is best-effort
                    logger.warning("Failed to cache enriched parse at %s: %s", enriched_ref, e)
        markdown = state["markdown"]

        updates = structure_analyzer(state)
        state.update(updates)

        updates = content_filter(state)
        state.update(updates)

        for _ in range(MAX_CLASSIFICATION_REVIEWS + 1):
            updates = item_identifier(state)
            state.update(updates)

            updates = classification_reviewer(state)
            state.update(updates)

            if not state.get("classification_review_feedback"):
                break
            if state.get("classification_review_count", 0) >= MAX_CLASSIFICATION_REVIEWS:
                break

        updates = metadata_extractor(state)
        state.update(updates)

        result = {
            "markdown": markdown,
            "figures": state.get("figures", []),
            "section_map": state.get("section_map", []),
            "abbreviations": state.get("abbreviations", {}),
            "grading_definitions": state.get("grading_definitions", ""),
            "archetype": state.get("archetype", ""),
            "item_manifest": state.get("item_manifest", []),
            "cpg_metadata": state.get("cpg_metadata", {}),
        }

        _, ref = store_artifact(_store, f"{uuid4()}/analysis_result.json", result)
        if ref:
            return {"analysis_result_ref": ref}
        if _store:
            raise RuntimeError("Artifact store available but failed to store analysis result")
        return result


def _do_generate(data: dict) -> dict:
    """Generate DMN, recommendations, and BPMN templates from an approved manifest."""
    from cpg_ingester.generation import generate_all

    analysis = resolve_ref(data, "analysis_result", _store)
    if not isinstance(analysis, dict) or "item_manifest" not in analysis:
        raise ValueError("analysis_result must contain item_manifest")

    review_feedback = data.get("review_feedback")
    review_comment = data.get("review_comment")
    review_iteration = data.get("review_iteration", 0)

    with tempfile.TemporaryDirectory() as output_dir:
        state = {
            "markdown": analysis.get("markdown", ""),
            "figures": analysis.get("figures", []),
            "section_map": analysis.get("section_map", []),
            "abbreviations": analysis.get("abbreviations", {}),
            "grading_definitions": analysis.get("grading_definitions", ""),
            "archetype": analysis.get("archetype", ""),
            "item_manifest": analysis.get("item_manifest", []),
            "cpg_metadata": analysis.get("cpg_metadata", {}),
            "output_dir": output_dir,
            "litellm_url": LITELLM_URL,
            "llm_model": LLM_MODEL,
            "llm_api_key": LLM_API_KEY,
        }

        if review_feedback and review_iteration:
            state["review_feedback"] = review_feedback
            state["review_comment"] = review_comment

        updates = generate_all(state)
        state.update(updates)

        result = {
            "cpg_metadata": state.get("cpg_metadata", {}),
            "item_manifest": state.get("item_manifest", []),
            "dmn_results": state.get("dmn_results", []),
            "recommendation_results": state.get("recommendation_results", []),
            "recommendation_escalations": state.get("recommendation_escalations", []),
            "bpmn_results": state.get("bpmn_results", []),
        }

        _, ref = store_artifact(_store, f"{uuid4()}/generate_result.json", result)
        if ref:
            return {"generate_result_ref": ref}
        if _store:
            raise RuntimeError("Artifact store available but failed to store generate result")
        return result


@app.post("/api/v1/analyze")
async def analyze(request: Request):
    """Run the full LLM analysis phase (sync)."""
    data = await request.json()
    return _do_analyze(data)


@app.post("/api/v1/analyze-async")
async def analyze_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run analysis in background, POST callback."""
    data = await request.json()
    callback_url = data.pop("callback_url", "")
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(
        _run_analyze_background, data, callback_url, process_instance_id
    )
    return {"status": "accepted"}


def _run_analyze_background(data: dict, callback_url: str, process_instance_id: str):
    iteration = data.pop("review_iteration", 0) or 0
    try:
        result = _do_analyze(data)
    except Exception as e:
        logger.error("Analyze background task failed: %s", e)
        result = {"error": str(e)}

    result["iteration"] = iteration
    post_callback(callback_url, process_instance_id, "analyze-done", result)


@app.post("/api/v1/generate")
async def generate(request: Request):
    """Generate DMN and recommendations from an approved analysis (sync)."""
    data = await request.json()
    return _do_generate(data)


@app.post("/api/v1/generate-async")
async def generate_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept immediately, run generation in background, POST callback."""
    data = await request.json()
    callback_url = data.pop("callback_url", "")
    process_instance_id = data.pop("process_instance_id", "")
    background_tasks.add_task(
        _run_generate_background, data, callback_url, process_instance_id
    )
    return {"status": "accepted"}


def _run_generate_background(data: dict, callback_url: str, process_instance_id: str):
    iteration = data.pop("review_iteration", 0) or 0
    try:
        result = _do_generate(data)
    except Exception as e:
        logger.error("Generate background task failed: %s", e)
        result = {"error": str(e)}

    result["iteration"] = iteration
    post_callback(callback_url, process_instance_id, "generate-done", result)
