"""Ingestion pod service — Docling PDF parsing.

Produces: parse_result_ref (stores markdown + docling_json + figures index in
MinIO; figure bitmaps are stored as separate objects and referenced by
image_ref). Security profile: filesystem + ML models, no external network.
"""

import base64
import logging
import os
import tempfile
from uuid import uuid4

import mlflow
from cpg_contracts import get_artifact_store, post_callback, store_artifact
from cpg_contracts.cloud_events import require_callback_url
from cpg_contracts.service_auth import require_service_token
from cpg_ingester.nodes.docling_agent import docling_agent
from fastapi import BackgroundTasks, Depends, FastAPI, File, Request, UploadFile

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", force=True)
logger = logging.getLogger(__name__)

app = FastAPI(title="cpg-ingester-ingestion", version="0.1.0")
_store = get_artifact_store()


@app.get("/health")
def health():
    return {"status": "UP", "service": "ingestion"}


@app.post("/api/v1/parse", dependencies=[Depends(require_service_token('SERVICE_TOKEN_CPG_INGESTION'))])
async def parse_pdf(file: UploadFile = File(...)):
    """Parse a CPG PDF into markdown and Docling JSON (sync)."""
    pdf_bytes = await file.read()
    return _do_parse_bytes(pdf_bytes, file.filename or "input.pdf")


@app.post("/api/v1/parse-async", dependencies=[Depends(require_service_token('SERVICE_TOKEN_CPG_INGESTION', workflow_route=True))])
async def parse_pdf_async(request: Request, background_tasks: BackgroundTasks):
    """Async version: accept JSON with pdf_ref (MinIO key), parse in background."""
    data = await request.json()
    callback_url = require_callback_url(data)
    process_instance_id = data.get("process_instance_id", "")
    pdf_ref = data.get("pdf_ref", "")
    background_tasks.add_task(
        _run_parse_background, pdf_ref, callback_url, process_instance_id
    )
    return {"status": "accepted"}


def _do_parse_bytes(pdf_bytes: bytes, filename: str = "input.pdf") -> dict:
    """Run Docling parsing on raw bytes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, filename)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir)

        result = docling_agent({
            "pdf_path": pdf_path,
            "output_dir": output_dir,
        })

        prefix = str(uuid4())
        figures = _persist_figures(
            result.get("figures", []) or [],
            result.get("figure_images", {}) or {},
            prefix,
        )

        parse_output = {
            "markdown": result.get("markdown", ""),
            "docling_json": result.get("docling_json", {}),
            "figures": figures,
        }

        _, ref = store_artifact(_store, f"{prefix}/parse_result.json", parse_output)
        if ref:
            return {"parse_result_ref": ref}
        if _store:
            raise RuntimeError("Artifact store available but failed to store parse result")
        return parse_output


@mlflow.trace(name="persist_figures")
def _persist_figures(
    figures: list[dict], figure_images: dict[str, str], prefix: str
) -> list[dict]:
    """Move figure bitmaps to the artifact store; annotate the index.

    Each figure's base64-PNG is uploaded as its own object and referenced by
    ``image_ref`` so parse_result.json stays lean. With no artifact store (local
    mode) the bitmap is kept inline as ``image_b64`` instead.
    """
    for fig in figures:
        b64 = figure_images.get(fig.get("id", ""))
        if not b64:
            continue
        if _store:
            raw = base64.b64decode(b64)
            fig["image_ref"] = _store.put_raw(
                f"{prefix}/figures/{fig['id']}.png", raw, "image/png"
            )
        else:
            fig["image_b64"] = b64
    return figures


def _run_parse_background(
    pdf_ref: str, callback_url: str, process_instance_id: str
):
    try:
        if _store and pdf_ref:
            pdf_bytes = _store.get_raw(pdf_ref)
        else:
            post_callback(callback_url, process_instance_id, "parse-done",
                          {"error": "No artifact store or pdf_ref provided"})
            return

        result = _do_parse_bytes(pdf_bytes)
    except Exception as e:
        logger.error("Parse background task failed exception_type=%s", type(e).__name__)
        result = {"error": f"operation-failed:{type(e).__name__}"}

    post_callback(callback_url, process_instance_id, "parse-done", result)
