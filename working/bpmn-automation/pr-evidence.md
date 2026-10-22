# PR implementation evidence

## Task 1 — Green suites and the build fix

- Files planned: `cpg-ingester/data/golden/automation/*` generated outputs; `acp-writer/ui/src/pages/__tests__/CarePlanSlice.test.tsx`; rename `shared/tests/test_contracts.py` to `shared/tests/test_automation_contracts.py`; `.github/workflows/publish-images.yml`.
- Expected behavior: goldens regenerate with only the `Property.values` contract update; the CarePlan slice test asserts the fixture's current template identity; shared-root pytest collection no longer collides with prompt-eval; the ingester UI image action builds with repository-root context.
- Baseline checks: golden `--check` failed only for four stale template JSON files; `CarePlanSlice.test.tsx` failed on its old template ID; shared pre-change `pytest --collect-only tests -q` collected 394 tests, so the basename collision was not reproduced with the current `testpaths` configuration.
- Implementation: regenerated the four template JSON goldens; updated the UI test's template ID (fixture CPG ID remains `SYN-HTN-2026-002`); renamed the shared contract test as directed; changed the ingester UI workflow build context to repository root.
- Checks: golden regeneration `--check` passes; focused `CarePlanSlice.test.tsx` passes; shared 394 passed (one existing Pydantic warning); ingester 577 passed, 30 skipped, 9 deselected; writer 737 passed, 6 skipped, 15 deselected; automation 13 passed using `PYTHONPATH=src` with the installed shared environment because `automation/.venv` exists but is empty; Appendix A includes the automation venv setup step; shared UI typecheck/build pass (build reports an existing bpmn-moddle warning); ingester UI typecheck/tests (21)/build pass (large-chunk warning); writer UI typecheck/tests (54)/build pass (large-chunk warning).
- Shared pre-change collection found 394 tests with current `testpaths`; the prompt-eval duplicate basename was not collected. The rename was applied as explicitly requested and the post-change full shared suite passes.
- Sam checks: root-context `podman build -f cpg-ingester/ui/Containerfile .` pending; container commands are Sam-owned by the plan.
- Task 1 implementation is complete. Move to 2A; no tests or build outputs besides the four intended template JSON goldens are changed.

## Task 2A — Publisher state machine and API mapping (complete)

- Files planned: `acp-writer/src/acp_writer/services/automation_publisher.py`, `acp-writer/src/acp_writer/nodes/fhir_server_writer.py`, `acp-writer/src/acp_writer/services/fhir_server.py`, `acp-writer/src/acp_writer/api.py`, `acp-writer/src/acp_writer/services/bff.py`, `acp-writer/api/openapi.yaml`, `acp-writer/api/bff-openapi.yaml`, `acp-writer/ui/src/api/types.ts`, `acp-writer/tests/test_automation_publisher.py`, `acp-writer/tests/test_fhir_server_writer.py`, `acp-writer/tests/test_api_startup.py`, and new `acp-writer/tests/test_publication_routes.py`.
- Expected behavior: preserve durable FHIR approval/recovery independently of automation configuration; use no localhost automation URL fallback; make `not-applicable`, `unconfigured`, `published`, and `withdrawn` terminal; retain bounded retry for configured `pending`/`failed`; preserve withdrawal tombstones; make both retry routes return existing terminal/approving jobs unchanged and 404 only when no job exists; expose only a status in the BFF CarePlan view.
- Inspection findings before edits: publisher currently defaults to `http://localhost:8096`, does not model automation-token availability, and only treats `published`/`withdrawn` as terminal. The BFF currently drops `automation_publication`; the UI has no publication view/type or error banner, so this task will add the plan's minimal status-only `automationPublication` contract rather than modify a nonexistent banner. `openapi.yaml` does not currently document the retry endpoint.
- Implementation: removed the localhost automation URL fallback; normalized URL/token configuration and added the post-FHIR `not-applicable`/`unconfigured` states; unified terminal-state handling across publication, retries, recovery and withdrawal; preserved FHIR lookup/replay on disabled automation; persisted withdrawal tombstones; made retry responses status-only; added the BFF/UI status-only CarePlan contract.
- Focused checks: `env -u LITELLM_URL -u LLM_MODEL -u LITELLM_API_KEY -u OPENAI_API_KEY .venv/bin/python -m pytest tests/test_automation_publisher.py tests/test_fhir_server_writer.py tests/test_api_startup.py tests/test_publication_routes.py -q -m 'not network'` — 58 passed, 8 existing deprecation warnings.
- Authorized Task 2A scope update: add only `automation_token="test-token"` to the two `AutomationPublisher(...)` fixtures in `acp-writer/tests/test_slice_in_process.py` (around lines 361 and 572). The same file is eligible later in 3B only for setting `AUTOMATION_SERVICE_TOKEN` on its in-process automation app and accepting `X-Automation-Token` in its fake `dispatch_post`.
- Focused checks: `env -u LITELLM_URL -u LLM_MODEL -u LITELLM_API_KEY -u OPENAI_API_KEY .venv/bin/python -m pytest tests/test_automation_publisher.py tests/test_fhir_server_writer.py tests/test_api_startup.py tests/test_publication_routes.py -q -m 'not network'` — 58 passed.
- Component check before authorized fixture update: `env -u LITELLM_URL -u LLM_MODEL -u LITELLM_API_KEY -u OPENAI_API_KEY .venv/bin/python -m pytest tests -q --ignore=tests/test_integration.py -m 'not network'` — 753 passed, 6 skipped, 15 deselected, 2 failed in `test_slice_in_process.py` because fixtures lacked the newly required token.
- Final 2A checks: focused writer tests — 58 passed, 8 warnings; full writer suite — 755 passed, 6 skipped, 15 deselected, 10 warnings; writer UI `npm run typecheck` passed; `npm test` — 16 files / 54 tests passed; `npm run build` passed with the existing large-chunk warning. Regenerated `ui/src/api/types.ts` using `npm run gen:api` from `acp-writer/ui`.
- Task 2A checkpoint: complete. No Sam checks are pending; proceed to 2B.

## Task 2B — Configuration and loader tolerance (complete)

- Files changed: `compose.orchestrated.yml`, `acp-writer/deploy/chart-pods/values.yaml`, `acp-writer/deploy/chart-pods/templates/deployments.yaml`, `deploy/load-published-artifacts.sh`, and `deploy/tests/test-load-published-artifacts.sh`.
- Behavior: configured the orchestrated fhir-server URL; added an opt-in chart value that conditionally renders `AUTOMATION_SERVICE_URL` without a cluster default; left `BPMN_PREFLIGHT_URL` unset. Optional template retrieval, staging, HTTP rejection, and transport failures are warned/counted and do not block later templates; final summary reports registered and failed counts. Guideline, recommendation, DMN, and template-reference validation failures remain fatal.
- Checks: `bash -n deploy/load-published-artifacts.sh` passed; `bash deploy/tests/test-load-published-artifacts.sh` passed all 9 scenarios including HTTP 422, transport failure followed by later success, and existing DMN hard-failure cases; `helm lint acp-writer/deploy/chart-pods` passed (icon recommendation only); default chart render omitted `AUTOMATION_SERVICE_URL`; explicit URL rendered on fhir-server; scoped `git diff --check` passed.
- Pending Sam check: Compose proof remains assigned to Sam in Step 6. No unresolved implementation defects identified. No files staged.
- Task 2B checkpoint: complete. Next eligible task is 3B; no 3B edits started.
- Follow-up: The explicit URL render check applies only when `openshellMode=false`; default OpenShell mode creates the fhir-server sandbox in `deploy/openshell/deploy.sh`, so the chart value has no effect there.
- OpenShell sandbox note: the automation service is not deployed in this release; when it is, pass `AUTOMATION_SERVICE_URL=` in the `sb-fhir-server` sandbox call.
- Follow-up checks: `bash -n acp-writer/deploy/openshell/deploy.sh` passed; `helm lint acp-writer/deploy/chart-pods` passed; default chart render reported `0` `AUTOMATION_SERVICE_URL` occurrences.

## Task 3B — C2 write authentication and local access (in progress)

- Files planned: `automation/src/automation_service/app.py`, `automation/tests/test_app.py`, `acp-writer/src/acp_writer/services/automation_publisher.py`, `acp-writer/tests/test_automation_publisher.py`, `acp-writer/tests/test_fhir_server_writer.py` (fake `fake_post` accepts the no-redirect keyword only), `acp-writer/tests/test_slice_in_process.py`, `compose.yml`, `compose.orchestrated.yml`, `compose.slice.yml`, `scripts/run-slice-local.sh`, `automation/README.md`, `acp-writer/README.md`, and new `scripts/tests/test_slice_automation_access.sh`.
- Expected behavior: authenticate automation write routes with the shared token using constant-time comparison; keep health/list/get behavior as specified; send authenticated writer POSTs without following redirects; add the documented local-only Compose default on all three services; bind the slice host port only to loopback; test receiver, publisher, and slice configuration without real services.
- Test-file restriction: in `acp-writer/tests/test_slice_in_process.py`, make only the authorized changes: configure `AUTOMATION_SERVICE_TOKEN` for the in-process automation app and accept `X-Automation-Token` in its fake `dispatch_post`.
- Checks planned: focused automation/writer Python tests, full PY-A/PY-W, shell syntax and the new slice access shell test. Compose/container proof remains Sam-owned.
- Final focused checks: automation `test_app.py` — 17 passed; writer publisher, in-process slice, and FHIR-server writer tests — 55 passed.
- Final component checks: full automation suite — 18 passed; full writer suite — 757 passed, 6 skipped, 15 deselected, 10 warnings.
- Shell checks: `bash scripts/tests/test_slice_automation_access.sh` passed; `bash -n scripts/tests/test_slice_automation_access.sh` passed. Replaced all `rg` invocations with equivalent fixed-string `grep` checks.
- Resolved the prior full-writer failure by adding only `allow_redirects=None` to the `fake_post` signature; its behavior and assertions are unchanged.
- Task 3B checkpoint: complete. Compose/container proof remains Sam-owned; no 3C work started.

## Task 3C — H1 bucket-pinned, prefix-scoped artifact reads (complete)

- Files: `shared/src/cpg_contracts/artifact_store.py`, new `shared/src/cpg_contracts/artifact_paths.py`, new `shared/tests/test_artifact_access.py`, `acp-writer/src/acp_writer/services/bff.py`, new `acp-writer/tests/test_bff_artifact_access.py`, `cpg-ingester/src/cpg_ingester/services/bff.py`, and new `cpg-ingester/tests/test_bff_artifact_access.py`.
- Behavior: added literal-key `get_raw_in_bucket` constrained to the configured bucket; shared strict validation for decoded run IDs and relative paths; serve writer artifacts only from `cpg-phi` and ingester artifacts only from `cpg-uploads`; reject malformed/reserved paths before object-store access; return 503 when the correct store is unavailable and 404 when an object is missing; preserve trusted internal qualified-ref reads.
- Focused checks: shared artifact-access tests — 21 passed; writer BFF artifact-access tests — 10 passed; ingester BFF artifact-access tests — 10 passed.
- Component checks: PY-S — 415 passed; PY-I — 587 passed, 30 skipped, 9 deselected; PY-W — 767 passed, 6 skipped, 15 deselected.
- Pending Sam checks: none for 3C. Existing 3B Compose proof remains Sam-owned. No unresolved 3C defects identified.
- Task 3C checkpoint: complete. Next eligible task is 3D; no 3D work started.

## Task 3D — M2 trusted template identity and storage-safe IDs (complete)

- Files: `cpg-ingester/src/cpg_ingester/nodes/bpmn_ir_creator.py`, `cpg-ingester/src/cpg_ingester/generation.py`, `shared/src/cpg_contracts/automation/ir.py`, `shared/src/cpg_contracts/automation/templates.py`, `shared/src/cpg_contracts/automation/instances.py`, generated `shared/schemas/process-ir-1.0.json`, `shared/tests/automation/test_ir.py`, `shared/tests/automation/test_templates.py`, `shared/tests/automation/test_instances.py`, and `cpg-ingester/tests/test_bpmn_loop_mechanics.py`.
- Expected behavior: derive template identity only from trusted manifest ID/name; overwrite model-selected source CPG with `cpg_metadata.cpg_id`; escalate with a single coded static finding and no artifact write when identity metadata is absent; enforce storage-safe IDs across process IDs, template summary IDs, and template references while preserving valid existing IR hashes.
- Implemented: manifest `template_id` or deterministic manifest-name slug now owns process identity; `cpg_metadata.cpg_id` overwrites model-selected provenance; missing trusted identity returns a single coded escalation before LLM or artifact work; storage-safe validation covers process IDs, template summaries, and template references, including nested instance refs through `TemplateRef`. `shared/src/cpg_contracts/automation/instances.py` needed no source change. Valid existing IDs and IR hashes are preserved.
- Focused checks: shared IR/template/instance tests — 101 passed; schema-export test — 1 passed; BPMN loop mechanics — 34 passed. Regenerated with `shared/.venv/bin/python shared/scripts/export-ir-schema.py`; no golden identity changes.
- Component checks: PY-S — 447 passed; PY-I — 593 passed, 30 skipped, 9 deselected; PY-W — 767 passed, 6 skipped, 15 deselected.
- Other checks: `git diff --check` passed.
- Pending Sam checks: none for 3D. Independent review of the committed diff found no blocking issues; no unresolved implementation defects identified.
- Task 3D checkpoint: complete. Next eligible task is 3E; no 3E code work started.

## Task 3E — M3 bounded pattern matching (complete)

- Files: `shared/pyproject.toml`, new `shared/src/cpg_contracts/automation/validators/patterns.py`, `shared/src/cpg_contracts/automation/validators/ladder.py`, `shared/src/cpg_contracts/automation/validators/grounding.py`, new `shared/tests/automation/test_patterns.py`, and `shared/tests/automation/test_ladder.py`.
- Implemented: added the `regex` runtime dependency and a shared `VERSION0` full-match helper with 256-character pattern, 4096-character subject, 0.05-second timeout, and 128-entry compilation cache limits. Fixed safe errors cover malformed, oversized and timed-out patterns without exposing match inputs or exception details. L0 validates declared patterns even without defaults; ladder and template/instance grounding report typed errors at their validation rung.
- Focused checks: `shared/.venv/bin/python -m pytest tests/automation/test_patterns.py tests/automation/test_ladder.py tests/automation/test_grounding_template.py -q` — 54 passed. Includes boundaries, malformed patterns, deterministic timeout conversion, an isolated nested-quantifier timeout, L0 validation without defaults, and both grounding paths.
- Component checks: PY-S — 458 passed, 1 warning; PY-I — 593 passed, 30 skipped, 9 deselected; PY-W — 767 passed, 6 skipped, 15 deselected; PY-A — 18 passed, 2 warnings.
- Other checks: `git diff --check` passed. Refreshed shared dependencies in all four component environments; the automation venv was initialized per Appendix A. No lockfiles changed.
- Pending Sam checks: none for 3E. No blocking issues identified in this implementation review; final security-review finding status remains for Step 4.
- Task 3E checkpoint: complete. Next eligible task is 3F after independent review; no 3F work started. Changes remain unstaged.

## Task 3F — M4 bounded expressions (in progress)

- Files: `shared/src/cpg_contracts/automation/expressions.py`, `shared/src/cpg_contracts/automation/validators/ladder.py`, `shared/tests/automation/test_expressions_parse.py`, `shared/tests/automation/test_expressions_eval.py`, `shared/tests/automation/test_expressions_java.py`, and `shared/tests/automation/test_ladder.py`.
- Expected behavior: enforce 4096 source characters, 256 non-EOF tokens, 32 nested parentheses, 32 consecutive unary operators, and AST depth 64; reject limits with fixed `ExpressionError` messages; validate AST depth iteratively before recursive operations; catch parser `RecursionError`; report L0 `expression-limit` findings without invoking compilation.
- Checks planned: each boundary and one-over case, 5,000 nested parentheses, long unary and deep left-associated expressions, quoted parentheses, direct evaluation/typecheck/Java protection, and full PY-S/PY-I/PY-W/PY-A suites.
- Implemented: added the five fixed limits and `ExpressionLimitError`; bounds tokenization, parenthesis depth, and unary recursion; validates AST depth iteratively before parsing returns or recursive evaluation, typechecking, and Java generation; converts defensive parser recursion failures into a fixed limit error; maps expression-limit failures to L0 ladder findings before compilation.
- Focused checks: expression parse/eval/Java and ladder tests — 113 passed. Coverage includes limit boundaries and one-over cases, quoted parentheses, 5,000 nested parentheses, long unary and deep left-associated expressions, direct AST evaluation/typecheck/Java guards, and a ladder no-compilation assertion.
- Component checks: PY-S — 470 passed, 1 warning; PY-I — 593 passed, 30 skipped, 9 deselected; PY-W — 767 passed, 6 skipped, 15 deselected, 10 warnings; PY-A — 18 passed, 2 warnings.
- Other checks: `git diff --check` passed. No pending Sam checks for 3F; no blocking issues found in this checkpoint review. The independent security review and opening gate remain for Step 4/5.
- Task 3F checkpoint: complete. Next eligible task is 3G after review; no 3G work started. Changes remain unstaged.

## Task 3G — M5 bounded fuzzy grounding (in progress)

- Files: `shared/src/cpg_contracts/automation/validators/textmatch.py`, `shared/tests/automation/test_grounding_template.py`, and new `shared/tests/automation/test_textmatch_limits.py`.
- Expected behavior: check exact whole-token containment across the entire normalized section before fuzzy limits; cap fuzzy work at 80 citation tokens, 1000 section tokens, and 10000 proposed window comparisons; when a fuzzy cap is exceeded, return only the exact-match result without truncation or partial acceptance; preserve existing threshold semantics within the budget.
- Checks planned: zero matcher calls for each exceeded cap; no more than 10000 calls within the budget; exact match near the end of an over-limit section; long nonmatching citation rejection; existing short fuzzy match; no input mutation; PY-S/PY-I/PY-W.
- Implemented: exact normalized whole-token containment now uses padded joined strings before the fuzzy caps. Fuzzy windows run only within all three limits and only when their computed comparison count is at most 10000; over-limit fuzzy work returns false without truncation. Existing threshold calculation is unchanged.
- Focused checks: `env -u LITELLM_URL -u LLM_MODEL -u LITELLM_API_KEY -u OPENAI_API_KEY .venv/bin/python -m pytest tests/automation/test_textmatch_limits.py tests/automation/test_grounding_template.py -q` from `shared` — 39 passed. Coverage verifies zero matcher calls above each cap, comparison count within budget, exact match near the end of an over-limit section, nonmatching over-limit citation rejection, whole-token boundaries, unchanged abbreviations, and the existing short fuzzy match.
- Component checks: PY-S — 477 passed, 1 warning; PY-I — 593 passed, 30 skipped, 9 deselected; PY-W — 767 passed, 6 skipped, 15 deselected, 10 warnings.
- Other checks: `git diff --check` passed. The existing affected test file had no wall-clock-only one-second assertion to replace; deterministic work-count checks were added. No pending Sam checks or blocking issues found in this checkpoint review; independent security triage remains in Step 4/5.
- Task 3G checkpoint: complete. Next eligible task is Step 4 after review; no Step 4 work started. Changes remain unstaged.

## Step 4 — PR hygiene and short docs (complete)

- Files: `dev_docs/design/automation-design.md` (verbatim point-in-time copy with dated branch header), `dev_docs/README.md`, new `docs/automation.md`, `docs/README.md`, `deploy/README.md`, `AGENTS.md`, `cpg-ingester/README.md`, and root `README.md`. Root README was also corrected to satisfy the SonataFlow execution wording check and reflect the new ingester output. Existing `acp-writer/README.md` already documents `AUTOMATION_SERVICE_TOKEN`, the trusted-local-network limit, slice loopback binding, and the `local-embeddings` extra; no edit was needed there.
- Documentation covers the component path, validation ladder, all six requested environment variables, publication states and terminal `unconfigured` behavior, Compose-only service and trusted-network limits, known gaps, loader step 4 warnings, and the prompt/code-lookup rule. `BPMN_PREFLIGHT_URL` and the Automations tab are now documented in the ingester README.
- PY-S — 477 passed, 1 warning; PY-I — 593 passed, 30 skipped, 9 deselected; PY-W — 767 passed, 6 skipped, 15 deselected, 10 warnings; PY-A — 18 passed, 2 warnings.
- UI-S: typecheck passed; 6 files / 34 tests passed; build passed with the existing `bpmn-moddle` export warning. UI-I: local `tsc --noEmit` passed; 7 files / 21 tests passed; build passed with the existing large-chunk warning. UI-W: typecheck passed; 16 files / 54 tests passed; build passed with the existing large-chunk warning.
- HELM: all five chart lints passed (icon recommendations only). SHELL: loader syntax/tests passed all nine scenarios; slice access test passed; slice/OpenShell syntax checks passed.
- Other checks: design copy compared byte-for-byte to its source after the header; documentation index targets exist; `git diff --check` passed. `grep -rn "SonataFlow" README.md AGENTS.md` was reviewed and no longer describes SonataFlow as executing BPMN.
- Pending Sam checks: no Step 4-specific checks. Compose/Java/cluster proof and the Step 5 opening gate remain pending. Step 4 checkpoint complete; no Step 5 PR work started. Changes remain unstaged.

## Pre-Step 6 — callback retry and review-state fixes (complete)

- Scope: defects observed on run `eb982b9b765c`, checkout `57e3037a3b5543951f04dfd525180105c69d9a75`.
- Implemented: `post_callback` retries only connection/timeout failures up to three attempts with 2s/10s backoff, `(30, 60)` timeouts, stable CloudEvent IDs, and type/instance-only logs; BFF maps the care-plan gate only when `careplanReviewCount` is present and keeps FHIR review iterations out of the review gate.
- Tests: focused shared tests — 3 passed; focused writer tests — 13 passed; PY-S — 480 passed, 1 warning; PY-W — 770 passed, 6 skipped, 15 deselected, 10 warnings.
- Podman proof: rebuilt and recreated only `acp-patient-data`, `acp-llm-reasoning`, `acp-fhir-generation`, and `acp-writer-bff`; all four containers are running. Existing dependencies were left running.
- Checkpoint complete; no commit or push.
