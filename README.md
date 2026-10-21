# Carewright: CPG to Actionable Care Plans

> **Note:** This project is just starting. None of the code in this repo is finalized. Treat it all as experimental and expect volatile and frequent changes.

A multi-agent system that transforms Clinical Practice Guidelines (CPGs) into patient-specific, FHIR-compliant, actionable care plans — built on the Red Hat AI platform.

CPGs are published as narrative documents (PDFs, sometimes hundreds of pages) containing decision logic, recommendations, dosing tables, risk assessments, and care pathways. Today, translating a CPG into actionable care for a specific patient is manual, error-prone, and depends on individual clinician recall. This project bridges that gap with AI while keeping clinical decisions deterministic, auditable, and governed.

**See the project website:** [https://samschifman.github.io/carewright/](https://samschifman.github.io/carewright/)

## Architecture

The system has four application components connected by standards-based contracts, plus shared platform services:

```
  CPG (PDF)
      │
      ▼
┌─────────────┐   DMN    ┌─────────────┐   FHIR CarePlan   ┌──────────┐
│ cpg-ingester │ ───────► │  acp-writer  │ ────────────────► │ mock-EHR │
│              │  Recs    │              │                   └──────────┘
│              │ ───────► │  (Drools /   │   BPMN
└─────────────┘          │   Kogito)    │ ────────────────► ┌────────────┐
                         └─────────────┘                   │ automation  │
                               ▲                           └────────────┘
                               │
                          Patient Data
                           (FHIR IPS)
```

**Standards as contracts:** DMN is the interface between `cpg-ingester` and `acp-writer` for decision logic. FHIR is the interface for patient data and care plans. BPMN is the interface between `acp-writer` and `automation`. Each component is pluggable behind its standard — swap the runtime without changing the producer. Recommendations use a custom contract defined in `shared/cpg_contracts/` (`Recommendation`, `RecommendationBundle`) — no established standard exists for this boundary, so we defined one with normalized certainty grades, cross-references, and source provenance.

## Components

| Directory | Purpose |
|---|---|
| [`cpg-ingester/`](cpg-ingester/) | Parses CPG documents (via Docling) and produces two outputs: (1) DMN decision tables for computable logic, and (2) recommendations and other non-computable content for the acp-writer's vector store. Includes a PatternFly 6 / React 19 UI for upload, progress monitoring, and human-in-the-loop review with cyclical feedback gates, plus a FastAPI BFF that mediates between the UI and SonataFlow/MinIO. |
| [`acp-writer/`](acp-writer/) | Composes patient-specific care plans by invoking DMN decision services (Drools/Kogito), retrieving recommendations from its vector store, and integrating patient data from FHIR. Outputs FHIR CarePlans and BPMN for automatable activities. The decision engine and vector store are internal implementation details. Includes the clinician review UI (SMART on FHIR). |
| [`automation/`](automation/) | Executes BPMN process definitions produced by the acp-writer. The runtime is pluggable — Ansible playbooks, SonataFlow, or any BPMN-conformant engine. |
| [`carewright-operator/`](carewright-operator/) | Kubernetes operator for cluster-managed `CPGIngester` and `CarePlanWriter` resources. It provisions OpenShell sandboxes for processing components and reconciles their Kubernetes services, UIs, routes, and SonataFlow workflows. See its [deployment and prerequisite guide](carewright-operator/README.md#bootstrap-prerequisites-on-openshift). |
| [`mock-EHR/`](mock-EHR/) | Mock EHR built on Medplum (FHIR R4, OAuth, SMART on FHIR). Includes a clinical-facing React UI ("CareView EHR") and an IPS Viewer SMART app. Used for development and demonstration. See [`mock-EHR/README.md`](mock-EHR/README.md). |
| [`platform/`](platform/) | Shared infrastructure services (MaaS, MLflow) consumed by multiple application components. On OpenShift AI these are platform capabilities; for local dev this directory provides equivalent deployments. |
| [`shared/`](shared/) | Shared contracts and utilities across components. Used sparingly to prevent coupling. |
| [`docs/`](docs/) | User-facing documentation: architecture, security, deployment guides. |
| [`dev_docs/`](dev_docs/) | Internal development documents: design docs, spikes, project plan. Point-in-time references — may not reflect current state. |

## Pipeline Overview

1. **Ingest CPGs** — Parse complex guideline PDFs into structured, machine-readable form (Docling).
2. **Extract Decision Logic** — LLM extracts operational logic into DMN decision tables — the reviewable, executable artifact at the center of the system.
3. **Extract Recommendations** — Narrative recommendations and clinical rationale become RAG-retrievable context.
4. **Ingest Patient Data** — Query patient data from FHIR (IPS format), fresh for every plan.
5. **Compose Care Plan** — Multi-agent system invokes decision services, retrieves recommendations, and assembles a patient-specific FHIR CarePlan with provenance. Produces BPMN for automatable activities.
6. **Execute Automations** — BPMN process definitions drive care plan activities through pre-approved automation pathways.

## Key Design Principles

- **Deterministic where it matters.** Clinical decisions are made by validated DMN logic executed in a rules engine, not by LLM inference.
- **Standards-based contracts.** DMN, BPMN, and FHIR at every component boundary. Swap any runtime without changing its neighbors.
- **Auditable end-to-end.** Every decision traceable to its guideline source. Full provenance chain in FHIR output.
- **Human-in-the-loop.** Clinicians review and approve DMN tables (extraction) and care plans (composition). The system proposes; clinicians approve.
- **Pluggable architecture.** The platform is the constant; document parsers, decision engines, agent frameworks, vector stores, and automation runtimes are all swappable.

## Introduction Videos

Here is a playlist of videos that introduce the project. The purpose is to provide a high-level understanding of the parts and concepts in the project, not an in-depth exploration of the subjects.

[Full playlist](https://www.youtube.com/playlist?list=PLBrn0oRf1lNM)

Individual Videos:
- [CPG to ACP Intro Part 1: overview](https://youtu.be/bP39Nwj0WUQ) - a general overview of the goals and parts of the project
- [CPG to ACP Intro Part 2: CPG](https://youtu.be/-pfzh3IQH18) - a short introduction to Clinical Practice Guidelines
- [CPG to ACP Intro Part 3: DMN](https://youtu.be/H6Z3my6Lv5Q) - a short introduction to Decision Model and Notation
- [CPG to ACP Intro Part 4: Recommendations](https://youtu.be/h2ekn2hdnfo) - a short introduction to modeling recommendations
- [CPG to ACP Intro Part 5: FHIR](https://youtu.be/LuFMUkjXaOY) - a short introduction to Fast Healthcare Interoperability Resources
- [CPG to ACP Intro Part 6: Care Plan](https://youtu.be/-7EwXg68jMA) - a short introduction to care plans
- [CPG to ACP Intro Part 7: BPMN](https://youtu.be/rotr4CHi794) - a short introduction to Business Process Model and Notation
- [CPG to ACP Intro Part 8: UI](https://youtu.be/CKAtb7Ibbz4) - a brief overview of the user interfaces needed
- [CPG to ACP Intro Part 9: Future](https://youtu.be/vcVf6HB4zy8) - a brief look at future directions for the project
- [CPG to ACP Intro Part 10: Code](https://youtu.be/iU2ok5_p5r8) - a brief overview of the project structure

> **WARNING:** These videos were shot at a point in time; the project may have changed since then. Please refer to the GitHub repository for the latest.

## Getting Started

This walks through the full pipeline: CPG PDF → parse → extract DMN → deploy decisions → generate care plan.

### Prerequisites

- [Podman](https://podman.io/) (preferred) or Docker with compose support
- Python 3.11+
- OpenAI API key (for LLM-driven extraction)

### 1. Configure credentials

```bash
cp platform/litellm/deploy/.env.example platform/litellm/deploy/.env
# Edit .env with your OpenAI API key
```

### 2. Start infrastructure services

```bash
podman-compose up -d   # or: docker compose up -d
# Wait for services (includes MLflow on port 5000)
curl -sf http://localhost:8081/q/health/ready > /dev/null && echo "Kogito ready"
curl -sf http://localhost:8082/health/ready > /dev/null && echo "ACP Writer ready"
curl -sf http://localhost:5000/health > /dev/null && echo "MLflow ready"
```

### 3. Parse a CPG with Docling

```bash
cd cpg-ingester
python3 -m venv .venv && source .venv/bin/activate
pip install -e . -e ../shared

# Parse the synthetic hypertension CPG
cpg-parse data/synthetic-hypertension-cpg.pdf -o output
# Produces: output/synthetic-hypertension-cpg.md
```

### 4. Run the ingestion pipeline and deploy to acp-writer

The multi-agent pipeline parses the CPG, generates and reviews DMN decision
tables, extracts recommendations, and (with `--acp-writer-url`) deploys the
results to acp-writer in one run:

```bash
cpg-ingest data/synthetic-hypertension-cpg.pdf -o output/my-run \
  --acp-writer-url http://localhost:8082
# Generated DMN is written to output/my-run/dmn/*.dmn
```

To deploy DMN that was generated in a previous run without re-ingesting:

```bash
cpg-deploy-dmn output/my-run/dmn/*.dmn --acp-writer-url http://localhost:8082
```

Verify the models are deployed:
```bash
curl -sf http://localhost:8082/api/v1/decisions/models | python3 -m json.tool
```

### 5. Register the CPG

The guideline resolver matches patient conditions against registered CPGs. Register the CPG metadata so the pipeline knows which guidelines apply:

```bash
curl -X POST http://localhost:8082/api/v1/guidelines \
  -H "Content-Type: application/json" \
  -d '{
    "cpg_id": "cpg-hypertension-v1",
    "title": "Synthetic Hypertension CPG",
    "scope": "Essential (primary) hypertension, Type 2 diabetes mellitus",
    "version": "1.0",
    "publication_date": "2024-01-01"
  }'
```

The `scope` field is matched against patient condition display text — include all conditions the guideline covers.

### 6. Generate a care plan

Post patient data (FHIR Bundle) to the acp-writer API:
```bash
# Patient with hypertension + diabetes → medication path
curl -X POST http://localhost:8082/api/v1/careplans \
  -H "Content-Type: application/fhir+json" \
  -d @mock-EHR/data/patient-bundle-medication.json | python3 -m json.tool

# Patient with mild hypertension only → lifestyle path
curl -X POST http://localhost:8082/api/v1/careplans \
  -H "Content-Type: application/fhir+json" \
  -d @mock-EHR/data/patient-bundle-lifestyle.json | python3 -m json.tool
```

### 7. Tear down

```bash
podman-compose down   # or: docker compose down
```

### What each step exercises

| Step | Component | Red Hat AI tech |
|---|---|---|
| Parse CPG | cpg-ingester | Docling |
| Extract DMN | cpg-ingester | LLM via LiteLLM (OpenAI GPT-5.6 or Claude) |
| Deploy DMN | cpg-ingester → acp-writer API | — |
| Register CPG | acp-writer API | — |
| Generate CarePlan | acp-writer → decision-service (JIT) | Drools/Kogito |

All pipeline steps are traced in [MLflow](http://localhost:5000) when running locally.

## OpenShift Deployment

The system runs on OpenShift with Red Hat AI platform capabilities. Each component has its own Helm chart under `deploy/chart/`.

```bash
# Deploy all components (requires oc login and a target namespace)
NAMESPACE=sschifma-cpg-to-acp ./deploy/install.sh
```

On OpenShift, MaaS replaces LiteLLM for governed inference routing, and MLflow tracing is provided by the RHOAI-managed MLflow instance. See `platform/README.md` for details.

## Standards Versions

| Standard | Version | Notes |
|---|---|---|
| **DMN** | 1.4 | Latest version supported by Drools/Kogito at conformance level 3. Language namespace: `https://www.omg.org/spec/DMN/20211108/MODEL/` (the per-model target `namespace=` attribute is a unique URI per model). Upgrade to 1.5 when Drools/Kogito formally adds support. |
| **FHIR** | R4 | Via Medplum FHIR server |
| **BPMN** | 2.0 | Phase 4 |

## Developer Setup

### Secret scanning

This repo has two layers of protection against committing secrets:

1. **GitHub Push Protection** — enabled server-side, blocks pushes containing known API key patterns
2. **Gitleaks pre-commit hook** — local scanning before each commit

To activate the local hook:
```bash
brew install gitleaks
git config core.hooksPath .githooks
```

**Never hardcode API keys, passwords, or tokens in source files.** Use environment variables or Kubernetes Secrets. See `AGENTS.md` for the full policy.

## License

This project is licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).
