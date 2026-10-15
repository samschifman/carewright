# CPG-to-ACP Project Plan

> **Note:** This plan is directional. Phases, priorities, and technology choices are subject to change as the project evolves and as the Red Hat AI platform matures. Phase ordering beyond Phase 3 may be adjusted based on priorities and dependencies.

## Goal

Transform Clinical Practice Guidelines into patient-specific, FHIR-compliant, actionable care plans — running on OpenShift with Red Hat AI platform capabilities. Enable parallel development across areas with cross-cutting milestones.

## Current State (Phase 3.3 Complete)

Both cpg-ingester and acp-writer are multi-agent LangGraph pipelines with adversarial review, running on OpenShift with OpenShell sandboxing, SonataFlow orchestration, and MaaS inference. Phase 3.3 closed 2026-07-25 with a clean E2E test passing. Remaining items (multi-CPG E2E, FHIR approval lifecycle, golden test suite) captured in the backlog for future work.

**What works (verified in clean E2E on 2026-07-25):**
- **Full cross-pipeline E2E:** CPG PDF → cpg-ingester (Parse → LLM Analysis → Assembly → Delivery) → acp-writer (Scan → Resolve → Execute DMN → Retrieve Recs → Compose → Generate FHIR → Review → Write) → CarePlan on FHIR server
- **cpg-ingester:** Multi-agent LangGraph pipeline producing 5-9 DMN models + 12 recommendations from the synthetic hypertension CPG. Reviewer prompts tuned with CRITICAL/MINOR severity classification.
- **acp-writer:** 11-node pipeline producing care plans with clinically appropriate content (Lisinopril 10mg, BMP monitoring, BP confirmation). AI Transparency IG compliance. Approval workflow.
- **Infrastructure:** Pod-per-security-profile (11 pods), SonataFlow orchestration with async callbacks, MinIO artifact store with PHI-segmented buckets, API gateway, MCP Gateway (12 tools, 3 virtual servers), OpenShell sandboxes with per-pod network policies (Landlock + OPA + OCSF audit), MaaS inference (gpt-5.6-terra), MLflow tracing.

**Deferred to backlog:**
- Multi-CPG test with second CPG (diabetes — synthetic CPG prepared but not tested E2E on OpenShift)
- FHIR approval workflow E2E test (draft → active / entered-in-error)
- Golden test suite

**What doesn't exist yet:** Production UIs (current are minimal Python/Jinja), BPMN output, automation service, identity/auth, multi-CPG at scale.

---

## Phase Index

| Phase | Name | Status | Description |
|---|---|---|---|
| 2 | OpenShift + OpenShell + Platform Foundation | Complete | Deploy to OpenShift, OpenShell sandboxing, MaaS inference, MLflow tracing, agent framework selection |
| 3.0 | Contracts and Shared Infrastructure | Complete | Recommendation contract, CPG metadata contract, knowledge ingestion API, decision model contract |
| 3.1 | cpg-ingester Multi-Agent Pipeline | Complete | Multi-agent LangGraph pipeline: filtering, classification, DMN creation, recommendation extraction, delivery |
| 3.2 | acp-writer Multi-Agent Composition | Complete | 11-node pipeline: condition scanning, guideline resolution, DMN execution, plan composition, FHIR generation |
| 3.3 | Integration, Governance, and E2E Testing | Complete | Pod-per-security-profile, SonataFlow orchestration, MinIO artifact store, OpenShell sandboxes, MCP Gateway, cross-pipeline E2E |
| 4 | UI + UX = Demo-Ready | Not started | React/PatternFly UIs for cpg-ingester and acp-writer, mock-EHR with SMART on FHIR launch |
| 5 | Prompt Evaluation + Quality Improvement | Not started | Systematic evaluation of all prompts against CPG corpus, baseline metrics, iterative improvement |
| 6 | Activity Automation (BPMN++) | Not started | Generate BPMN process definitions from care plans, automation service execution |
| 7 | Governance + Safety + Evaluation | Not started | Clinical safety hardening, security fixes, AI guardrails, quality scoring, EvalHub gating, model evaluation |
| 8 | Identity, Auth & Access Control | Not started | Keycloak OIDC, RBAC, SPIFFE/SPIRE agent identity, credential scoping, audit trail |
| 9 | Interactive Editing + Advanced UX | Not started | Multi-CPG at scale, conflict resolution, interactive editing, clinical documentation, BPMN execution |
| — | Cross-Cutting Enhancements & Technical Debt | Ongoing | Phase-independent items: contract integrity, FHIR output quality, DMN/FEEL validation, resilience, performance, testing, platform infrastructure |

---

## Phases

### Phases 2–3 (Complete)

Phases 2 through 3.3 are complete. See [Appendix: Completed Phases](#appendix-completed-phases) for full work items, exit criteria, and results.

- **Phase 2** — Deployed to OpenShift with OpenShell, MaaS, MLflow, MCP. Agent framework selected (LangGraph).
- **Phase 3.0** — Defined recommendation, guideline, and decision model contracts in `shared/cpg_contracts`.
- **Phase 3.1** — Built cpg-ingester multi-agent pipeline (8+ nodes, DMN + recommendation extraction).
- **Phase 3.2** — Built acp-writer 11-node pipeline (condition scanning through FHIR server write). 250 tests.
- **Phase 3.3** — Connected E2E with pod-per-security-profile (11 pods), SonataFlow, MinIO, OpenShell sandboxes, MCP Gateway. Clean E2E test passed 2026-07-25.

---

### Phase 4 — UI + UX + Demo-Ready

**Goal:** Replace the minimal Python/Jinja UIs with production-quality React/PatternFly applications. Make the system demo-ready with a mock-EHR that launches the acp-writer via SMART on FHIR.

> **Re-prioritized:** UI work was moved from Phase 7 to Phase 4. After Phase 3.3, the backend is functionally complete (CPG → care plan → FHIR server with governance), but invisible without proper UIs. Demo readiness is the highest priority. See `working/prompts/planning_260722_analysis.md` for the full analysis.

> **Important:** The UI must never display the Red Hat logo or name. PatternFly supports white-labeling.

#### Spikes

| Spike | Focus | Status | Results |
|---|---|---|---|
| **A. UI Technology & Design System** | PatternFly 6 + React + TypeScript, PatternFly AI components, build tooling | ✅ Complete | [`dev_docs/ui/spike-a-technology.md`](ui/spike-a-technology.md) |
| **B. UI ↔ Backend Interaction Pattern** | Async communication, WebSocket vs SSE vs polling, BFF pattern, human-in-the-loop | ✅ Complete | [`dev_docs/ui/spike-b-backend-interaction.md`](ui/spike-b-backend-interaction.md) |
| **C. cpg-ingester UX Design** | Upload flow, CPG lineage, item manifest review, DMN visualization, approval workflow | ✅ Complete | [`dev_docs/ui/spike-c-cpg-ingester-ux.md`](ui/spike-c-cpg-ingester-ux.md) |
| **D. acp-writer UX Design** | Patient context, care plan visualization, multi-CPG conflicts, AI Transparency, approval | ✅ Complete | [`dev_docs/ui/spike-d-acp-writer-ux.md`](ui/spike-d-acp-writer-ux.md) |
| **E. mock-EHR Research** | Medplum vs HAPI+components vs custom, SMART on FHIR, SMART-EHR-Launcher evaluation | ✅ Complete | [`dev_docs/ui/spike-e-mock-ehr.md`](ui/spike-e-mock-ehr.md) |

#### Work Items (staged)

| Stage | Area | Work | Auth needed? |
|---|---|---|---|
| 4.0 | **research** | ~~Complete Spikes A-E~~ ✅ Complete | No |
| 4.1 | **cpg-ingester** | Rebuild cpg-ingester UI in React/PatternFly — upload, review, approve flow. Show CPG-to-recommendation lineage. | No |
| 4.2 | **mock-EHR** | Evaluate and set up mock-EHR (Medplum vs HAPI+components vs custom). Patient list, basic EHR UI. | No |
| 4.3 | **acp-writer** | Rebuild acp-writer UI in React/PatternFly — care plan review, FHIR Bundle visualization, approve/reject. Standalone initially (mock patient context). | No |
| 4.4 | **platform** | Lightweight SMART on FHIR auth — Medplum built-in OAuth, Keycloak minimal (single realm, one user), or mock OAuth stub. Just enough for the launch flow. | Minimal |
| 4.5 | **integration** | Connect acp-writer UI to mock-EHR via SMART on FHIR launch. Clinician clicks patient → acp-writer launches in context → care plan generated. | Minimal |

#### Deferred to later phases

- Interactive editing of DMN (Phase 9 — needs DMN editor or chat interaction)
- Interactive editing of recommendations (Phase 9)
- Interactive editing of care plan activities (Phase 9)
- User-added clinical documentation for care plan context (Phase 9)
- Interactive conflict resolution (Phase 9 — needs structured conflict types)

#### Exit Criteria

- Both cpg-ingester and acp-writer have React/PatternFly UIs
- cpg-ingester UI shows CPG → decision/recommendation lineage
- acp-writer UI visualizes care plans and supports approve/reject
- mock-EHR launches acp-writer via SMART on FHIR with patient context
- UIs communicate with backend asynchronously (no blocking calls)
- Demo-ready: 5-minute walkthrough of full pipeline through the UIs

---

### Phase 5 — Prompt Evaluation + Quality Improvement

**Goal:** Systematically evaluate all LLM prompts across both pipelines, establish quality baselines, collect user feedback from the demo UI, and iteratively improve prompt quality before building higher-level features on top.

> **Why here:** Phase 4 delivers the demo UI, enabling clinicians to interact with the system and provide feedback. This phase uses that feedback plus systematic evaluation to improve extraction and composition quality before Phase 6 (BPMN) builds on top of the current output.

#### Work Items

**Prompt-evaluation workflow:** Each prompt-improvement story owns a
self-contained benchmark suited to its output contract and links its runnable
handoff from the tracking issue. Before starting another prompt-improvement
story, review the completed handoffs and record any reusable lessons on
GitHub #31 / RHAIENG-6453. After at least two efforts are complete, revisit that
spike to extract recurring conventions based on evidence rather than defining a
common framework up front. CPG-ingester benchmark entry points are indexed in
[`cpg-ingester/tests/benchmarks/README.md`](../cpg-ingester/tests/benchmarks/README.md).

Each applicable prompt-improvement story also owns multi-CPG validation within
that story. Its versioned corpus must cover synthetic hypertension, synthetic
diabetes, and at least one real CPG acquired at runtime through the shared
manifest, using stage-specific goldens and at least one case or section held out
from prompt tuning. Downstream acp-writer stories may consume frozen outputs
derived from those CPGs rather than the source PDFs directly. Existing baseline
artifacts remain immutable; adding cases creates a new dataset version. The
separate multi-CPG story is only the final end-to-end integration run over
already-frozen prompts, evaluators, and goldens.

| Work | Notes |
|---|---|
| **Revisit prompt evaluation patterns after initial improvement efforts** (deferred spike) — Compare at least two completed prompt-evaluation efforts and extract only the patterns that proved reusable | Keep stage-specific metrics and schemas where the output contracts differ; decide whether shared conventions or tooling are justified by the completed work |
| **Evaluate and improve structure analyzer + content filter prompts** — Section classification accuracy, false positive/negative rates | Baseline → identify issues → improve → re-measure |
| **Evaluate and improve DMN creator prompts** — Clinical accuracy, FEEL expression correctness, decision table completeness vs golden DMN | Baseline → identify issues → improve → re-measure |
| **Evaluate and improve recommendation extractor prompts** — Completeness, accuracy of certainty grading, content fidelity | Baseline → identify issues → improve → re-measure |
| **Evaluate and improve reviewer prompts (DMN semantic, rec semantic)** — False escalation rate, missed defects, CRITICAL/MINOR threshold tuning | Baseline → identify issues → improve → re-measure |
| **Evaluate and improve plan composer prompts** — Goal/activity clinical appropriateness, medication accuracy, dosing correctness | Baseline → identify issues → improve → re-measure |
| **Evaluate and improve FHIR generation prompts** — Bundle correctness, terminology accuracy, structural compliance vs $validate | Baseline → identify issues → improve → re-measure |
| **Evaluate and improve FHIR semantic reviewer prompts** — False approval rate, missed defects, feedback quality for revision loop | Baseline → identify issues → improve → re-measure |
| **Review and improve Docling usage** — Evaluate parsing quality across CPG formats, image/chart interpretation (vision model), OCR for scanned PDFs | Currently Docling detects image regions but doesn't interpret content |
| **Multi-CPG end-to-end integration validation** — Run the assembled pipeline across hypertension, diabetes, and at least one real CPG sourced at runtime through the existing manifest | Thin integration gate over prompts, evaluators, and stage-specific goldens already frozen by the individual improvement stories; no prompt tuning or catch-up golden authoring belongs here |

#### Exit Criteria

- Baseline quality metrics established for all prompts in both pipelines
- At least one round of user feedback collected and applied
- Measurable improvement in extraction quality (fewer missed recommendations, fewer DMN errors)
- Prompts evaluated against at least 3 CPGs (synthetic hypertension, synthetic diabetes, one real)
- Quality metrics documented for future regression tracking

---

### Phase 6 — BPMN + Automation

**Goal:** Add BPMN generation to make care plans actionable. Connect acp-writer to the automation service.

#### Work Items

| Area | Work | Notes |
|---|---|---|
| **acp-writer** | Add BPMN writing agent — writes BPMN for process/recommendations | — |
| **acp-writer** | Include BPMN in DocumentReferences linked to CarePlan activities via extension | FHIR extension design |
| **acp-writer** | Publish BPMN to automation service on care plan approval | — |
| **automation** | Implement automation service that accepts BPMN from acp-writer | Receives BPMN over API |
| **shared** | Define the BPMN contract in shared/ | — |
| **acp-writer UI** | Add BPMN visualization within care plan review | BPMN renderer in React UI |

#### Exit Criteria

- acp-writer generates BPMN for automatable activities
- Automation service receives and stores BPMN
- BPMN visible in the care plan review UI

---

### Phase 7 — Governance + Safety + Evaluation

**Goal:** Quality gates, guardrails, and evaluation pipelines.

#### Work Items

| Work | Notes |
|---|---|
| **Harden clinical review pipeline to fail safe** — Review gates fail closed on parse errors, only validated DMN delivered, escalated items surfaced for human review, terminology API verification | The pipeline should never silently pass bad clinical content |
| **Security hardening for clinical data** — Fix SSRF in delivery endpoint, gate sample data seeding behind dev/demo flag, MinIO per-pod IAM policies for PHI bucket access | Close known security gaps before production |
| **AI guardrails and adversarial testing** — NeMo Guardrails on agent I/O (healthcare-specific rules), Garak red-teaming for healthcare adversarial scenarios | Protect against bad/malicious input and output |
| **Golden test suite and quality scoring** — Parameterized regression suite (single-CPG medication, single-CPG lifestyle, multi-CPG, no-guidelines), CarePlan quality scoring (automated + clinician review), AI Transparency on FHIR IG validation | Measure pipeline and output quality systematically |
| **EvalHub integration and deployment gating** — Golden test sets per CPG, extraction fidelity scorers, plan quality scorers, deployment gates that block degraded models/pipelines | Quality measurement blocks bad deployments |
| **Evaluate self-hosted models vs frontier** (spike) — Smaller models via vLLM for cost, latency, and data locality | Research, not a deliverable |

#### Exit Criteria

- Clinical review gates fail closed; terminology codes API-verified
- Escalated content surfaced; only validated DMN delivered
- Security fixes applied (SSRF, sample data gating, MinIO IAM)
- Guardrails actively filtering agent I/O
- Golden test suite measuring quality across CPGs
- EvalHub gates preventing degraded deployments
- AI Transparency on FHIR compliance
- Self-hosted model evaluation complete

---

### Phase 8 — Identity, Auth & Access Control

**Goal:** Establish full user authentication, role-based access control, and agent credential scoping. Replace the lightweight Phase 4 auth with production-grade identity infrastructure.

> **Note:** Phase 4 uses lightweight SMART on FHIR auth (Medplum built-in, Keycloak minimal, or mock stub) for demos. This phase adds production identity: RBAC, SPIFFE/SPIRE agent credentials, audit trails, and multi-user auth.

#### Work Items

| Work | Notes |
|---|---|
| **Identity infrastructure — Keycloak, roles, and agent identity** — Deploy Keycloak on OpenShift (full OIDC provider), define roles (clinician, admin, reviewer) with Keycloak RBAC, agent identity via SPIFFE/SPIRE | Stand up production identity, replacing Phase 4 lightweight auth |
| **Integrate OIDC auth into all application components** — Wire Keycloak OIDC into acp-writer UI/API, cpg-ingester UI/API, and HAPI FHIR (token-based access) | All UIs require authentication; all APIs enforce token-based access |
| **Credential scoping and audit trail** — OpenShell credential scoping (user-scoped tokens via OpenShell + Keycloak, not shared service accounts), audit trail linking actions to authenticated identities (MLflow tracing + OpenShell sandbox audit) | Agents run with scoped credentials; every action traceable |

#### Exit Criteria

- Keycloak running on OpenShift with OIDC configured
- At least three roles (clinician, admin, reviewer) with distinct permissions
- Agent credentials scoped per-user via OpenShell + SPIFFE/SPIRE
- All UIs require authentication; APIs enforce token-based access
- Audit trail links every action to an authenticated identity

---

### Phase 9 — Scale + Polish

**Goal:** Multiple CPGs at scale, interactive editing, conflict resolution, and production polish.

#### Work Items

| Work | Notes |
|---|---|
| **Multi-CPG at scale with conflict resolution** — Expand to 3-5 real CPGs (VA/DoD), multi-plan merging when multiple CPGs apply, conflict resolution with clinician input (structured conflict types: same target, contradictory, overlapping), interactive resolution UI, resolution tracking in Provenance | Move beyond single-CPG to handle real-world guideline overlap |
| **Interactive editing across both UIs** — DMN editing (chat-based or visual editor) and recommendation editing in cpg-ingester UI, care plan activity editing in acp-writer UI | Let users refine AI-generated artifacts, not just accept/reject |
| **User-added clinical documentation for care plan context** — Free-text clinical input from clinicians to augment care plan context with patient-specific information not in structured FHIR data | — |
| **BPMN execution engine** — Add execution capability to the automation service (SonataFlow, BPMN-to-Ansible converter, or other BPMN-conformant engine). Builds on Phase 6 receive/store foundation. | Completes the automation story started in Phase 6 |

#### Exit Criteria

- 3-5 CPGs with multi-plan merging and conflict resolution
- Interactive editing in both UIs
- Clinician-added documentation augments care plans
- BPMN execution operational
- Production-ready

---

## Technology Adoption Timeline

| Phase | Status | Technologies Added |
|---|---|---|
| Phase 1 | Complete | Docling, LiteLLM (local), Drools/Kogito |
| Phase 2 | Complete | OpenShift, OpenShell, MaaS, MLflow, MCP |
| Phase 3.0 | Complete | cpg-contracts v1.0 (recommendations, guidelines, search) |
| Phase 3.1 | Complete | LangGraph (cpg-ingester agents) |
| Phase 3.2 | Complete | pgvector, LangGraph (acp-writer agents), AI Transparency IG |
| Phase 3.3 | Complete | MCP Gateway, SonataFlow, MinIO, async callbacks, API gateway, pod-per-security-profile |
| Phase 4 | Not started | React, PatternFly 6, TypeScript, SMART on FHIR (lightweight), Medplum (evaluate) |
| Phase 5 | Not started | Prompt evaluation corpus, quality metrics, user feedback loop |
| Phase 6 | Not started | — (BPMN generation, no new platform tech) |
| Phase 7 | Not started | NeMo Guardrails, EvalHub, Garak, vLLM, Praxis |
| Phase 8 | Not started | Keycloak (full), SPIFFE/SPIRE |
| Phase 9 | Not started | — (scale and polish, no new platform tech) |

## Parallel Development Tracks

Each area can advance semi-independently within a phase. Cross-cutting dependencies are noted in the phase tables. The key synchronization points are:

1. **Agent framework selection (Phase 2 spike)** — blocks all multi-agent work in Phase 3. Decision: LangGraph (see `dev_docs/spikes/spike-agent-framework.md`).
2. **OpenShift deployment (Phase 2)** — blocks OpenShell, MaaS
3. **Recommendation contract (Phase 3.0)** — blocks both Phase 3.1 and Phase 3.2. This is the single gate before cpg-ingester and acp-writer can advance independently.
4. **UI technology decision (Phase 4 Spike A)** — blocks all UI development in Phase 4.
5. **BPMN contract in shared/ (Phase 6)** — blocks automation service integration
6. **Keycloak full deployment (Phase 8)** — blocks production auth. Lightweight SMART on FHIR auth in Phase 4 does not require full Keycloak.

Within Phase 3, the cpg-ingester track (3.1) and acp-writer track (3.2) are designed to advance independently after the shared contracts (3.0) are defined. Neither blocks the other — cpg-ingester validates recommendations against the contract schema, acp-writer tests against hand-crafted recommendation data.

## Backlog — Phase-Independent Tasks

Work that can be picked up at any time, independent of the current phase. These items improve the project but don't block other work.

| Item | Status | Area | Notes |
|---|---|---|---|
| Abbreviation expansion in Rec Extractor | ✅ Complete | cpg-ingester | Rec Extractor prompt now expands ALL occurrences of abbreviations in `content` as "Full Name (ABBREVIATION)". No bare abbreviations — content is self-contained for vector search. |
| cpg-ingester metadata extractor null handling | ✅ Complete | cpg-ingester | `raw.get("title", "Untitled")` returns None when LLM outputs explicit null. Fixed to use `or "Untitled"` pattern. Commit `40ebd7d`. |
| acp-writer workflow ips_ref fix | ✅ Complete | acp-writer | SonataFlow workflow passed `ips_bundle` (null inline) instead of `ips_ref` (MinIO reference) to scan endpoint. Fixed in commit `9d80206`. |
| acp-writer patient-data scan ref resolution | ✅ Complete | acp-writer | Scan endpoint now resolves IPS from MinIO ref (`ips_ref`) in addition to inline `ips_bundle`. Uses `resolve_ref()` + fallback for legacy key name. Commit `b8cbaf0`. |
| DMN model discovery in pod-split deployment | ✅ Complete | acp-writer | Guideline resolver queries decision engine REST API (`GET /api/v1/decisions/models`). Proven in clean E2E test (5+ DMN models found). Commit `b3086d0`. |
| Guideline Resolver can't see in-process models (single-pod) | Won't fix | acp-writer | Single-pod/monolith mode is no longer a supported deployment. Pod-split with SonataFlow is the only deployment model going forward. |
| compose default profile can't reach the LLM | Won't fix | deploy | Single-pod mode is no longer supported. Pod-split with SonataFlow is the only deployment model going forward. |
| | | | **— Contract & data integrity —** |
| Preserve ingester-assigned GUIDs (identity contract) | Not started | shared / cpg-ingester / acp-writer | Contract-proposal design decision #2 requires ingester-assigned GUIDs to be preserved end-to-end, but they are lost on both sides. cpg-ingester never emits a `DecisionModelSummary` (`dmn_creator.py` returns only `dmn_xml`; `generation.py:221` reads an always-empty summary), so the manifest GUID, `category`, `modifies`, and `source_location` for decisions never cross the boundary. acp-writer then re-derives the id from the model name (`api.py:91`, `decision_engine.py:37`), ignoring `root.get("id")`. Net effect: `CrossReference.target_id` GUIDs to decision models can't resolve, `modifies` override chains break, same-named models collide. Fix: emit `DecisionModelSummary` (carrying the manifest id) from the DMN track and deliver it; have acp-writer read and preserve the DMN root `id` instead of deriving from the name. |
| Validate `contract_version` on ingestion | Not started | acp-writer | Contract principle #6 / design decision #6 require acp-writer to validate `contract_version` and reject incompatible payloads. There are no references to `contract_version` in `acp-writer/src`; `register_guideline` (`api.py:311`) and `ingest_recommendation_batch` (`api.py:355`) accept any/absent version. Add a version check on the ingest endpoints. |
| CPGMetadata contract drift vs approved design | Not started | shared | Code diverges from the approved contract-proposal: `CPGMetadata.supersedes` is `list[str]` where Contract 1 specifies `str`, and the `archetype`/`GuidelineArchetype` enum was dropped although the Structure Analyzer still detects archetype. Reconcile the code and the design doc (pick one representation and update the other). |
| In-memory state lost on pod restart | Not started | acp-writer | Guidelines and recommendations are stored in-memory (`_guidelines_store`, `_vector_store`). Pod restarts lose all registered CPGs and ingested recommendations, requiring re-delivery from cpg-ingester. Fix: persist to MinIO or a database, or reload from MinIO on startup. |
| | | | **— FHIR output quality —** |
| CarePlan omits `addresses` (patient Conditions) | Not started | acp-writer | `fhir_bundle_builder.py:205` builds CarePlan without `addresses=[Conditions]` (design Node 7). The semantic reviewer is prompted to check "addresses match conditions" — a check that can never pass. Populate `addresses` from the scanned condition references. |
| FHIR transaction bundle patient reference | Not started | acp-writer | Transaction bundle references Patient by ID but doesn't include the Patient resource. Normally the patient exists on the FHIR server (IPS originated from there), but need to handle the case where it doesn't — either include Patient in the transaction or use conditional references. |
| Approval workflow should POST/update on FHIR server | Not started | acp-writer | Care plan should be POSTed to FHIR in "draft" status on creation. Approval updates status to "active" on the FHIR server; rejection updates to "entered-in-error". AIAST → CLINAST_AIRPT transition should be reflected on the server, not just in-memory. |
| FHIR server-side validation ($validate) | Not started | acp-writer | Use HAPI FHIR's $validate operation to validate generated Bundles before writing. Currently only client-side validation. |
| Provenance CPG lineage improvement | Not started | acp-writer | Per-activity Provenance currently only references recommendation ID. Should include CPG title, section, page numbers (from SourceLocation), and recommendation title for meaningful lineage display in the care plan bundle. |
| PatientSummary allergies field | Not started | acp-writer | The PatientSummary Pydantic model doesn't include allergies. Add AllergyIntolerance extraction from IPS Bundle. |
| | | | **— DMN & FEEL validation —** |
| FEEL expression validator | Not started | cpg-ingester | Replace regex-based FEEL checks with a proper validator. Best option: expose a validation endpoint from the Kogito runtime (already running in acp-writer, Apache 2.0). No mature license-compatible Python FEEL parser exists. |
| DMN validator | Not started | cpg-ingester | Improve the DMN validator, look for opensource solutions and potentially expose an endpoint from Kogito to test. |
| Upgrade DMN to 1.5 | Not started | cpg-ingester | Currently targeting DMN 1.4 (latest supported by Drools/Kogito at conformance level 3). DMN 1.5 (Aug 2024) adds useful FEEL functions (`context put`, `now()`, `today()`). Upgrade when Drools/Kogito formally supports 1.5. Watch [Drools releases](https://github.com/apache/incubator-kie-drools/releases). |
| | | | **— Resilience & robustness —** |
| SonataFlow callback timeouts and retry | Not started | automation | Async callback states (Parse, Analyze in cpg-ingester; ComposePlan, GenerateBundle, ReviewFHIR in acp-writer) hang forever if the service pod dies mid-work. Add `timeouts.eventTimeout` to each callback state so SonataFlow transitions to an error/retry path instead. Design the retry flow: re-trigger the async call with the same MinIO ref, track attempt count, escalate after N failures. |
| Review `_extract_section_text` robustness | Not started | cpg-ingester | Current implementation in `generation.py` uses heading-level matching to extract section text. It now skips non-numbered headings (e.g., "Decision Table 1:", "Key principles:") but may still be brittle for CPGs with inconsistent heading structures, deeply nested sections, or non-standard numbering. Consider using the section_map page ranges as a fallback or combining heading-based and page-based extraction. |
| Review SonataFlow inline vs ref data flow | Not started | acp-writer | Several acp-writer service endpoints pass intermediate data (condition_codes, dmn_results, applicable_cpgs, patient_demographics, medication_codes) inline through SonataFlow workflow state rather than via MinIO refs. Review: (1) which fields could exceed SonataFlow payload limits as CPG complexity grows, (2) whether resolve_ref should be added to `/resolve` and `/retrieve` endpoints, (3) the general pattern of how data flows between SonataFlow states and whether large payloads should always use ref-based transfer. Currently safe for the hypertension CPG (~8 DMN models, handful of conditions) but may break with larger CPGs or multi-CPG scenarios. |
| | | | **— Performance —** |
| Parallel DMN and rec extraction | Not started | cpg-ingester | `generate_all()` runs DMN generation sequentially before rec extraction, but they're independent (different manifest items, no shared output). Run them as parallel LangGraph branches to roughly halve LLM analysis time. Constraint: MaaS rate limits may need tuning for doubled concurrent calls. |
| Embedding model tuning for clinical domain | Not started | acp-writer | Current vector store uses FakeEmbeddingProvider. Evaluate clinical-domain embedding models (NeuML/pubmedbert-base-embeddings or similar) for recommendation retrieval quality. |
| | | | **— Testing —** |
| FHIR approval lifecycle E2E test | Not started | acp-writer | Care plans are written as `draft`. The approve/reject flow (draft → active / entered-in-error) exists in code and works locally but has not been tested end-to-end through SonataFlow on OpenShift. May need a separate SonataFlow workflow or a manual trigger endpoint. |
| E2E test automation script | Not started | testing | Update `scripts/run-e2e-openshift.sh` with SonataFlow-based pipeline execution, OpenShell sandbox deployment, and results verification. Current E2E is manual CLI commands. |
| | | | **— Platform & infrastructure —** |
| Transition from LiteLLM to MaaS (including variable renames) | Not started | all | Phase 2 deployed LiteLLM on-cluster as the inference proxy. MaaS gateway is now operational but LiteLLM references remain in code and config. Complete the transition. Includes renaming technology-specific variables (LITELLM_URL → LLM_BASE_URL). |
| MaaS with Vertex AI (Claude) | Not started | platform | Configure MaaS ExternalModel to route to Claude on Vertex AI. Requires a GCP service account key (not ADC user credentials) with the Vertex AI User role, and `oauth2` auth type on the ExternalProvider. OpenAI routing is already working; this adds Claude as a second provider option on-cluster. |
| Enhance tracing in MLflow | Not started | all | Make sure that the use of MLflow is optimized and that traces are useful. |
| OpenShell upstream issues (transparent routing, botocore proxy, short hostnames) | Not started | deploy | Three related OpenShell limitations needing upstream engagement: (1) nginx hostname-translation router should become mutating webhook or init container injection, (2) boto3 S3 PUT fails through CONNECT proxy — workaround: presigned URLs, (3) OPA requires exact hostname match — short K8s DNS names don't match FQDN wildcards. See `dev_docs/design/openshell-integration-findings.md`. |

---

## Open Spikes and Research Items

| Item | Phase | Status | Notes |
|---|---|---|---|
| Agent framework evaluation | 2 | ✅ Complete | LangGraph selected. See `dev_docs/spikes/spike-agent-framework.md` |
| Praxis investigation | 2 | ✅ Complete | Too early. Track for Phase 7. See `dev_docs/spikes/spike-praxis.md` |
| Effective FHIR CarePlan goals | 3 | ✅ Complete | Implemented in acp-writer Plan Composer |
| AI Transparency on FHIR IG | 3 | ✅ Complete | AIAST/CLINAST_AIRPT implemented |
| Recommendation contract format | 3 | ✅ Complete | `cpg_contracts.recommendations` v1.0 |
| SonataFlow orchestration | 3.3 | ✅ Complete | Async callbacks, HTTP CloudEvents. See `dev_docs/spikes/spike-sonataflow-orchestration.md` |
| MCP Gateway governance | 3.3 | ✅ Complete | 12 tools, 3 virtual servers. See `dev_docs/spikes/spike-mcp-gateway.md` |
| Artifact store (MinIO) | 3.3 | ✅ Complete | PHI-segmented buckets. See `dev_docs/spikes/spike-artifact-store.md` |
| Async callback pattern | 3.3 | ✅ Complete | HTTP CloudEvents, no Kafka. See `dev_docs/spikes/spike-async-callback.md` |
| UI technology + design system | 4 | ✅ Complete | PatternFly 6 + React + TypeScript. See `dev_docs/ui/spike-a-technology.md` |
| UI ↔ backend interaction pattern | 4 | ✅ Complete | SSE + BFF pattern. See `dev_docs/ui/spike-b-backend-interaction.md` |
| cpg-ingester UX design | 4 | ✅ Complete | Wireframes + flow diagrams. See `dev_docs/ui/spike-c-cpg-ingester-ux.md` |
| acp-writer UX design | 4 | ✅ Complete | Wireframes + flow diagrams. See `dev_docs/ui/spike-d-acp-writer-ux.md` |
| mock-EHR research (Medplum) | 4 | ✅ Complete | Medplum recommended. See `dev_docs/ui/spike-e-mock-ehr.md` |
| Self-hosted models vs. frontier | 7 | Not started | Evaluate using smaller models (via vLLM) for cost, latency, and data locality |
| BPMN-to-Ansible conversion | 9 | Not started | Feasibility and approach |

---

## Appendix: Completed Phases

### Phase 2 — OpenShift + OpenShell + Platform Foundation

**Goal:** Get the system running on OpenShift with OpenShell sandboxing and governed inference.

**Exit Criteria (all met):**
- Pipeline runs on OpenShift
- acp-writer runs inside OpenShell sandbox with visible policy enforcement
- All inference routed through MaaS
- MLflow traces for every pipeline step
- Agent framework decision made: LangGraph (see `dev_docs/spikes/spike-agent-framework.md`)
- At least one real CPG processed end-to-end

---

### Phase 3.0 — Contracts and Shared Infrastructure

**Goal:** Define the recommendation contract and establish cross-cutting infrastructure.

**Exit Criteria (all met):**
- [x] Recommendation contract defined in `shared/` with Pydantic models
- [x] Knowledge ingestion API contract defined (OpenAPI + MCP tool schema)
- [x] Both cpg-ingester and acp-writer can implement against the contract independently
- [x] Test fixtures available for both tracks

**Key artifacts:** `cpg_contracts.recommendations` (v1.0), `cpg_contracts.guidelines`, `dev_docs/design/contract-proposal-ingester-writer.md`, `dev_docs/design/cpg-analysis.md` (42 CPGs analyzed)

---

### Phase 3.1 — cpg-ingester Multi-Agent Pipeline

**Goal:** Replace single-prompt DMN extraction with a multi-agent pipeline producing both DMN and recommendations.

**Exit Criteria (all met):**
- Multi-agent pipeline (LangGraph StateGraph with 8+ nodes)
- Produces both DMN and recommendations in the shared contract format
- DMN validated against golden test cases
- Recommendations validated against the shared contract schema
- Minimal upload/review UI functional
- All agents traced in MLflow

**Key artifacts:** `cpg_ingester/pipeline.py`, `cpg_ingester/generation.py`, `dev_docs/design/cpg-ingester-design.md`

---

### Phase 3.2 — acp-writer Multi-Agent Composition

**Goal:** Replace hardcoded care plan composition with a multi-agent system using DMN decisions, retrieved recommendations, and FHIR expertise.

**Exit Criteria (all met):**
- [x] CarePlans with recommendation-backed narrative activities
- [x] Vector store operational with recommendation retrieval
- [x] Knowledge ingestion and guidelines CRUD endpoints
- [x] Composition uses both DMN decisions and retrieved recommendations
- [x] Planning Brief validated by adversarial reviewer
- [x] FHIR output passes terminology, syntax, and semantic validation
- [x] AI Transparency on FHIR IG compliant (AIAST/CLINAST_AIRPT)
- [x] Care plans written to HAPI FHIR server
- [x] Approval workflow (AIAST → CLINAST_AIRPT)
- [x] Minimal review/approval UI functional
- [x] All agents traced in MLflow

**Key artifacts:** `acp_writer/pipeline.py` (11 nodes), `dev_docs/design/acp-writer-design.md`, 250 tests

---

### Phase 3.3 — Integration, Governance, and End-to-End Testing

**Goal:** Connect cpg-ingester and acp-writer end-to-end, apply governance (OpenShell, MCP Gateway), split into pod-per-security-profile with SonataFlow orchestration.

**Implementation plan:** `working/phase3.3-implementation.md` (15 steps)

**Exit Criteria:**
- [x] E2E pipeline: cpg-ingester → acp-writer produces CarePlans using both DMN and recommendations — **PASS** (2026-07-25: CarePlan/1115 with 4 goals + 4 activities)
- [ ] Pipeline tested with at least two CPGs — deferred to backlog
- [x] Pipeline runs on OpenShift with MLflow traces — **PASS**
- [x] Both components split into pod-per-security-profile — **PASS** (11 pods)
- [x] SonataFlow driving cross-pod execution — **PASS** (async callbacks)
- [x] OpenShell policies applied and enforced — **PASS** (Landlock + OPA + OCSF)
- [x] MCP Gateway demonstrating governed tool access — **PASS** (12 tools, 3 virtual servers)
- [ ] FHIR approval lifecycle (draft → active / entered-in-error) — deferred to backlog
- [x] Pipeline parallelism: Terminology ∥ FHIR Syntax validators — **PASS**
- [x] Inference routed through MaaS — **PASS** (gpt-5.6-terra)

**Key artifacts:** SonataFlow workflows, OpenShell policies (`deploy/openshell-policies/`), openshell-router (`deploy/openshell-router/`), MinIO artifact store, API gateway, `dev_docs/design/openshell-integration-findings.md`, `docs/sonataflow-orchestration.md`, `docs/openshell-agent-security.md`
