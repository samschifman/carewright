#!/usr/bin/env python3
"""Figure-interpretation metrics for the CPG benchmark (RHAIENG-6461).

Like ``metrics.py`` (which imports the production *parser*), this module imports
the production figure pipeline — ``docling_agent`` + ``figure_interpreter`` —
because the thing we want to measure IS the production interpreter's output.
Re-implementing the vision call here would score a different thing.

It is **live and opt-in**: interpreting a figure calls a vision LLM, so this
runs only when the benchmark is invoked with ``--interpret`` and the LLM env
(``LITELLM_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY``) is set. The default
``--synthetic`` benchmark stays offline and CI-safe.

Scoring (per flowchart figure, vs. the sidecar ground truth):
    mermaid_valid        did the returned Mermaid pass validation
    gt_node_count        ground-truth node count for this figure
    recovered_node_count nodes the model returned
    node_label_matches   normalized substring matches (recall proxy)
    gt_edge_count / recovered_edge_count
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _figure_groundtruth(gt: dict, fig: dict) -> tuple[dict, str]:
    """Pick the ground-truth block for one figure.

    Multi-figure sidecars carry a ``figures[]`` array keyed by reading-order
    ``index``; single-figure sidecars use top-level ``flowchart_*`` keys.
    """
    per_fig = gt.get("figures")
    if per_fig:
        idx = fig.get("reading_order_index")
        if not isinstance(idx, int):
            try:
                idx = int(str(fig.get("id", "fig-001")).split("-")[1]) - 1
            except (IndexError, ValueError):
                idx = 0
        match = [f for f in per_fig if f.get("index") == idx]
        block = match[0] if match else per_fig[min(idx, len(per_fig) - 1)]
        return block, f"figures[{block.get('index')}] ({block.get('heading')})"
    return gt, "top-level flowchart_*"


def _score_one(fig: dict, gt: dict) -> dict:
    interp = fig.get("interpretation") or {}
    block, source = _figure_groundtruth(gt, fig)

    gt_nodes = [_norm(n) for n in block.get("flowchart_nodes", []) or []]
    got_nodes = [_norm(n) for n in (interp.get("nodes") or [])]
    matches = sum(1 for g in gt_nodes if g and any(g in c or c in g for c in got_nodes if c))

    return {
        "id": fig.get("id"),
        "classification": fig.get("classification"),
        "groundtruth_source": source,
        "mermaid_valid": interp.get("mermaid_valid"),
        "gt_node_count": len(gt_nodes),
        "recovered_node_count": len(got_nodes),
        "node_label_matches": matches,
        "gt_edge_count": len(block.get("flowchart_edges", []) or []),
        "recovered_edge_count": len(interp.get("edges") or []),
        "has_description": bool(interp.get("description")),
    }


def score_figures(pdf_path, groundtruth_path, llm_cfg: dict) -> dict:
    """Interpret a PDF's figures with the production node and score them.

    ``llm_cfg`` supplies ``litellm_url`` / ``llm_model``; the vision call reads
    its credential from the process environment (see ``cpg_contracts.get_llm``). Returns a dict with a
    per-figure ``figures`` list of score rows.
    """
    import json

    from cpg_ingester.nodes.docling_agent import docling_agent
    from cpg_ingester.nodes.figure_interpreter import figure_interpreter

    pdf_path = Path(pdf_path)
    with tempfile.TemporaryDirectory() as tmp:
        parsed = docling_agent({"pdf_path": str(pdf_path), "output_dir": tmp})
        state = {
            "markdown": parsed["markdown"],
            "docling_json": parsed["docling_json"],
            "figures": parsed["figures"],
            "figure_images": parsed["figure_images"],
            "output_dir": tmp,
            **llm_cfg,
        }
        interpreted = figure_interpreter(state)

    figures = interpreted["figures"]
    gt = {}
    if groundtruth_path and Path(groundtruth_path).exists():
        gt = json.loads(Path(groundtruth_path).read_text())

    # Only score figures that produced a flowchart interpretation (nodes/edges).
    rows = [
        _score_one(f, gt)
        for f in figures
        if (f.get("interpretation") or {}).get("nodes") is not None
    ]
    return {"pdf": pdf_path.name, "figure_count": len(figures), "figures": rows}
