# Run the operator end to end on OpenShift (development)

This is the operator-managed deployment path validated in `jrao-carewright` on
2026-10-22. It is separate from the component Helm/script path in
[`deploy/README.md`](../../deploy/README.md); do not run both in one namespace.
The bootstrap below deliberately enables an unauthenticated, plaintext
*cluster-internal* OpenShell gateway and development MinIO credentials. Do not
use it for production or real patient data.

Run all repository commands from the monorepo root. You need a logged-in
`oc` session with cluster-admin rights, `helm`, `jq`, `curl`, `openssl`, a
default StorageClass, and OpenShift OVN-Kubernetes. The bootstrap checks these
and [documents its overrides](bootstrap-prerequisites.md). Keep the Enmaas key
in a nonempty local file **outside the repository**; never put its value in a
CR, shell command, or committed manifest.

## 1. Bootstrap the external prerequisites

```sh
OPENSHELL_DEV_INSECURE=1 \
LLM_ORIGIN=https://api.enmaas.devshift.net \
LLM_MODEL=gpt-6-sol \
LLM_API_KEY_FILE=/absolute/path/to/enmaas-api-key \
carewright-operator/scripts/bootstrap-prerequisites.sh --namespace jrao-carewright
```

The script creates or reuses the namespace, Agent Sandbox and SonataFlow
operators, MinIO and the `cpg-artifacts`/`cpg-phi` buckets, the OpenShell
gateway and service proxy, the `SonataFlowPlatform`, credential Secrets, and
the `carewright-minio`/`carewright-openai` OpenShell profiles. It prints
the URLs and Secret references needed by the pipeline CRs. It does **not**
install carewright-operator or create either pipeline CR.

The shell variables above configure this *bootstrap invocation*. They do not
patch existing pipeline CRs. In particular, `LLM_ORIGIN` must be the bare URL
origin (no `/v1` path), and its host and port must match both the OpenShell
profile and `spec.llm.url` in **both** pipeline CRs. The application appends
`/v1` for inference. The profile ID remains `carewright-openai` even when
using Enmaas. A 403 `credential_endpoint_mismatch` means the profile still
authorizes another host; see [endpoint recovery](#existing-namespace-endpoint-recovery).

For a fresh namespace the script reads `LLM_API_KEY_FILE` and creates
`carewright-llm-credentials` with the key named `LLM_API_KEY`. On rerun it
**reuses** an existing compatible Secret; supplying a different key file does
not rotate that Secret. The script also creates `minio-credentials` and
`carewright-artifact-credentials` (including AWS-named aliases for SigV4).
Do not copy any credential values into `extraEnv`.

## 2. Install CRDs, the namespace-scoped operator, and pipeline CRs

```sh
oc apply -k carewright-operator/config/crd
oc apply -k carewright-operator/config/overlays/jrao-carewright
oc -n jrao-carewright rollout status deployment/carewright-operator-controller-manager --timeout=5m
oc apply -f carewright-operator/config/overlays/jrao-carewright/careplanwriter.yaml
oc apply -f carewright-operator/config/overlays/jrao-carewright/cpgingester.yaml
```

The overlay sets `WATCH_NAMESPACE=jrao-carewright`. Its two CR files carry
the gateway URL, workspace, MinIO URL and bucket names, Enmaas URL and
`gpt-6-sol`, 600-second LLM request timeout, Secret references, component
images, and the writer's embedding endpoint. For another namespace, make a
copy of the overlay and change **all** namespace-qualified URLs, metadata
namespaces, and `WATCH_NAMESPACE`; do not apply the `jrao-carewright`
overlay unchanged.

| Setting | Where to configure it |
| --- | --- |
| Enmaas API key | Local `LLM_API_KEY_FILE` at first bootstrap; stored as `LLM_API_KEY` in `carewright-llm-credentials` |
| Authorized inference endpoint | Bootstrap `LLM_ORIGIN` / OpenShell `carewright-openai` profile, and both CRs' `spec.llm.url` |
| Model and timeout | Both CRs' `spec.llm.model` and `spec.llm.requestTimeoutSeconds`; `LLM_MODEL` is only a bootstrap output hint |
| Artifact store | Bootstrap `ARTIFACT_BUCKET`, `PHI_BUCKET` (if customized), then both CRs' `spec.artifactStore.*` |
| Gateway | Both CRs' `spec.sandbox.gateway.endpoint` and `insecure`; the operator's gateway environment fallback is not needed here |
| Operator watch scope | `WATCH_NAMESPACE` in the operator overlay Deployment |
| Embedding service | `CarePlanWriter.spec.llmReasoning.embedding.*`; `EMBEDDING_API_KEY: unused` is a non-secret placeholder for the unauthenticated test service |

Do not edit generated `SandboxRequest`, policy ConfigMap, SonataFlow,
Service, Route, or BFF/UI Deployment resources. The operator derives them from
the two parent CRs and will reconcile manual edits away.

Check the deployment:

```sh
oc -n jrao-carewright get cpgingester,careplanwriter,sandboxrequest,sonataflow
oc -n jrao-carewright get deployment/minio,statefulset/jrao-carewright-openshell
oc -n jrao-carewright get route cpg-ingester-ui care-plan-writer-ui
```

The current writer image set has an unrelated MCP import failure, so
`CarePlanWriter Ready=False` (`7 of 8 backend components`) can coexist with
a working core care-plan workflow. Investigate any *other* SandboxRequest
failure before testing. Do not treat this exception as production readiness.

## 3. Run the synthetic CPG-to-CarePlan smoke test

In one terminal, forward the ingester BFF:

```sh
oc -n jrao-carewright port-forward service/cpg-ingester-bff 18081:80
```

From another terminal, upload the repository's synthetic guideline:

```sh
curl -fsS -F 'pdf=@cpg-ingester/data/synthetic-hypertension-cpg.pdf;type=application/pdf' \
  http://127.0.0.1:18081/api/v1/upload
```

The response contains `runId`. Open the `cpg-ingester-ui` Route returned by
`oc get route`; inspect and submit each manifest/artifact review gate. The
workflow pauses at those gates until a human acts. Poll
`GET http://127.0.0.1:18081/api/v1/runs/<runId>` until its status is
`completed`; this delivers the ingested guideline to CarePlanWriter.

To exercise BPMN generation specifically, upload
`cpg-ingester/data/synthetic-hypertension-cpg-v2.pdf` with BPMN-capable
component image tags. At the artifact review gate, inspect the **Automations**
tab and review any generated templates. Accepted templates are delivered through
the same artifact notification; no additional operator-managed sandbox or
SonataFlow resource is created for each template. Kogito preflight is optional
and remains disabled unless `BPMN_PREFLIGHT_URL` is configured for the ingester
LLM-analysis sandbox together with appropriate egress policy.

Then forward the writer BFF in a separate terminal:

```sh
oc -n jrao-carewright port-forward service/care-plan-writer-bff 18082:80
```

Start a run with the synthetic hypertension IPS bundle:

```sh
jq -n --slurpfile ips acp-writer/benchmarks/bundles/htn-temporal-01.json \
  '{ipsBundle:$ips[0]}' |
  curl -fsS -H 'Content-Type: application/json' --data-binary @- \
    http://127.0.0.1:18082/api/v1/runs
```

Open the `care-plan-writer-ui` Route and submit the care-plan review when
requested. Poll `GET /api/v1/runs/<runId>`; success is `status=completed`
with a `careplanId`. Check `GET /api/v1/status` for guideline,
recommendation, and deployed decision-model counts. The 2026-10-22 synthetic
run completed and produced a CarePlan, but reported zero deployed decision
models: it did **not** validate DMN execution.

## Existing-namespace endpoint recovery

Bootstrap intentionally refuses to overwrite an existing
`carewright-openai` profile with a different endpoint. If an existing
namespace was initialized for `api.openai.com`, update that profile
explicitly before rerunning with `LLM_ORIGIN=https://api.enmaas.devshift.net`.
The Secret and provider instances do not need their values printed or copied.
In one terminal:

```sh
oc -n jrao-carewright port-forward statefulset/jrao-carewright-openshell 18080:8080
```

In another terminal from the repository root:

```sh
gateway=http://127.0.0.1:18080
profile_dir="$(mktemp -d)"
profile_file="$profile_dir/carewright-openai.yaml"
sed -e 's/__LLM_HOST__/api.enmaas.devshift.net/' -e 's/__LLM_PORT__/443/' \
  carewright-operator/config/bootstrap/profiles/carewright-openai.yaml.tmpl > "$profile_file"
version="$(openshell provider profile export carewright-openai \
  --gateway-endpoint "$gateway" --workspace default -o json | jq -er '.resource_version')"
printf 'resource_version: %s\n' "$version" >> "$profile_file"
openshell provider profile update carewright-openai --file "$profile_file" \
  --gateway-endpoint "$gateway" --workspace default
openshell provider profile export carewright-openai \
  --gateway-endpoint "$gateway" --workspace default -o json |
  jq '{id,endpoints}'
rm -- "$profile_file"
rmdir -- "$profile_dir"
```

The endpoint should be only `api.enmaas.devshift.net:443`. To test from
within a running sandbox, use `openshell sandbox exec`; `oc exec` enters
the outer workload container and does not exercise the same sandbox network
path. A read-only `GET /v1/models` from the LLM sandbox returned HTTP 200
after the profile update. Never print the injected key.

## Image and failure notes

The checked-in overlays use `:latest` for fresh installs. Operator and
component images are built by separate GitHub Actions workflows; check both
builds before deployment. Moving the registry's `:latest` tag does **not**
change a CR's spec, so it does not replace an existing sandbox. For an
upgrade, change every relevant `spec.<component>.image` to the newly built
immutable short-SHA tag, apply the CRs, and wait for replacement
SandboxRequests. Conversely, applying these `:latest` samples to the
SHA-pinned namespace *will* change its CR specs and trigger another rollout.

If an ingester run stops at LLM analysis, check the workflow and
`cpg-ingester-llm-analysis` sandbox logs for an error before changing
MinIO. A Ready parent CR means its components are available; it does not
guarantee that an individual run completed.
