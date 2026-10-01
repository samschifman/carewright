# MLflow

MLflow provides end-to-end tracing and observability across the CPG-to-ACP pipeline.

## Deployment

| Environment | How MLflow runs |
|---|---|
| **Local dev** | Container in `compose.yml` (`ghcr.io/mlflow/mlflow`) on port 5000 |
| **OpenShift** | Managed by RHOAI MLflow Operator — no deployment needed from this project |

## Configuration

Set `MLFLOW_TRACKING_URI` to point your services at the MLflow server:

| Environment | Value |
|---|---|
| Local (compose) | `http://mlflow:5000` (set automatically in compose.yml) |
| OpenShift | The MLflow Route URL (set in Helm chart values) |
| CLI tools | `export MLFLOW_TRACKING_URI=http://localhost:5000` |

## What's traced

### acp-writer (FastAPI service)
- All API endpoints via FastAPI auto-instrumentation
- `extract_patient_data` — FHIR Bundle parsing
- `invoke_decisions` / `invoke_decisions_dynamic` — DMN decision evaluation
- `evaluate_jit_dmn` — individual JIT decision engine calls
- `conflict_analyst` — LLM plan-level conflict detection
- `coerce_conflicts` — normalize loosely-shaped conflict entries into the brief contract
- `build_conflict_provenance` — emit an AI-Provenance recording a detected plan conflict
- `plan_conflict_from_provenance` — reconstruct a conflict view-model from a stored Provenance (persisted plans)
- `plan_conflict_from_entry` — reconstruct a conflict view-model from a live planning-brief entry
- `build_careplan` — FHIR CarePlan construction

### cpg-ingester (CLI tools)
- `parse_cpg_pdf` — Docling PDF parsing
- LangGraph pipeline nodes (DMN generation/review, recommendation extraction) via `mlflow.langchain.autolog()`
- `deploy_dmn` — DMN deployment to acp-writer

## Viewing traces

- **Local:** http://localhost:5000
- **OpenShift:** Access via the MLflow Route in the RHOAI dashboard

## Prompt-evaluation experiments

The shared [prompt-evaluation infrastructure](../../shared/prompt-eval/README.md)
uses operator-managed MLflow for official baselines, tuned comparisons, and sealed
holdouts. It verifies artifact upload/download and SHA-256 before executor calls.
See [the experiment guide](../../docs/prompt-evaluation.md) for workspace-aware
configuration, immutable receipts, capture/redaction, exports, and retention.
