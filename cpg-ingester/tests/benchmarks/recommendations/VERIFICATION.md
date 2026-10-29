# Evaluator-only verification

Verified on 2026-10-08 from the uncommitted recommendation branch based on
upstream `3a242f9`. This is **not a frozen release or live quality baseline**.

## Results

- 52 new evaluator tests pass: selected-case validation, digests, matching,
  scoring, real production-node capture, module CLI, shared runner receipts,
  fail-closed official/local gating, and safe local MLflow spans.
- 65 unchanged shared prompt-evaluation infrastructure tests pass.
- 111 tests pass when the new evaluator suite is run with the unchanged
  `test_rec.py` and `test_rec_semantic_reviewer.py` regressions.
- Full ingester suite: **483 passed, 16 failed, 30 skipped, 9 deselected**.
  Failures below require uncached Hugging Face Docling models; verification used
  `HF_HUB_OFFLINE=1` to prevent downloading models or contacting external services.
- New evaluator Python files pass Ruff; production prompts/contracts are unchanged.
- Fresh read-only review found three Important issues. Each was reproduced RED
  and fixed: module/canonical factory binding, production `TBD` source handling,
  and leading decimal/case-sensitive unit normalization. Per-CPG fidelity metrics
  were also added. No reviewer was asked to re-review after the regression fix pass.

The local tracing integration uses a temporary SQLite MLflow store, verifies
safe `execute`/`score` spans and checks no source text occurs in span data. Raw
content remains in redacted immutable capture artifacts. This does not certify
RHOAI connectivity; PR #189's platform verification is separately documented.

One upstream SQLAlchemy 2.1 deprecation warning occurred in MLflow's trace-query
mapping. The temporary environment used MLflow 3.16.1 and Python 3.12.11.
Installing Docling 2.113.0 resolved docling-core 2.101.0; the repository currently
declares docling-core 2.87.1. This test environment is therefore not claimed as a
fully reproduced deployment dependency lock.

## Commands

From `cpg-ingester`, with the dedicated environment activated:

```bash
export PYTHONPATH=tests:src:../shared/src:../shared/prompt-eval/src
export MLFLOW_DISABLE_AGENT_HINT=1
export MLFLOW_TRACKING_URI=sqlite:////tmp/carewright-rec-eval-tests.sqlite
python -m pytest tests/test_rec_eval_dataset.py tests/test_rec_eval_scoring.py tests/test_rec_eval_adapter.py
HF_HUB_OFFLINE=1 python -m pytest
```

From the repository root:

```bash
PYTHONPATH=shared/prompt-eval/src python -m pytest shared/prompt-eval/tests
ruff check cpg-ingester/tests/benchmarks/recommendations \
  cpg-ingester/tests/rec_eval_fixtures.py cpg-ingester/tests/test_rec_eval_dataset.py \
  cpg-ingester/tests/test_rec_eval_scoring.py cpg-ingester/tests/test_rec_eval_adapter.py
```

## Full-suite failures

All are model-cache/offline failures outside the new evaluator:

- `test_docling_agent.py::TestDoclingAgent::test_produces_markdown`
- `test_docling_agent.py::TestDoclingAgent::test_produces_docling_json`
- `test_docling_agent.py::TestDoclingAgent::test_docling_json_has_provenance`
- `test_docling_agent.py::TestDoclingAgent::test_writes_artifacts`
- `test_docling_agent.py::TestDoclingAgent::test_markdown_contains_expected_content`
- `test_docling_agent.py::TestDoclingAgent::test_returns_figure_index`
- `test_docling_agent.py::TestDoclingAgent::test_docling_json_has_no_embedded_bitmaps`
- `test_docling_agent.py::TestFigureExtraction::test_extracts_classified_flowchart_figure`
- `test_docling_agent.py::TestFigureExtraction::test_figure_bitmap_is_valid_png`
- `test_docling_agent.py::TestMultiFigurePlacement::test_extracts_two_figures_in_order`
- `test_docling_agent.py::TestMultiFigurePlacement::test_pictures_anchored_between_correct_headings`
- `test_docling_agent.py::TestConditionalOcr::test_ocr_recovers_scanned_text`
- `test_docling_agent.py::TestConditionalOcr::test_ocr_disabled_leaves_text_unrecovered`
- `test_structure_analyzer.py::TestStructureAnalyzerWithMockedLLM::test_extracts_sections_from_real_docling`
- `test_structure_analyzer.py::TestStructureAnalyzerWithMockedLLM::test_with_mocked_llm`
- `test_structure_analyzer.py::TestStructureAnalyzerWithMockedLLM::test_abbreviation_extraction_on_real_content`
