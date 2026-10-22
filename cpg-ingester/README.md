# Clinical Practice Guideline Ingester

Multi-agent pipeline that parses CPG documents and extracts both computable decision logic (DMN) and narrative recommendations for the acp-writer.

## Architecture

Two-phase pipeline built with LangGraph:

```
Phase 1 — Analysis (sequential):
  Docling Agent → Structure Analyzer → Content Filter →
  Item Identifier ↔ Classification Reviewer → Metadata Extractor

Phase 2 — Generation (parallel with adversarial review):
  ┌─ DMN Creator → Syntax Validator → Semantic Reviewer ─┐
  │                                                       ├→ Assembly → Delivery
  └─ Rec Extractor → Schema Validator → Semantic Reviewer ┘
```

See `dev_docs/design/cpg-ingester-design.md` for the full design rationale.

## Outputs

1. **DMN decision tables** — Computable logic extracted from clinical decision algorithms, delivered to acp-writer's Drools/Kogito decision service (DMN 1.4).
2. **Recommendations** — Non-computable narrative content for RAG retrieval. Contract defined in `shared/cpg_contracts/` (`Recommendation`, `RecommendationBundle`).
3. **Automation templates** — BPMN process templates grounded in identified CPG items and reviewed in the **Automations** tab before delivery to acp-writer.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e . -e ../shared
```

## Usage

### Full pipeline (new)

```bash
# Run the complete multi-agent pipeline
cpg-ingest data/synthetic-hypertension-cpg.pdf -o output/my-run

# With LLM and delivery configuration
cpg-ingest data/synthetic-hypertension-cpg.pdf \
  --litellm-url http://localhost:4000 \
  --model default \
  --acp-writer-url http://localhost:8082
```

Environment variables (alternative to CLI flags):
- `LITELLM_URL` — LiteLLM proxy URL (default: `http://localhost:4000`)
- `LLM_MODEL` — Model name (default: `default`)
- `LITELLM_API_KEY` — API key (default: `sk-change-me`)
- `DMN_PREFLIGHT_URL` — optional decision-service validation endpoint; when set,
  accepted DMN is validated at `<url>?validate_only=true` and failures feed the
  repair loop. Unset means preflight is disabled.
- `BPMN_PREFLIGHT_URL` — optional Kogito automation-template validation endpoint;
  when set, generated and reviewer-edited templates are submitted with
  `validate_only=true`. Unset disables this additional engine preflight.

Docling / figure knobs (all optional; blank or unset means the default):
- `INGESTION_OCR_ENABLED` — run the conditional OCR re-parse for scanned/image-only
  PDFs (default: `true`). Set falsey (`0`/`false`/`no`/`off`) to skip it, e.g. where
  the RapidOCR models are not available in the image.
- `FIGURE_INTERPRETATION_ENABLED` — run vision interpretation of extracted figures
  (flowcharts → Mermaid + description; other figures → description) (default: `true`).
  Set falsey for a cost-sensitive or offline run; figures are then left as-is.
- `FIGURE_INTERPRETATION_MAX_FIGURES` — ceiling on vision calls per document so a
  figure compendium can't fan out into hundreds of frontier-model calls
  (default: `100`). Trivial figures (logos/stamps/…) get a cheap label and don't
  count against this budget.

### Output artifacts

Each run writes to an output directory (`output/<run-id>/`):

| File | Contents |
|---|---|
| `parsed.md` | Docling markdown output |
| `heading-page-map.json` | Section headings with page numbers |
| `section-map.json` | Section classifications (decision/recommendation/both/reference/skip) |
| `abbreviations.json` | Extracted abbreviation dictionary |
| `filtered.md` | Markdown after content filtering |
| `filter-report.json` | What was removed/restored by the filter |
| `manifest.json` | Item manifest with pre-assigned GUIDs |
| `metadata.json` | CPGMetadata |
| `classification-review-*.json` | Adversarial review reports |
| `dmn/*.dmn` | Generated DMN 1.4 XML files |
| `dmn-review-*.json` | DMN semantic review reports |
| `recommendations-*.json` | Extracted recommendations per section |
| `rec-review-*.json` | Recommendation semantic review reports |
| `recommendation-bundle.json` | Assembled RecommendationBundle |
| `assembly-report.json` | Integrity check results |
| `escalated-items.json` | Items needing human review (if any) |
| `delivery-status.json` | API delivery results |
| `run-summary.json` | Overall run summary |

Decision model IDs are stable slugs of the manifest decision name (for example,
`Treatment Recommendation` becomes `treatment-recommendation`). The same ID is
carried on the DMN `<definitions>` element and in `DecisionModelSummary`, so a
redeploy updates the intended model rather than creating a second name-derived
identity. Inputs with explicit temporal language may also carry an
`acp:extraction` JSON annotation naming one of the supported temporal
primitives and its parameters; ordinary inputs omit the annotation.

### Legacy commands (still available)

```bash
# Parse only (Docling)
cpg-parse data/synthetic-hypertension-cpg.pdf -o output

# Deploy an already-generated DMN file to acp-writer
cpg-deploy-dmn output/my-run/dmn/treatment-recommendation.dmn \
  --acp-writer-url http://localhost:8082
```

## Testing

```bash
# Run all unit tests (no LLM required)
pytest tests/

# Run integration tests (requires running LiteLLM)
LITELLM_URL=http://localhost:4000 pytest tests/ -k "Integration"
```

### Mock acp-writer receiver

For testing delivery without a running acp-writer:

```bash
python -m tests.mock_receiver --output-dir ./received --port 8082
```

## Review Strategy

Every extraction step has a two-layer review:

| Stage | Syntax (deterministic) | Semantic (LLM) |
|---|---|---|
| Content Filter | Keyword safety gate | — |
| Item Identification | — | Adversarial classification reviewer |
| Metadata | Pydantic + grading cross-check | — |
| DMN | XML/XSD/FEEL/structure checks | Claim-level source comparison |
| Recommendations | Pydantic/enum/cross-ref checks | Content faithfulness review |

Syntax review retries up to 3 times and semantic review retries up to 2 times,
then escalates to human review.

The manifest-fed DMN benchmark and its offline tests are documented in
[`tests/benchmarks/dmn/README.md`](tests/benchmarks/dmn/README.md).

## Tracing

All pipeline nodes are traced via `mlflow.langchain.autolog()`. Set `MLFLOW_TRACKING_URI` to point to your MLflow server, or traces will be stored locally.

## Web UI

A PatternFly 6 + React 19 SPA for uploading CPGs, monitoring pipeline progress, reviewing AI-extracted artifacts with cyclical feedback gates, and approving delivery.

### Screens

| Screen | Route | Purpose |
|---|---|---|
| Dashboard | `/` | List pipeline runs, status, rerun actions |
| Upload | `/upload` | Drag-and-drop PDF upload |
| Run Detail | `/runs/:id` | Live progress stepper, AI reasoning log, tabbed sub-views |
| Structure Review | `/runs/:id` (tab) | TreeView of CPG sections with classifications |
| Decision Review | `/runs/:id` (tab) | DMN models with decision tables, per-item feedback |
| Recommendation Review | `/runs/:id` (tab) | Recommendations with certainty grades, per-item feedback |
| Automations | `/runs/:id` (tab) | Generated BPMN templates, validation findings, diagrams, and review feedback |
| Assembly Report | `/runs/:id` (tab) | Integrity checks, escalated items |
| Approval & Delivery | `/runs/:id` (tab) | Artifact delivery to acp-writer, per-artifact status |

Review screens support a **cyclical feedback pattern**: approve OR request changes with per-item comments. The pipeline regenerates incorporating feedback, then presents updated artifacts for re-review.

### Tech stack

- **PatternFly 6** — Red Hat design system (Page, Nav, Table, DataList, TreeView, ProgressStepper, CodeBlock, FileUpload, Modal, Skeleton, EmptyState)
- **dmn-js 17** — renders DMN 1.3; the UI remaps the DMN 1.4 namespaces on the copy it passes to the viewer (`src/utils/dmnViewerCompat.ts`). Generated artifacts are DMN 1.4 and are not modified.
- **React 19 + TypeScript** (strict mode)
- **Vite 6** — dev server on port 3003, proxies `/api` to BFF at `localhost:8095`
- **TanStack Query v5** — data fetching with adaptive polling (stops on errors, stops when no active runs)
- **react-router v7** — client-side routing

### Local development

```bash
cd cpg-ingester/ui
npm install
npm run dev          # http://localhost:3003
```

The UI proxies `/api/*` requests to the BFF (default `http://localhost:8095`). Start the BFF first (see below).

### Production build

The UI is built as a multi-stage Docker image: Node 22 Alpine for the build step, nginx-unprivileged for serving. nginx uses `envsubst` to resolve `${BFF_HOST}` at container startup, proxying `/api/` requests to the BFF pod.

```
cpg-ingester/ui/Containerfile       # Multi-stage build
cpg-ingester/ui/nginx.conf       # Template with ${BFF_HOST} substitution
```

## Backend-for-Frontend (BFF)

A FastAPI service that mediates between the SPA and SonataFlow/MinIO. The UI talks **only** to the BFF — never directly to SonataFlow or MinIO.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/runs` | List pipeline runs |
| `GET` | `/api/v1/runs/{id}` | Run detail with steps, artifacts, review state |
| `POST` | `/api/v1/upload` | Accept PDF upload, start pipeline |
| `POST` | `/api/v1/runs/{id}/review/{gate}` | Submit review decision (approve / request changes) |
| `POST` | `/api/v1/runs/{id}/rerun` | Re-run a completed or failed pipeline |
| `GET` | `/api/v1/runs/{id}/artifacts/{path}` | Fetch artifact from storage |
| `GET` | `/health` | Health check |

### Mock mode

When `SONATAFLOW_URL` and `MINIO_ENDPOINT` are **not** configured, the BFF automatically loads mock data from `cpg_ingester/mocks/`. This provides 5 sample runs at different pipeline stages so the UI can be developed and demonstrated without real infrastructure.

Mock data lives in its own package:

```
src/cpg_ingester/mocks/
  ├── __init__.py
  ├── data.py       # Mock runs, artifacts, decisions, recommendations
  └── router.py     # FastAPI router with mock endpoint implementations
```

The BFF uses a guarded import — if the mocks package is absent (e.g., stripped from a production image), the BFF logs a warning and starts without mock endpoints.

### Running locally

```bash
cd cpg-ingester
python3 -m venv .venv && source .venv/bin/activate
pip install -e . -e ../shared
pip install fastapi uvicorn python-multipart

uvicorn cpg_ingester.services.bff:app --host 0.0.0.0 --port 8095 --reload
```

Without `SONATAFLOW_URL` / `MINIO_ENDPOINT` set, the BFF starts in mock mode automatically.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SONATAFLOW_URL` | _(empty)_ | SonataFlow base URL. When absent, enables mock mode |
| `SONATAFLOW_WORKFLOW_NAME` | `cpgingester` | Workflow path/ID; the CareWright operator sets this to the generated SonataFlow name |
| `ACP_WRITER_BFF_URL` | _(empty)_ | Care Plan Writer BFF address; enables downstream notification when set |
| `MINIO_ENDPOINT` | _(empty)_ | MinIO endpoint. When absent, enables mock mode |
| `MLFLOW_TRACKING_URI` | _(empty)_ | MLflow tracing endpoint |

## Deployment

See [`deploy/README.md`](../deploy/README.md) for the full cluster deployment guide.

### Quick reference

```bash
# Full deploy (builds + Helm + OpenShell sandboxes)
cpg-ingester/deploy/deploy.sh --config deploy/config/cluster.env

# Redeploy without rebuilding images
cpg-ingester/deploy/deploy.sh --skip-build --tag <sha> --config deploy/config/cluster.env

# Verify (includes BFF minio/sonataflow dependency check)
cpg-ingester/deploy/verify.sh --config deploy/config/cluster.env

# Teardown (preserves Secrets and ImageStreams)
cpg-ingester/deploy/teardown.sh --config deploy/config/cluster.env
```

### Pod-split architecture

In cluster mode (`openshellMode: true`), cpg-ingester runs as 4 OpenShell sandboxes + 2 Helm pods:

| Sandbox/Pod | Service | Role |
|---|---|---|
| `sb-ingestion` | `cpg-ingester-ingestion` | Docling PDF parsing |
| `sb-llm-analysis` | `cpg-ingester-llm-analysis` | LLM structure analysis + DMN/recommendation generation |
| `sb-assembly` | `cpg-ingester-assembly` | Artifact assembly |
| `sb-delivery` | `cpg-ingester-delivery` | Publish to MinIO |
| `cpg-ingester-bff` (Helm) | `cpg-ingester-bff` | BFF — upload, run management, SonataFlow trigger |
| `cpg-ingester-ui` (Helm) | `cpg-ingester-ui` | Web UI (UBI nginx) |

### BFF dependencies

The BFF requires `MINIO_ENDPOINT` and `SONATAFLOW_URL` to be set. Without them it runs in **mock mode** (fake upload, no workflow trigger). The Helm chart sets these by default. The `verify.sh` script checks for `minio=true` and `sonataflow=true` in the BFF health response.

### SonataFlow workflow

The `cpgingester` SonataFlow workflow orchestrates: Parse → Analyze → ReviewManifest → Generate → ReviewArtifacts → Assemble → Deliver. The workflow and its props CM (`cpgingester-props.yaml`) are applied automatically by `deploy.sh`.

### OpenShift (Helm only)

All pods are managed by one Helm release. The deploy scripts handle all `--set` overrides:

```bash
helm upgrade --install cpg-ingester deploy/chart/ \
  --namespace $NAMESPACE \
  --set image.namespace=$NAMESPACE
```

### Docker Compose

Both services are in the root `compose.yml` under the `ingester-pods` profile:

```bash
podman-compose --profile ingester-pods up cpg-ingester-bff cpg-ingester-ui
```

| Service | Port | Notes |
|---|---|---|
| `cpg-ingester-bff` | 8095 | FastAPI, mock mode by default |
| `cpg-ingester-ui` | 3003 | nginx serving SPA, proxies `/api/` to BFF |

## Data

- `data/synthetic-hypertension-cpg.pdf` — Synthetic CPG for testing
- `data/golden/` — Golden DMN files for validation
