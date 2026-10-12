#!/usr/bin/env python3
"""Benchmark runner (RHAIENG-6461).

Runs ``metrics.score_pdf`` over a selected PDF set and writes both a
machine-readable ``report.json`` and a human-readable ``report.md`` to
``working/benchmarks/parsing/reports/`` (gitignored).

Sets:
    --synthetic   the checked-in synthetic corpus (NO network; CI-safe)
    --real        downloaded real CPGs under working/benchmarks/parsing/real/
                  (local only)
    --all         both

The synthetic path has no network dependency beyond Docling's local model
cache and is the target CI runs.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# cpg-ingester/tests/benchmarks/parsing -> repo root
REPO_ROOT = HERE.parents[3]
SYNTHETIC_DIR = HERE / "synthetic"
REAL_DIR = REPO_ROOT / "working" / "benchmarks" / "parsing" / "real"
REPORTS_DIR = REPO_ROOT / "working" / "benchmarks" / "parsing" / "reports"

sys.path.insert(0, str(HERE))
import metrics as metrics_mod  # noqa: E402


def _discover(directory: Path) -> list[tuple[Path, Path | None]]:
    """Return (pdf, groundtruth_or_None) pairs for a directory."""
    pairs = []
    if not directory.exists():
        return pairs
    for pdf in sorted(directory.glob("*.pdf")):
        gt = pdf.with_suffix(".groundtruth.json")
        pairs.append((pdf, gt if gt.exists() else None))
    return pairs


def _run_set(label: str, pairs, *, classify: bool, do_ocr: bool) -> list[dict]:
    rows = []
    for pdf, gt in pairs:
        print(f"[{label}] parsing {pdf.name} (ocr={do_ocr}) ...", flush=True)
        try:
            m = metrics_mod.score_pdf(
                pdf, str(gt) if gt else None, do_ocr=do_ocr, classify_pictures=classify
            )
            m["set"] = label
            m["error"] = None
        except Exception as exc:  # keep the run resilient
            m = {"pdf": pdf.name, "set": label, "error": repr(exc)}
        rows.append(m)
    return rows


def _llm_cfg_from_env():
    """LLM config for figure interpretation. Direct-OpenAI example:

        LITELLM_URL=https://api.openai.com LLM_MODEL=gpt-5.6 \\
        LLM_API_KEY=$OPENAI_API_KEY  run-benchmark.sh --synthetic --interpret
    """
    import os

    return {
        "litellm_url": os.environ.get("LITELLM_URL", "http://localhost:4000"),
        "llm_model": os.environ.get("LLM_MODEL", "default"),
    }


def _run_interpret(label: str, pairs) -> list[dict]:
    """Interpret figures (live vision call) and score vs. ground truth."""
    import interpret_metrics  # noqa: E402 — lazy: pulls in cpg_ingester + LLM

    llm_cfg = _llm_cfg_from_env()
    out = []
    for pdf, gt in pairs:
        print(f"[{label}] interpreting figures in {pdf.name} ...", flush=True)
        try:
            r = interpret_metrics.score_figures(pdf, str(gt) if gt else None, llm_cfg)
            r["set"] = label
            r["error"] = None
        except Exception as exc:  # keep the run resilient
            r = {"pdf": pdf.name, "set": label, "figures": [], "error": repr(exc)}
        out.append(r)
    return out


def _interpret_report(interp_rows: list[dict]) -> str:
    lines = [
        "## Figure interpretation",
        "",
        "Live vision-model recovery of figure *content* (node/edge recovery + "
        "Mermaid validity), scored against the per-figure ground truth. Only "
        "present when run with `--interpret`.",
        "",
        "| set | pdf | figure | class | mermaid | nodes (match/gt) | edges (rec/gt) | ground truth |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in interp_rows:
        if r.get("error"):
            lines.append(f"| {r.get('set','-')} | {r['pdf']} | ERROR | - | - | - | - | `{r['error']}` |")
            continue
        if not r.get("figures"):
            lines.append(f"| {r.get('set','-')} | {r['pdf']} | (no flowchart figures) | - | - | - | - | - |")
            continue
        for f in r["figures"]:
            lines.append(
                f"| {r.get('set','-')} | {r['pdf']} | {f.get('id')} | "
                f"{_fmt(f.get('classification'))} | "
                f"{'valid' if f.get('mermaid_valid') else 'INVALID'} | "
                f"{f.get('node_label_matches')}/{f.get('gt_node_count')} | "
                f"{f.get('recovered_edge_count')}/{f.get('gt_edge_count')} | "
                f"{_fmt(f.get('groundtruth_source'))} |"
            )
    lines.append("")
    return "\n".join(lines)


def _fmt(v):
    return "-" if v is None else v


def _markdown_report(rows: list[dict], generated_at: str, *, ocr: bool) -> str:
    lines = [
        "# CPG Docling Parse-Quality Benchmark Report",
        "",
        f"_Generated: {generated_at}_",
        f"_OCR: {'ON (RapidOCR, forced full-page)' if ocr else 'OFF (baseline)'}_",
        "",
        "Baseline metrics for the Docling parse stage (RHAIENG-6461). "
        "Ground-truth-dependent columns (heading/table recovery) are only "
        "populated for the synthetic set, which ships with sidecars.",
        "",
        "## Metric summary",
        "",
        "| set | pdf | archetype | pages | total_chars | text_yield (chars/pg) | headings | table cells | figures | scanned? |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(
                f"| {r.get('set','-')} | {r['pdf']} | ERROR | - | - | - | - | - | - | - |"
            )
            continue
        hr = r.get("heading_recovery")
        hcell = (
            f"{r.get('headings_found','-')}/{r.get('headings_expected','-')} "
            f"({hr:.0%})" if hr is not None else "-"
        )
        tr = r.get("table_recovery")
        tcell = (
            f"{r.get('cells_detected','-')}/{r.get('cells_expected','-')} "
            f"({tr:.0%})" if tr is not None else str(r.get("cells_detected", "-"))
        )
        lines.append(
            f"| {r.get('set','-')} | {r['pdf']} | {_fmt(r.get('archetype'))} | "
            f"{_fmt(r.get('page_count'))} | {_fmt(r.get('total_chars'))} | "
            f"{_fmt(r.get('text_yield'))} | {hcell} | {tcell} | "
            f"{_fmt(r.get('figure_count'))} | {r.get('likely_scanned')} |"
        )

    # Notes on flowchart docs and any errors
    lines += ["", "## Notes", ""]
    for r in rows:
        if r.get("error"):
            lines.append(f"- **{r['pdf']}**: ERROR — `{r['error']}`")
            continue
        notes = []
        if r.get("headings_missing"):
            notes.append(f"missing headings: {r['headings_missing']}")
        figure_types = r.get("figure_types") or {}
        if figure_types:
            classes = ", ".join(f"{k}×{v}" for k, v in sorted(figure_types.items()))
            notes.append(f"figure classes: {classes}")
        if r.get("flowchart_nodes_expected"):
            notes.append(
                f"flowchart ground truth: {r['flowchart_nodes_expected']} nodes / "
                f"{r['flowchart_edges_expected']} edges (figure detected as "
                f"{r.get('figure_count')} picture(s); content not yet interpreted)"
            )
        if r.get("likely_scanned"):
            notes.append("LIKELY SCANNED (low text yield; triggers conditional OCR)")
        if notes:
            lines.append(f"- **{r['pdf']}**: " + "; ".join(notes))
    lines.append("")
    lines += [
        "## How to read these metrics",
        "",
        "- **text_yield** — extracted characters per page. Very low (< "
        f"{metrics_mod.LIKELY_SCANNED_CPP}) flags a likely-scanned PDF.",
        "- **heading recovery** — fraction of ground-truth section headings found "
        "in the parse output.",
        "- **table cells** — detected structured cells vs. ground-truth cell count.",
        "- **figures** — picture regions Docling detected (content interpretation "
        "is a separate, later stage).",
        "- **scanned?** — heuristic low-yield flag (the trigger the conditional "
        "OCR re-parse consumes).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--synthetic", action="store_true", help="synthetic corpus (CI-safe)")
    g.add_argument("--real", action="store_true", help="downloaded real CPGs (local)")
    g.add_argument("--all", action="store_true", help="both sets")
    ap.add_argument(
        "--no-classify", action="store_true",
        help="disable Docling picture classification (on by default to match the "
             "production parser, which extracts + classifies figures)",
    )
    ap.add_argument(
        "--ocr", action="store_true",
        help="force RapidOCR on for every PDF. Baseline runs keep OCR "
             "off; use this to measure the OCR recovery on scanned fixtures.",
    )
    ap.add_argument(
        "--interpret", action="store_true",
        help="LIVE: interpret figures with a vision model and score content "
             "recovery. Needs LITELLM_URL/LLM_MODEL/LLM_API_KEY. Off by "
             "default so the synthetic run stays offline/CI-safe.",
    )
    args = ap.parse_args()
    classify = not args.no_classify

    pairs_synth = _discover(SYNTHETIC_DIR)
    pairs_real = _discover(REAL_DIR)

    rows: list[dict] = []
    if args.synthetic or args.all:
        if not pairs_synth:
            print("No synthetic PDFs found — run generate_synthetic.py first.", file=sys.stderr)
            return 2
        rows += _run_set("synthetic", pairs_synth, classify=classify, do_ocr=args.ocr)
    if args.real or args.all:
        if not pairs_real:
            print(
                f"No real CPGs found in {REAL_DIR}. Run fetch-benchmark-cpgs.sh "
                "first (real set is local-only, never committed).",
                file=sys.stderr,
            )
            if args.real:
                return 2
        else:
            rows += _run_set("real", pairs_real, classify=classify, do_ocr=args.ocr)

    interp_rows: list[dict] = []
    if args.interpret:
        if args.synthetic or args.all:
            interp_rows += _run_interpret("synthetic", pairs_synth)
        if (args.real or args.all) and pairs_real:
            interp_rows += _run_interpret("real", pairs_real)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    report_json = {
        "generated_at": generated_at,
        "ocr": args.ocr,
        "interpret": args.interpret,
        "likely_scanned_threshold_cpp": metrics_mod.LIKELY_SCANNED_CPP,
        "results": rows,
        "interpretation": interp_rows,
    }
    (REPORTS_DIR / "report.json").write_text(json.dumps(report_json, indent=2) + "\n")
    md = _markdown_report(rows, generated_at, ocr=args.ocr)
    if args.interpret:
        md += "\n" + _interpret_report(interp_rows)
    (REPORTS_DIR / "report.md").write_text(md)

    print(f"\nWrote:\n  {REPORTS_DIR / 'report.json'}\n  {REPORTS_DIR / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
