# AGENTS.md

Rules, conventions, and architectural boundaries for AI coding agents working in this repository.

## Project Overview

This is a multi-agent system that transforms Clinical Practice Guidelines (CPGs) into patient-specific, FHIR-compliant, actionable care plans on the Red Hat AI platform. See [`dev_docs/`](dev_docs/) for the full project proposal and architecture slides.

## Repository Structure

```
cpg-to-acp/
├── cpg-ingester/    # Steps 1-3: Parse CPGs, extract DMN decision logic, extract recommendations
├── acp-writer/      # Steps 4-5: Patient data integration, care plan composition, clinician review UI
├── automation/      # Step 6: Execute BPMN process definitions produced by acp-writer
├── carewright-operator/ # Kubernetes operator for cluster-managed pipelines
├── mock-EHR/        # HAPI FHIR server + simple EHR client (dev/test infrastructure)
├── platform/        # Shared infrastructure services (MaaS, MLflow)
├── shared/          # Cross-component contracts and utilities (use sparingly)
├── docs/            # User-facing documentation (architecture, security, deployment)
└── dev_docs/        # Internal dev docs: design docs, spikes, project plan (point-in-time references)
```

## Architectural Boundaries

These are hard rules. Do not violate them.

### Component Ownership

- **`cpg-ingester`** has two outputs: (1) DMN decision tables for computable logic, and (2) recommendations and other non-computable content destined for a vector store in `acp-writer`. It must not be coupled to the decision engine runtime or vector store implementation. It interacts with downstream services only through API/MCP. The recommendation contract is defined in `shared/cpg_contracts/recommendations.py` — see `dev_docs/design/contract-proposal-ingester-writer.md` for the full design rationale.
- **`acp-writer`** owns the Drools/Kogito decision engine runtime and the vector store. Both are internal implementation details of `acp-writer` — they are not platform services. It deploys and executes DMN. It produces two outputs: FHIR CarePlans (to the FHIR server) and BPMN (to automation). The API contract is defined in `acp-writer/api/openapi.yaml` (REST) and `acp-writer/api/mcp-tools.json` (MCP tools). Callers provide patient data directly — acp-writer does not query FHIR servers. Plan-level conflicts are detected by an LLM `conflict_analyst` node and recorded as FHIR Provenance resources (AI-Provenance profile) with a single `careplan-conflict-detected` marker on the CarePlan — see [`docs/ai-transparency.md`](docs/ai-transparency.md).
- **`automation`** is a downstream runtime service that executes BPMN process definitions. It does not orchestrate other services.
- **`carewright-operator`** owns the Kubernetes API and reconciliation for the `CPGIngester`, `CarePlanWriter`, and `SandboxRequest` custom resources. Its Go module and container build context stay inside this directory. Keep its cluster-managed deployment path separate from the existing component Helm/deploy scripts unless a migration explicitly changes those contracts.
- **`mock-EHR`** is development/test infrastructure. It is not application logic.
- **`platform`** holds shared infrastructure services (MaaS, MLflow) that multiple application components consume. These are platform-level dependencies, not application logic. On OpenShift AI, these are typically configured rather than deployed; for local dev, this directory contains the deployment artifacts.
- **`shared`** holds cross-component contracts and utilities. Use it sparingly to prevent coupling between components.

### Standards as Contracts

Each component boundary uses a standards-based contract:

| Boundary | Standard | Producer | Consumer |
|---|---|---|---|
| Decision logic | **DMN** | cpg-ingester | acp-writer |
| Recommendations | **cpg-contracts** (`Recommendation`, `RecommendationBundle`) | cpg-ingester | acp-writer (vector store) |
| Patient data | **FHIR** (IPS) | mock-EHR | acp-writer |
| Care plans | **FHIR** (CarePlan) | acp-writer | mock-EHR |
| Process automation | **BPMN** | acp-writer | automation |
| Automation templates | **cpg-contracts** (`AutomationTemplate`) | cpg-ingester | acp-writer (registry) |

Producers must not assume a specific consumer runtime. Consumers are pluggable behind the standard.

### Deployment

- **Cluster deployment guide:** [`deploy/README.md`](deploy/README.md) — the full procedure for deploying to OpenShift with MaaS + OpenShell. Follow it for any cluster work.
- **Component-specific deployment:** each component has its own `deploy/deploy.sh`, `deploy/verify.sh`, and `deploy/teardown.sh`. See each component's README for details.
- Each component owns its own deployment artifacts (Containerfiles, Helm charts, OpenShell policies, router fragments, SonataFlow workflows + props CMs).
- A root-level `deploy/` directory holds only shared infrastructure (MinIO, openshell-router, MCP gateway CRs) and thin orchestrators (`deploy-all.sh`, `teardown-all.sh`, `verify-all.sh`). It must not contain component-specific knowledge.
- **Secrets:** managed via `deploy/setup/setup-secrets.sh` → K8s Secrets → `secretKeyRef` (Helm pods) / `read_secret` + `--env` (OpenShell sandboxes). Never commit secrets. See [`deploy/README.md` § Secrets](deploy/README.md#secrets) for the full inventory and rotation procedure.
- **OpenShell provisioning:** `deploy/setup/setup-openshell.sh` provisions the OpenShell gateway + SonataFlow platform per-namespace. Creates cluster-scoped RBAC that must be cleaned up via `teardown-all.sh --full-wipe` when abandoning a namespace.
- **Container registry:** All images (project-built and vendor mirrors) are hosted on `quay.io/carewright`. BuildConfigs push via a `carewright-quay-push-secret` push secret. GitHub Actions uses a Carewright Quay robot credential to publish changed component images. No images are pulled from the OpenShift internal registry or Docker Hub at deploy time.
- **Operator image:** `carewright-operator/` has its own multi-platform image workflow in `.github/workflows/publish-operator-image.yml`; it publishes `quay.io/carewright/carewright-operator:<short-commit-sha>` when that directory changes on `main` (or by manual dispatch). Operator CI is in `.github/workflows/carewright-operator-ci.yml`.
- **Images:** SHA-tagged (`git rev-parse --short HEAD`), `imagePullPolicy: Always`. Never use `--skip-build` without an explicit `--tag` matching built images.
- **Local dev** uses `compose.yml` (unchanged by the cluster framework).

## Development Rules

### Code Quality
- Do not introduce security vulnerabilities (OWASP top 10). This project handles clinical data.
- **Never commit secrets** (API keys, passwords, tokens) to the repository. Use environment variables or Kubernetes Secrets. The repo has two layers of protection:
  - **GitHub Push Protection** — server-side scanning that blocks pushes containing known secret patterns.
  - **Gitleaks pre-commit hook** (`.githooks/pre-commit`) — local scanning before each commit. Activate with: `brew install gitleaks && git config core.hooksPath .githooks`
- Keep components independent. Avoid adding cross-component dependencies unless the contract goes through `shared/`.
- Prefer standard interfaces (MCP, REST, FHIR, DMN, BPMN) over proprietary integrations.
- Do not put guideline-specific clinical language, thresholds, drug names, intervals, codes, or URLs in prompts or source. Look up codes rather than inventing them; report reviewer rejection patterns instead of patching clinical content into prompts or source.

### Observability
- All new functions that perform meaningful work (API calls, LLM invocations, data transformations, external service calls) must be traced with `@mlflow.trace`. This is not optional.
- `acp-writer` uses `mlflow.fastapi.autolog()` for endpoint tracing and `mlflow.langchain.autolog()` for LangGraph pipeline tracing.
- `cpg-ingester` uses `mlflow.langchain.autolog()` for automatic LangGraph/LLM call tracing.
- Set `MLFLOW_TRACKING_URI` in environment configuration for both local (compose.yml) and OpenShift (Helm chart) deployments.
- See `platform/mlflow/README.md` for the full tracing inventory.

### Documentation
- `docs/` contains user-facing documentation (architecture, security, deployment guides). Keep this up to date when making changes that affect how the system works or is deployed.
- `dev_docs/` contains internal development documents (design docs, spikes, project plan). Point-in-time references — may not reflect current state.
- Each component has its own README describing its purpose, setup, and usage.
- Use **Mermaid** for all diagrams in documentation. Do not use ASCII art, image files, or external diagramming tools for new diagrams.

### Agent Memory (MemoryHub)
- **Optional but recommended.** If a `memoryhub` MCP server is configured, register at session start: call `register_session` with the API key from your MCP server configuration, then `memory(action="search", query="...", project_id="cpg-to-acp")` to load relevant context. If MemoryHub is not configured, skip this — it is not required for contributing.
- **Use MemoryHub** (when available) for project-level memory: meeting decisions, architecture context, team agreements, and anything that should persist across sessions and be accessible to all team agents.
- **Project ID:** `cpg-to-acp`

## Technology Context

Key technologies referenced in this project (all subject to change):

- **Document parsing:** Docling
- **Decision engine:** Drools / Kogito (Apache KIE), DMN 1.4 (latest supported at conformance level 3; upgrade to 1.5 when Drools adds support)
- **AI transparency:** HL7 AI Transparency on FHIR IG (AI-Device, AI-Provenance, AI-InputPrompt, AI-ModelCard, AIAST labels, AIconfidence, human-verifier agents) — see `docs/ai-transparency.md`
- **FHIR server:** HAPI FHIR
- **Agent framework:** LangGraph (see `dev_docs/spikes/spike-agent-framework.md`)
- **Observability:** MLflow (tracing, experiment tracking)
- **LLM inference:** vLLM, MaaS
- **Process automation:** Pluggable (Ansible, SonataFlow, BPMN engine)
- **Vector store:** Pluggable (Milvus, pgvector, etc.)
- **Platform:** Red Hat AI (OpenShift) with OpenShell

## License

Apache License, Version 2.0
