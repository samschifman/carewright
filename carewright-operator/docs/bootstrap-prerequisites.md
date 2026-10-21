# Bootstrap the external prerequisites

Run `scripts/bootstrap-prerequisites.sh` **before** installing the
carewright-operator or creating CPGIngester/CarePlanWriter resources. The script
is for development OpenShift clusters: it installs OpenShell 0.1.3 with
unauthenticated plaintext access on its *cluster-internal* Service. Do not use
this configuration on a shared production cluster.

The installer needs `oc`, `helm`, `jq`, `curl`, `openssl`, and cluster-admin
permissions. It checks for an OpenShift OVN-Kubernetes CNI, a default
StorageClass, and a logged-in `oc` session. A different CNI is allowed only
after independently verifying ingress **and** egress NetworkPolicy enforcement
and setting `CONFIRM_NETWORK_POLICY_ENFORCEMENT=1`. The OpenShell CLI is
downloaded from the matching tagged GitHub release and checksum-verified if a
matching CLI is not already installed. Set `OPENSHELL_CLI` to a matching binary
to avoid the download.

For a fresh namespace, put the LLM key in a local file outside this repository
and run:

```sh
OPENSHELL_DEV_INSECURE=1 \
LLM_API_KEY_FILE=/private/path/to/llm-api-key \
scripts/bootstrap-prerequisites.sh --namespace jrao-carewright
```

If the namespace already contains `llm-api-key` with `OPENAI_API_KEY` or
`LLM_API_KEY`, `LLM_API_KEY_FILE` is optional. The script creates
`carewright-llm-credentials` with the `LLM_API_KEY` key for operator-managed
provider registration. It never prints key values. Existing credential Secrets
are reused; on rerun it adds or refreshes the two AWS-named aliases in
`carewright-artifact-credentials` from its existing `ARTIFACT_STORE_*` keys,
without changing those legacy keys. A missing MinIO root password is
generated and stored in the `minio-credentials` Secret. For this first-pass
development setup, `carewright-artifact-credentials` copies those root
credentials under both the legacy `ARTIFACT_STORE_*` keys and the AWS-named
keys required for OpenShell SigV4 signing when that Secret is absent. An
existing compatible Secret is reused, including one backed by a separately
scoped MinIO account. Use a scoped account before treating this as
production-ready.

On a fresh cluster, the script installs the pinned Agent Sandbox controller and
the SonataFlow Operator from the available OpenShift OperatorHub catalog. It
then creates the MinIO PVC, Deployment, Service, `cpg-artifacts` and `cpg-phi`
buckets; installs OpenShell from its pinned Helm chart; imports the
`carewright-minio` and `carewright-openai` **profiles**; creates a cluster-only
`openshell-service-proxy` Deployment and Service for sandbox HTTP; creates a
`SonataFlowPlatform`; and grants the sandbox and default workload ServiceAccounts
image-pull access from `IMAGE_SOURCE_NAMESPACE` if that namespace exists. Provider *instances*
are deliberately not created here: the SandboxRequest controller creates them
from the referenced Secrets when pipeline CRs use `credentials`.

OpenShell 0.1.3 returns sandbox service URLs below `openshell.localhost`. That
name resolves to loopback in ordinary Pods, so the operator detects the
bootstrap-managed Service and publishes in-cluster URLs through it. No extra
operator setting is required. `OPENSHELL_SERVICE_PROXY_URL` can override the
proxy base URL for a custom deployment.

The proxy accepts `/<sandbox>--<service>/...`, restores the gateway's virtual
Host header, and forwards to the OpenShell gateway Service. It has no public
Route. A NetworkPolicy limits callers to operator-managed pipeline Pods and
OpenShell supervisors. Requests still traverse the gateway and supervisor;
they do not connect directly to sandbox workload Pods. `OPENSHELL_SERVICE_PROXY_IMAGE`
overrides the default Red Hat UBI Nginx image if your cluster needs a mirror.

The script reuses compatible resources on rerun. It updates an existing
`carewright-minio` profile if its credential declarations or MinIO SigV4
signing settings differ from the bootstrap template. It updates the image of a
MinIO Deployment it created when `MINIO_IMAGE` changes, but does not replace
the Deployment, its storage, or the image of a separately managed MinIO
Deployment. It does not perform a gateway-version migration. When upgrading an
existing 0.1.1 Helm release to 0.1.3, first back up any sandbox workspace data
you need and delete the old sandboxes, then upgrade the chart with
`--reset-then-reuse-values` so the new `gatewayConfig.openshell.version = 2`
default is included. `--reuse-values` alone can omit that header and prevent
the gateway from starting. A compatible
non-Helm OpenShell gateway can be reused without being adopted into Helm; set
`OPENSHELL_RELEASE` if its name differs from `<namespace>-openshell`. The script
stops with a conflict message when it cannot safely reuse a resource. The SonataFlow package, channel, and catalog source
can be overridden with `SONATAFLOW_PACKAGE`, `SONATAFLOW_CHANNEL`, and
`SONATAFLOW_SOURCE` if auto-discovery does not fit your cluster. The pinned
MinIO image defaults to the public `quay.io/jrao/minio` mirror pinned to an
`amd64` digest from the `RELEASE.2025-09-07T16-13-09Z` image. Set
`MINIO_IMAGE` to another pullable image for other architectures or registries;
it must include `/usr/bin/mc` for bucket initialization. `STORAGE_CLASS`,
`MINIO_STORAGE`, `AGENT_SANDBOX_VERSION`, `AGENT_SANDBOX_MANIFEST` (a
local or mirrored manifest), `OPENSHELL_VERSION`, `LLM_ORIGIN`,
`LLM_MODEL`, and `IMAGE_SOURCE_NAMESPACE` are also configurable. Set
`EXPOSE_MINIO_CONSOLE=1` for an optional HTTPS MinIO console Route; the in-cluster
MinIO API Service is always created.

The final output gives the gateway URL, MinIO URL and buckets, workspace, and
the `credentials` references for later pipeline CRs. For example:

```yaml
artifactStore:
  url: http://minio.jrao-carewright.svc.cluster.local:9000
  artifactBucket: cpg-artifacts
  credentials:
    type: carewright-minio
    secretRef:
      name: carewright-artifact-credentials
llm:
  url: https://api.openai.com
  model: gpt-5.6
  credentials:
    type: carewright-openai
    secretRef:
      name: carewright-llm-credentials
```

The `jrao-carewright` overlay targets `https://api.enmaas.devshift.net` with
model `rits/zai-org/glm-5-3`. For a fresh bootstrap using that overlay, set
`LLM_ORIGIN=https://api.enmaas.devshift.net` and
`LLM_MODEL=rits/zai-org/glm-5-3`, and provide an API key for that service.
Configure the bare origin here and in `spec.llm.url`: the application appends
`/v1` to inference requests. The profile ID remains `carewright-openai` for
compatibility, but its endpoint must match the origin. The bootstrap script
stops if an existing profile points to a different host; change that profile
explicitly before rerunning it with the new `LLM_ORIGIN`.

The script does **not** install carewright-operator, its CRDs, pipeline CRs,
SandboxRequests, per-sandbox egress policies, UI/BFF Deployments, or UI/workflow
Routes. Those are the operator's responsibility after bootstrap. The checked-in
pipeline samples still use managed MinIO and externally registered LLM
`credentialsProvider` names; change them to the printed external URL and
`credentials` blocks before applying them with this bootstrap setup.
