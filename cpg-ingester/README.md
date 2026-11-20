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

The UI needs an operator session before it shows anything: without one `/api/v1/session` returns 401 and the page stops at the session gate. [Local operator session](#local-operator-session) shows the two ways to get one on your machine.

### Production build

The UI is built as a multi-stage Docker image: Node 22 Alpine for the build step, nginx-unprivileged for serving. nginx uses `envsubst` to resolve `${BFF_HOST}` at container startup, proxying `/api/` requests to the BFF pod.

```
cpg-ingester/ui/Containerfile       # Multi-stage build
cpg-ingester/ui/nginx.conf       # Template with ${BFF_HOST} substitution
```

### Async service callbacks

When running async services by hand, set `WORKFLOW_CALLBACK_ORIGIN` to the
SonataFlow origin reachable **from those services**, for example
`WORKFLOW_CALLBACK_ORIGIN=http://localhost:8097`. A comma-separated list of
origins is supported. Each entry must be `http://host[:port]` or
`https://host[:port]`; paths and credentials are not allowed. A callback must
match a configured scheme, host, and effective port. Missing configuration or
a disallowed callback returns HTTP 400 before background work starts; callback
delivery rechecks the origin and never follows redirects.

`compose.orchestrated.yml` sets `http://sonataflow-devmode:8080` on each async
service. Operator-managed deployments also need their workflow Service origin
in this variable; the operator wiring is delivered separately in Track B6.

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

# The UI origin, a non-Secure cookie for plain-HTTP localhost, and the
# development auto-session (mock mode only). See "Local operator session".
export CPG_PUBLIC_ORIGIN=http://localhost:3003
export SESSION_COOKIE_SECURE=false
export CPG_DEV_AUTO_SESSION=true

uvicorn cpg_ingester.services.bff:app --host 127.0.0.1 --port 8095 --reload
```

### Local operator session

Every run, upload and review route needs an operator session, and the UI stops at a session gate until it has one. On a cluster the session comes from a launch link minted inside the BFF pod (see [Operator browser sessions](#operator-browser-sessions)). Locally there are two ways.

**Development auto-session (mock mode).** With `CPG_DEV_AUTO_SESSION=true` the BFF creates an operator session the first time the UI asks for one. It applies only in mock mode (no `SONATAFLOW_URL`), and the BFF refuses to start with it unless `CPG_PUBLIC_ORIGIN` is a localhost origin, so it cannot be enabled on a cluster. Three settings must agree:

| Variable | Local value | Why |
|---|---|---|
| `CPG_PUBLIC_ORIGIN` | `http://localhost:3003` | The exact origin the browser shows for the UI (Vite dev server and the Compose UI both use port 3003). State-changing requests from any other origin are refused. Use `localhost`, not `127.0.0.1`, in the browser as well. |
| `SESSION_COOKIE_SECURE` | `false` | The session cookie is `Secure` by default, which a plain-HTTP page will not send back. `false` is accepted only with a localhost HTTP origin. |
| `CPG_DEV_AUTO_SESSION` | `true` | Creates the session without a launch link. Off by default. |

Standalone: export the three values before `uvicorn` as shown above, run `npm run dev` in `cpg-ingester/ui`, and open `http://localhost:3003`.

Compose already sets the first two for `cpg-ingester-bff`; pass the third when you start it:

```bash
CPG_DEV_AUTO_SESSION=true podman-compose --profile ingester-pods up cpg-ingester-bff cpg-ingester-ui
# then open http://localhost:3003
```

**Local launch link (any mode).** When the BFF is not in mock mode (for example the orchestrated Compose profile, where `SONATAFLOW_URL` is set) the auto-session does not apply. Mint a single-use link against the local BFF with its service token, then open it through the UI origin so the cookie is set for the page you will use:

```bash
# Compose default; use your own value if you overrode it. Standalone: export
# SERVICE_TOKEN_CPG_BFF to any value before starting uvicorn, and use it here.
export SERVICE_TOKEN_CPG_BFF="${SERVICE_TOKEN_CPG_BFF:-carewright-local-service-token-cpg-bff-dev-only}"

curl -s -X POST http://localhost:8095/api/v1/admin/launch-links \
  -H "X-Service-Token: $SERVICE_TOKEN_CPG_BFF"
# {"url": "/api/v1/launch/<token>", "expiresIn": 600}

# Open http://localhost:3003/api/v1/launch/<token> in the browser.
```

The link works once and expires after ten minutes. Sign out from the UI to end the session.

Without `SONATAFLOW_URL` / `MINIO_ENDPOINT` set, the BFF starts in mock mode automatically.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SONATAFLOW_URL` | _(empty)_ | SonataFlow base URL. When absent, enables mock mode |
| `SONATAFLOW_WORKFLOW_NAME` | `cpgingester` | Workflow path/ID; the CareWright operator sets this to the generated SonataFlow name |
| `ACP_WRITER_BFF_URL` | _(empty)_ | Care Plan Writer BFF address; enables downstream notification when set |
| `MINIO_ENDPOINT` | _(empty)_ | MinIO endpoint. When absent, enables mock mode |
| `MLFLOW_TRACKING_URI` | _(empty)_ | MLflow tracing endpoint |
| `CPG_PUBLIC_ORIGIN` | _(empty)_ | Exact origin of the UI as the browser sees it; state-changing requests from other origins are refused |
| `SESSION_COOKIE_SECURE` | `true` | `false` only with a localhost HTTP `CPG_PUBLIC_ORIGIN` |
| `CPG_DEV_AUTO_SESSION` | `false` | Local mock mode only: create an operator session without a launch link. The BFF refuses to start with it off a localhost origin |
| `SERVICE_TOKEN_CPG_BFF` | _(empty)_ | Credential for `POST /api/v1/admin/launch-links` (mint a launch link) |

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
CPG_DEV_AUTO_SESSION=true podman-compose --profile ingester-pods up cpg-ingester-bff cpg-ingester-ui
```

`CPG_DEV_AUTO_SESSION=true` gives the UI an operator session in mock mode; without it, mint a local launch link. See [Local operator session](#local-operator-session).

| Service | Port | Notes |
|---|---|---|
| `cpg-ingester-bff` | 8095 | FastAPI, mock mode by default |
| `cpg-ingester-ui` | 3003 | nginx serving SPA, proxies `/api/` to BFF |

## Data

- `data/synthetic-hypertension-cpg.pdf` — Synthetic CPG for testing
- `data/golden/` — Golden DMN files for validation

## Operator browser sessions

Mint a ten-minute, single-use link with `carewright-operator/scripts/ingester-launch-link.sh <namespace> <cpgingester-name>` (run from the repository root). The service token stays inside the BFF pod. Treat the printed URL as a bearer credential; do not paste it into logs or public chat. Following the link consumes it before session creation, sets an HttpOnly operator cookie and redirects to `/`. Responses and the UI set `Referrer-Policy: no-referrer`; nginx API access logging is off. Configure upstream ingress logging to avoid recording launch paths.

Set exact `CPG_PUBLIC_ORIGIN`, `SERVICE_TOKEN_CPG_BFF` and `CPG_SIGNING_KEY`. The UI obtains a CSRF token from `/api/v1/session` and sends it on upload/review/cancel/rerun/logout. Reviews require current `reviewRound` and `artifactRevision`; completion time is server assigned. Stale rounds fail with 409; confirmed duplicates send no second event. All operator reads require a session. `CPG_DEV_AUTO_SESSION=true` enables an explicitly logged development auto-session only when SonataFlow is absent; it is off by default. Use one BFF replica/worker, with Recreate updates. See [security](../docs/security.md).

Review-gate cancellation requires both SonataFlow and the artifact store: without MinIO it returns `503 approval-store-unavailable`, preserving the signed round binding.
