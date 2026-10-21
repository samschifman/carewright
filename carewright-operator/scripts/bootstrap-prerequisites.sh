#!/usr/bin/env bash
# Prepare a development OpenShift namespace for carewright-operator.
# This script does not install the operator or create pipeline/provider instances.
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"

namespace="${CAREWRIGHT_NAMESPACE:-carewright}"
workspace="${OPENSHELL_WORKSPACE:-default}"
release="${OPENSHELL_RELEASE:-}"
openshell_version="${OPENSHELL_VERSION:-0.1.3}"
openshell_port="${OPENSHELL_LOCAL_PORT:-18080}"
service_proxy_image="${OPENSHELL_SERVICE_PROXY_IMAGE:-registry.access.redhat.com/ubi9/nginx-124:latest}"
minio_image="${MINIO_IMAGE:-quay.io/jrao/minio@sha256:9c25bac3d44ef57943e1ed9ca808a6c6c04d128a5e38937db6818a16a4aa5473}"
minio_storage="${MINIO_STORAGE:-5Gi}"
storage_class="${STORAGE_CLASS:-}"
artifact_bucket="${ARTIFACT_BUCKET:-cpg-artifacts}"
phi_bucket="${PHI_BUCKET:-cpg-phi}"
llm_origin="${LLM_ORIGIN:-https://api.openai.com}"
llm_model="${LLM_MODEL:-gpt-5.6}"
llm_key_file="${LLM_API_KEY_FILE:-}"
minio_password_file="${MINIO_ROOT_PASSWORD_FILE:-}"
minio_root_user="${MINIO_ROOT_USER:-carewright-admin}"
image_source_namespace="${IMAGE_SOURCE_NAMESPACE:-carewright}"
agent_sandbox_version="${AGENT_SANDBOX_VERSION:-v0.5.4}"
agent_sandbox_manifest="${AGENT_SANDBOX_MANIFEST:-}"
sonataflow_operator_namespace="${SONATAFLOW_OPERATOR_NAMESPACE:-openshift-serverless-logic}"
sonataflow_package="${SONATAFLOW_PACKAGE:-}"
sonataflow_channel="${SONATAFLOW_CHANNEL:-}"
sonataflow_source="${SONATAFLOW_SOURCE:-}"
openshell_cli="${OPENSHELL_CLI:-}"
expose_minio_console="${EXPOSE_MINIO_CONSOLE:-0}"
port_forward_pid=""
temporary_dir=""

usage() {
  cat <<'USAGE'
Usage: OPENSHELL_DEV_INSECURE=1 LLM_API_KEY_FILE=/private/path/key \
       scripts/bootstrap-prerequisites.sh [--namespace NAME]

Bootstraps Agent Sandbox, SonataFlow, MinIO, and OpenShell before the
carewright operator is installed. Existing compatible resources are reused.
The explicit OPENSHELL_DEV_INSECURE=1 opt-in enables unauthenticated plaintext
gateway access within the cluster; do not use it on a shared production cluster.

Common settings (environment variables):
  CAREWRIGHT_NAMESPACE       Target namespace (default: carewright)
  LLM_API_KEY_FILE           File containing the LLM key. Alternatively, an
                             existing llm-api-key Secret with OPENAI_API_KEY
                             or LLM_API_KEY is reused.
  MINIO_ROOT_PASSWORD_FILE   Optional file with MinIO root password; generated
                             when minio-credentials does not already exist.
  MINIO_IMAGE                Pullable MinIO image (pinned default shown in script)
  STORAGE_CLASS              StorageClass for MinIO and sandbox workspaces
  IMAGE_SOURCE_NAMESPACE     Namespace with pipeline images (default: carewright)
  LLM_ORIGIN                 Profile endpoint (default: https://api.openai.com)
  LLM_MODEL                  Pipeline model hint (default: gpt-5.6)
  EXPOSE_MINIO_CONSOLE       Set to 1 to create a console Route
  OPENSHELL_RELEASE          Gateway name (default: <namespace>-openshell)
  OPENSHELL_SERVICE_PROXY_IMAGE  Internal sandbox-service proxy image
  OPENSHELL_CLI              Optional matching-version OpenShell CLI path
  AGENT_SANDBOX_MANIFEST     Optional local path or mirrored release manifest
  SONATAFLOW_PACKAGE/CHANNEL/SOURCE  Override auto-discovered OLM package
USAGE
}

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
  if [[ -n "$port_forward_pid" ]]; then
    kill "$port_forward_pid" 2>/dev/null || true
    wait "$port_forward_pid" 2>/dev/null || true
  fi
  if [[ -n "$temporary_dir" && -d "$temporary_dir" ]]; then
    rm -r -- "$temporary_dir"
  fi
}
trap cleanup EXIT

exists() { oc -n "$namespace" get "$1" "$2" >/dev/null 2>&1; }
require() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

while (($#)); do
  case "$1" in
    --namespace) (($# >= 2)) || die '--namespace requires a value'; namespace="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

release="${release:-${namespace}-openshell}"
[[ "$namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid namespace'
[[ "$workspace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid workspace'
[[ "$release" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid OpenShell release name'
((${#release} <= 53)) || die 'OpenShell Helm release name must be at most 53 characters'
[[ "$artifact_bucket" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid artifact bucket'
[[ "$phi_bucket" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid PHI bucket'
[[ "$minio_storage" =~ ^[0-9]+(Mi|Gi|Ti)$ ]] || die 'MINIO_STORAGE must be a quantity such as 5Gi'
[[ "$openshell_port" =~ ^[0-9]+$ ]] || die 'OPENSHELL_LOCAL_PORT must be numeric'
((openshell_port >= 1 && openshell_port <= 65534)) || die 'OPENSHELL_LOCAL_PORT must be between 1 and 65534'
openshell_health_port=$((openshell_port + 1))
[[ "$minio_root_user" =~ ^[A-Za-z0-9_-]{3,}$ ]] || die 'MINIO_ROOT_USER must be at least three simple characters'
[[ "$minio_image" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die 'MINIO_IMAGE must be one image reference without whitespace'
[[ "$service_proxy_image" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die 'OPENSHELL_SERVICE_PROXY_IMAGE must be one image reference without whitespace'
[[ "$llm_model" =~ ^[A-Za-z0-9._:/-]+$ ]] || die 'Invalid LLM_MODEL'
[[ -z "$storage_class" || "$storage_class" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || die 'Invalid STORAGE_CLASS'
[[ -z "$image_source_namespace" || "$image_source_namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid IMAGE_SOURCE_NAMESPACE'
[[ "$sonataflow_operator_namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid SONATAFLOW_OPERATOR_NAMESPACE'
[[ -z "$sonataflow_package" || "$sonataflow_package" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid SONATAFLOW_PACKAGE'
[[ -z "$sonataflow_channel" || "$sonataflow_channel" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || die 'Invalid SONATAFLOW_CHANNEL'
[[ -z "$sonataflow_source" || "$sonataflow_source" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || die 'Invalid SONATAFLOW_SOURCE'
[[ "$agent_sandbox_version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'AGENT_SANDBOX_VERSION must be a release tag'
[[ "$openshell_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'OPENSHELL_VERSION must be a release version'
[[ "${OPENSHELL_DEV_INSECURE:-0}" == 1 ]] || die 'Set OPENSHELL_DEV_INSECURE=1 to acknowledge plaintext unauthenticated development gateway access'
[[ "$llm_origin" =~ ^(https?)://([A-Za-z0-9.-]+)(:([0-9]{1,5}))?/?$ ]] || die 'LLM_ORIGIN must be a URL origin without a path'
llm_scheme="${BASH_REMATCH[1]}"
llm_host="${BASH_REMATCH[2]}"
llm_port="${BASH_REMATCH[4]:-}"
if [[ -z "$llm_port" ]]; then
  if [[ "$llm_scheme" == https ]]; then llm_port=443; else llm_port=80; fi
fi
((llm_port >= 1 && llm_port <= 65535)) || die 'LLM_ORIGIN has an invalid port'

for tool in oc helm jq curl sed awk tar openssl; do require "$tool"; done
[[ -z "$llm_key_file" || -s "$llm_key_file" ]] || die 'LLM_API_KEY_FILE does not exist or is empty'
[[ -z "$minio_password_file" || -s "$minio_password_file" ]] || die 'MINIO_ROOT_PASSWORD_FILE does not exist or is empty'
oc whoami >/dev/null || die 'Not logged in to an OpenShift cluster'
temporary_dir="$(mktemp -d)"
chmod 700 "$temporary_dir"

prepare_cli() {
  if [[ -z "$openshell_cli" ]] && command -v openshell >/dev/null 2>&1; then
    openshell_cli="$(command -v openshell)"
  fi
  if [[ -n "$openshell_cli" && "$("$openshell_cli" --version 2>/dev/null || true)" == "openshell $openshell_version" ]]; then
    return
  fi
  if [[ -n "${OPENSHELL_CLI:-}" ]]; then
    die "OPENSHELL_CLI must report openshell $openshell_version"
  fi
  local target archive checksums expected actual
  case "$(uname -s)/$(uname -m)" in
    Darwin/arm64) target=aarch64-apple-darwin ;;
    Linux/x86_64) target=x86_64-unknown-linux-musl ;;
    Linux/aarch64|Linux/arm64) target=aarch64-unknown-linux-musl ;;
    *) die 'No published OpenShell CLI binary for this host; set OPENSHELL_CLI' ;;
  esac
  archive="openshell-${target}.tar.gz"
  checksums="$temporary_dir/openshell-checksums-sha256.txt"
  log "Downloading OpenShell CLI $openshell_version"
  curl -fsSL --retry 3 "https://github.com/NVIDIA/OpenShell/releases/download/v${openshell_version}/${archive}" -o "$temporary_dir/$archive"
  curl -fsSL --retry 3 "https://github.com/NVIDIA/OpenShell/releases/download/v${openshell_version}/openshell-checksums-sha256.txt" -o "$checksums"
  expected="$(awk -v name="$archive" '$2 == name || $2 == "*" name {print $1; exit}' "$checksums")"
  [[ "$expected" =~ ^[[:xdigit:]]{64}$ ]] || die "No valid checksum for $archive"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$temporary_dir/$archive" | awk '{print $1}')"
  else
    require shasum
    actual="$(shasum -a 256 "$temporary_dir/$archive" | awk '{print $1}')"
  fi
  [[ "$actual" == "$expected" ]] || die 'OpenShell CLI checksum mismatch'
  tar -xzf "$temporary_dir/$archive" -C "$temporary_dir" openshell
  openshell_cli="$temporary_dir/openshell"
  [[ "$("$openshell_cli" --version)" == "openshell $openshell_version" ]] || die 'Downloaded CLI version mismatch'
}
prepare_cli

preflight_credentials() {
  if [[ -z "$llm_key_file" ]] &&
     ! oc -n "$namespace" get secret carewright-llm-credentials >/dev/null 2>&1 &&
     ! oc -n "$namespace" get secret llm-api-key >/dev/null 2>&1; then
    die "Provide LLM_API_KEY_FILE for a fresh namespace (or precreate llm-api-key)"
  fi
}
preflight_credentials

ensure_namespace() {
  if oc get namespace "$namespace" >/dev/null 2>&1; then
    log "Reusing namespace $namespace"
  else
    oc create namespace "$namespace"
  fi
}

gateway_compatible() {
  exists statefulset "$release" && exists service "$release" &&
    exists configmap "${release}-config" && exists serviceaccount "${release}-sandbox" &&
    exists networkpolicy "${release}-sandbox-ssh" || return 1
  oc get statefulset "$release" -n "$namespace" -o json | jq -e --arg version "$openshell_version" '
    .metadata.labels["app.kubernetes.io/version"] == $version and
    any(.spec.template.spec.containers[]; .name == "openshell-gateway" and (.image | endswith(":" + $version)))
  ' >/dev/null || return 1
  oc get configmap "${release}-config" -n "$namespace" -o json | jq -e --arg ns "$namespace" '
    (.data["gateway.toml"] // "") as $config |
    ($config | contains("disable_tls            = true")) and
    ($config | contains("allow_unauthenticated_users = true")) and
    ($config | contains("namespace                    = " + ($ns | @json)))
  ' >/dev/null
}

preflight_openshell_ownership() {
  if helm status "$release" -n "$namespace" >/dev/null 2>&1; then return; fi
  if exists statefulset "$release" || exists service "$release"; then
    gateway_compatible || die "Existing $release gateway is not a compatible OpenShell $openshell_version development installation; refusing to replace it"
    log "Will reuse compatible non-Helm gateway $release without adopting it"
  fi
}

ensure_storage_class() {
  if [[ -z "$storage_class" ]]; then
    storage_class="$(oc get storageclass -o json | jq -r '[.items[] | select(.metadata.annotations["storageclass.kubernetes.io/is-default-class"] == "true" or .metadata.annotations["storageclass.beta.kubernetes.io/is-default-class"] == "true") | .metadata.name][0] // empty')"
  fi
  [[ -n "$storage_class" ]] || die 'No default StorageClass; set STORAGE_CLASS'
  oc get storageclass "$storage_class" >/dev/null || die "StorageClass $storage_class does not exist"
  log "Using StorageClass $storage_class"
}

ensure_network_policy_cni() {
  local cni
  cni="$(oc get network.operator.openshift.io cluster -o jsonpath='{.spec.defaultNetwork.type}' 2>/dev/null || true)"
  if [[ "$cni" != OVNKubernetes && "${CONFIRM_NETWORK_POLICY_ENFORCEMENT:-0}" != 1 ]]; then
    die "Cannot confirm ingress and egress NetworkPolicy enforcement for CNI '$cni'; set CONFIRM_NETWORK_POLICY_ENFORCEMENT=1 after verifying it"
  fi
  log "NetworkPolicy CNI: ${cni:-manually confirmed}"
}

ensure_agent_sandbox() {
  if oc get crd sandboxes.agents.x-k8s.io >/dev/null 2>&1; then
    log 'Reusing Agent Sandbox CRD'
  else
    log "Installing Agent Sandbox $agent_sandbox_version"
    oc apply -f "${agent_sandbox_manifest:-https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${agent_sandbox_version}/sandbox.yaml}"
  fi
  oc wait --for=condition=Established crd/sandboxes.agents.x-k8s.io --timeout=180s
  oc rollout status deployment/agent-sandbox-controller -n agent-sandbox-system --timeout=300s || die 'Agent Sandbox controller is not ready'
}

ensure_sonataflow_operator() {
  if oc get crd sonataflows.sonataflow.org >/dev/null 2>&1 &&
     oc get crd sonataflowplatforms.sonataflow.org >/dev/null 2>&1; then
    oc get deployments -A -o json | jq -e '
      any(.items[]; (.metadata.name | test("logic-operator|sonataflow")) and (.status.availableReplicas // 0) > 0)
    ' >/dev/null || die 'SonataFlow CRDs exist but no ready operator controller was found'
    log 'Reusing ready SonataFlow Operator and CRDs'
    return
  fi

  local existing package_json candidate channel source source_namespace installed_csv
  existing="$(oc get subscriptions.operators.coreos.com -A -o json | jq -r '[.items[] | select(.spec.name == "logic-operator" or .spec.name == "logic-operator-rhel8") | [.metadata.namespace, .metadata.name] | @tsv][0] // empty')"
  if [[ -n "$existing" ]]; then
    read -r sonataflow_operator_namespace existing_name <<<"$existing"
    log "Waiting for existing SonataFlow subscription $sonataflow_operator_namespace/$existing_name"
  else
    if [[ -z "$sonataflow_package" ]]; then
      for candidate in logic-operator logic-operator-rhel8; do
        if oc get packagemanifest "$candidate" -n openshift-marketplace -o json >"$temporary_dir/package.json" 2>/dev/null; then
          sonataflow_package="$candidate"
          break
        fi
      done
    else
      oc get packagemanifest "$sonataflow_package" -n openshift-marketplace -o json >"$temporary_dir/package.json" || die "SonataFlow package $sonataflow_package is unavailable"
    fi
    [[ -n "$sonataflow_package" ]] || die 'No SonataFlow Operator package in OperatorHub; set SONATAFLOW_PACKAGE/CHANNEL/SOURCE or install it separately'
    package_json="$temporary_dir/package.json"
    channel="$sonataflow_channel"
    if [[ -z "$channel" ]]; then
      channel="$(jq -r '[.status.channels[].name] | if index("stable") then "stable" elif index("alpha") then "alpha" else .[0] // empty end' "$package_json")"
    fi
    [[ -n "$channel" ]] || die "No channel found for $sonataflow_package"
    jq -e --arg channel "$channel" '.status.channels | any(.name == $channel)' "$package_json" >/dev/null || die "Channel $channel is unavailable for $sonataflow_package"
    source="${sonataflow_source:-$(jq -r '.status.catalogSource' "$package_json")}"
    [[ -n "$source" && "$source" != null ]] || die "No catalog source found for $sonataflow_package"
    source_namespace="$(jq -r '.status.catalogSourceNamespace // "openshift-marketplace"' "$package_json")"
    if ! oc get namespace "$sonataflow_operator_namespace" >/dev/null 2>&1; then
      oc create namespace "$sonataflow_operator_namespace"
    fi
    if [[ "$(oc get operatorgroups.operators.coreos.com -n "$sonataflow_operator_namespace" -o json | jq '.items | length')" == 0 ]]; then
      oc create -f - <<EOF
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: carewright-sonataflow
  namespace: $sonataflow_operator_namespace
spec: {}
EOF
    fi
    log "Installing SonataFlow Operator $sonataflow_package ($channel)"
    oc create -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: carewright-sonataflow
  namespace: $sonataflow_operator_namespace
spec:
  channel: $channel
  installPlanApproval: Automatic
  name: $sonataflow_package
  source: $source
  sourceNamespace: $source_namespace
EOF
    existing_name=carewright-sonataflow
  fi

  for _ in {1..90}; do
    installed_csv="$(oc get subscription "$existing_name" -n "$sonataflow_operator_namespace" -o jsonpath='{.status.installedCSV}' 2>/dev/null || true)"
    if [[ -n "$installed_csv" ]] && [[ "$(oc get csv "$installed_csv" -n "$sonataflow_operator_namespace" -o jsonpath='{.status.phase}' 2>/dev/null || true)" == Succeeded ]]; then
      break
    fi
    sleep 5
  done
  [[ -n "${installed_csv:-}" ]] && [[ "$(oc get csv "$installed_csv" -n "$sonataflow_operator_namespace" -o jsonpath='{.status.phase}' 2>/dev/null || true)" == Succeeded ]] || die 'SonataFlow Operator subscription did not become ready'
  oc wait --for=condition=Established crd/sonataflows.sonataflow.org crd/sonataflowplatforms.sonataflow.org --timeout=180s
}

ensure_minio_secrets() {
  local artifact_secret
  if exists secret minio-credentials; then
    oc get secret minio-credentials -n "$namespace" -o json | jq -e '.data.MINIO_ROOT_USER and .data.MINIO_ROOT_PASSWORD' >/dev/null || die 'Existing minio-credentials lacks MINIO_ROOT_USER or MINIO_ROOT_PASSWORD'
    log 'Reusing minio-credentials Secret'
  else
    printf %s "$minio_root_user" >"$temporary_dir/minio-user"
    if [[ -n "$minio_password_file" ]]; then
      tr -d '\r\n' <"$minio_password_file" >"$temporary_dir/minio-password"
    else
      openssl rand -hex 24 | tr -d '\n' >"$temporary_dir/minio-password"
    fi
    [[ "$(wc -c <"$temporary_dir/minio-password")" -ge 8 ]] || die 'MinIO password must be at least eight characters'
    oc create secret generic minio-credentials -n "$namespace" \
      --from-file=MINIO_ROOT_USER="$temporary_dir/minio-user" \
      --from-file=MINIO_ROOT_PASSWORD="$temporary_dir/minio-password"
  fi

  if exists secret carewright-artifact-credentials; then
    artifact_secret="$temporary_dir/artifact-credentials.json"
    oc get secret carewright-artifact-credentials -n "$namespace" -o json >"$artifact_secret"
    jq -e '.data.ARTIFACT_STORE_ACCESS_KEY and .data.ARTIFACT_STORE_SECRET_KEY' "$artifact_secret" >/dev/null || die 'Existing artifact credential Secret has incompatible keys'
    if jq -e '.data.AWS_ACCESS_KEY_ID == .data.ARTIFACT_STORE_ACCESS_KEY and .data.AWS_SECRET_ACCESS_KEY == .data.ARTIFACT_STORE_SECRET_KEY' "$artifact_secret" >/dev/null; then
      log 'Reusing carewright-artifact-credentials Secret'
    else
      jq -c '{data:{AWS_ACCESS_KEY_ID:.data.ARTIFACT_STORE_ACCESS_KEY,AWS_SECRET_ACCESS_KEY:.data.ARTIFACT_STORE_SECRET_KEY}}' "$artifact_secret" >"$temporary_dir/artifact-credentials-patch.json"
      oc patch secret carewright-artifact-credentials -n "$namespace" --type=merge --patch-file "$temporary_dir/artifact-credentials-patch.json" >/dev/null
      log 'Added AWS credential aliases to carewright-artifact-credentials Secret'
    fi
  else
    oc get secret minio-credentials -n "$namespace" -o json | jq -e --arg ns "$namespace" '
      {apiVersion:"v1", kind:"Secret", metadata:{name:"carewright-artifact-credentials",namespace:$ns,labels:{"app.kubernetes.io/managed-by":"carewright-bootstrap"}},type:"Opaque",data:{ARTIFACT_STORE_ACCESS_KEY:.data.MINIO_ROOT_USER,ARTIFACT_STORE_SECRET_KEY:.data.MINIO_ROOT_PASSWORD,AWS_ACCESS_KEY_ID:.data.MINIO_ROOT_USER,AWS_SECRET_ACCESS_KEY:.data.MINIO_ROOT_PASSWORD}}
    ' | oc create -f -
  fi

  if exists secret carewright-llm-credentials; then
    oc get secret carewright-llm-credentials -n "$namespace" -o json | jq -e '.data.LLM_API_KEY' >/dev/null || die 'Existing LLM credential Secret lacks LLM_API_KEY'
    log 'Reusing carewright-llm-credentials Secret'
  elif [[ -n "$llm_key_file" ]]; then
    tr -d '\r\n' <"$llm_key_file" >"$temporary_dir/llm-key"
    [[ -s "$temporary_dir/llm-key" ]] || die 'LLM key file is empty after trimming newlines'
    oc create secret generic carewright-llm-credentials -n "$namespace" --from-file=LLM_API_KEY="$temporary_dir/llm-key"
  elif exists secret llm-api-key; then
    oc get secret llm-api-key -n "$namespace" -o json | jq -e --arg ns "$namespace" '
      (.data.LLM_API_KEY // .data.OPENAI_API_KEY) as $key |
      if $key == null then error("llm-api-key has no LLM_API_KEY or OPENAI_API_KEY") else
        {apiVersion:"v1",kind:"Secret",metadata:{name:"carewright-llm-credentials",namespace:$ns,labels:{"app.kubernetes.io/managed-by":"carewright-bootstrap"}},type:"Opaque",data:{LLM_API_KEY:$key}}
      end
    ' | oc create -f -
  else
    die 'Provide LLM_API_KEY_FILE or an existing llm-api-key Secret'
  fi
}

ensure_minio() {
  if exists deployment minio; then
    oc get deployment minio -n "$namespace" -o json | jq -e '
      any(.spec.template.spec.containers[]; .name == "minio" and any(.envFrom[]?; .secretRef.name == "minio-credentials")) and
      any(.spec.template.spec.volumes[]?; .persistentVolumeClaim.claimName == "minio-data")
    ' >/dev/null || die 'Existing MinIO Deployment does not use minio-credentials and minio-data; refusing to adopt it'
  fi
  ensure_minio_secrets
  if ! exists pvc minio-data; then
    oc create -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: minio-data
  namespace: $namespace
  labels:
    app.kubernetes.io/managed-by: carewright-bootstrap
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: $storage_class
  resources:
    requests:
      storage: $minio_storage
EOF
  fi
  if ! exists deployment minio; then
    oc create -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: minio
  namespace: $namespace
  labels:
    app.kubernetes.io/managed-by: carewright-bootstrap
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: minio
  template:
    metadata:
      labels:
        app: minio
    spec:
      containers:
        - name: minio
          image: $minio_image
          imagePullPolicy: IfNotPresent
          args: [server, /data, --console-address, ':9001']
          envFrom:
            - secretRef:
                name: minio-credentials
          ports:
            - {name: api, containerPort: 9000}
            - {name: console, containerPort: 9001}
          readinessProbe:
            httpGet: {path: /minio/health/ready, port: api}
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet: {path: /minio/health/live, port: api}
            initialDelaySeconds: 15
            periodSeconds: 10
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: '1', memory: 1Gi}
          volumeMounts:
            - {name: data, mountPath: /data}
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: minio-data
EOF
  else
    if oc get deployment minio -n "$namespace" -o json | jq -e '.metadata.labels["app.kubernetes.io/managed-by"] == "carewright-bootstrap"' >/dev/null; then
      current_image="$(oc get deployment minio -n "$namespace" -o json | jq -r '.spec.template.spec.containers[] | select(.name == "minio") | .image')"
      if [[ "$current_image" != "$minio_image" ]]; then
        oc set image deployment/minio -n "$namespace" "minio=$minio_image"
        log "Updated bootstrap-managed MinIO image to $minio_image"
      else
        log 'Reusing bootstrap-managed MinIO Deployment'
      fi
    else
      log 'Reusing existing MinIO Deployment without changing its image or storage'
    fi
  fi
  if ! exists service minio; then
    oc create -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: minio
  namespace: $namespace
  labels:
    app.kubernetes.io/managed-by: carewright-bootstrap
spec:
  selector: {app: minio}
  ports:
    - {name: api, port: 9000, targetPort: api}
    - {name: console, port: 9001, targetPort: console}
EOF
  fi
  oc get service minio -n "$namespace" -o json | jq -e '
    ([.spec.ports[].port] | (index(9000) != null and index(9001) != null)) and
    (.spec.selector.app == "minio" or .spec.selector["app.kubernetes.io/name"] == "minio")
  ' >/dev/null || die 'Existing MinIO Service must target a MinIO pod on ports 9000 and 9001'
  oc rollout status deployment/minio -n "$namespace" --timeout=300s
  oc exec -n "$namespace" deployment/minio -- /usr/bin/mc --version >/dev/null || die 'MinIO image must contain /usr/bin/mc for bucket initialization'

  # The official MinIO server image includes mc and a shell. Reuse the pod so
  # no separate init image needs registry access. Credentials stay in pod env.
  oc exec -n "$namespace" deployment/minio -- /bin/sh -ec '
    export MC_CONFIG_DIR=/tmp/.mc
    mc alias set carewright http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc mb --ignore-existing "carewright/$1" "carewright/$2"
  ' -- "$artifact_bucket" "$phi_bucket"

  if [[ "$expose_minio_console" == 1 ]]; then
    if exists route minio-console; then
      oc get route minio-console -n "$namespace" -o json | jq -e '
        .spec.to.name == "minio" and .spec.port.targetPort == "console" and .spec.tls.termination == "edge"
      ' >/dev/null || die 'Existing minio-console Route is not HTTPS edge-terminated to the MinIO console'
    else
      oc create -f - <<EOF
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: minio-console
  namespace: $namespace
  labels:
    app.kubernetes.io/managed-by: carewright-bootstrap
spec:
  to:
    kind: Service
    name: minio
  port:
    targetPort: console
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
EOF
    fi
  fi
}

ensure_openshell() {
  local uid_range group_range run_uid run_gid current_chart
  uid_range="$(oc get namespace "$namespace" -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}')"
  group_range="$(oc get namespace "$namespace" -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.supplemental-groups}')"
  run_uid="${uid_range%%/*}"
  run_gid="${group_range%%/*}"
  [[ "$run_uid" =~ ^[0-9]+$ ]] || die "Namespace $namespace has no OpenShift UID range"
  if [[ ! "$run_gid" =~ ^[0-9]+$ ]]; then run_gid="$run_uid"; fi

  if helm status "$release" -n "$namespace" >/dev/null 2>&1; then
    current_chart="$(helm list -n "$namespace" --filter "^${release}$" -o json | jq -r '.[0].chart // empty')"
    [[ "$current_chart" == *"-$openshell_version" ]] || die "Existing OpenShell Helm release uses $current_chart; upgrade it separately before rerunning"
    helm get values "$release" -n "$namespace" -o json | jq -e --arg ns "$namespace" --arg release "$release" '
      .fullnameOverride == $release and .server.sandboxNamespace == $ns and
      .server.disableTls == true and .server.auth.allowUnauthenticatedUsers == true
    ' >/dev/null || die 'Existing OpenShell release uses different gateway settings; review it manually'
    log "Reusing OpenShell Helm release $release ($openshell_version)"
  elif gateway_compatible; then
    log "Reusing compatible non-Helm OpenShell gateway $release"
  else
    if exists statefulset "$release" || exists service "$release"; then
      die "Existing $release gateway is incompatible; refusing to replace it"
    fi
    log "Installing OpenShell Helm chart $openshell_version"
    helm upgrade --install "$release" oci://ghcr.io/nvidia/openshell/helm-chart \
      --version "$openshell_version" --namespace "$namespace" --wait --timeout 10m \
      --set-string fullnameOverride="$release" \
      --set-string server.sandboxNamespace="$namespace" \
      --set-string server.workspaceStorageClass="$storage_class" \
      --set server.disableTls=true \
      --set server.auth.allowUnauthenticatedUsers=true \
      --set server.telemetryEnabled=false \
      --set securityContext.runAsUser="$run_uid" \
      --set podSecurityContext.fsGroup="$run_gid"
  fi
  oc adm policy add-scc-to-user privileged -z "${release}-sandbox" -n "$namespace" >/dev/null
  oc rollout status "statefulset/$release" -n "$namespace" --timeout=300s
  exists service "$release" || die "OpenShell gateway Service $release is missing"
}

ensure_service_proxy() {
  local resource kind name owner config_hash
  for resource in configmap/openshell-service-proxy deployment/openshell-service-proxy service/openshell-service-proxy networkpolicy/openshell-service-proxy; do
    kind="${resource%%/*}"
    name="${resource#*/}"
    if exists "$kind" "$name"; then
      owner="$(oc -n "$namespace" get "$kind" "$name" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}')"
      [[ "$owner" == carewright-bootstrap ]] || die "Existing $resource is not bootstrap-managed; refusing to overwrite it"
    fi
  done
  sed -e "s/__NAMESPACE__/$namespace/g" \
      -e "s/__RELEASE__/$release/g" \
      -e "s|__PROXY_IMAGE__|$service_proxy_image|g" \
      "$repo_dir/config/bootstrap/openshell-service-proxy.yaml.tmpl" >"$temporary_dir/openshell-service-proxy-draft.yaml"
  config_hash="$(openssl dgst -sha256 "$temporary_dir/openshell-service-proxy-draft.yaml" | awk '{print $NF}')"
  sed "s/__CONFIG_SHA__/$config_hash/g" "$temporary_dir/openshell-service-proxy-draft.yaml" >"$temporary_dir/openshell-service-proxy.yaml"
  oc apply -f "$temporary_dir/openshell-service-proxy.yaml"
  oc rollout status deployment/openshell-service-proxy -n "$namespace" --timeout=300s || die 'OpenShell service proxy is not ready'
  exists service openshell-service-proxy || die 'OpenShell service proxy Service is missing'
  log "Sandbox HTTP proxy ready at http://openshell-service-proxy.$namespace.svc.cluster.local:8080"
}

ensure_profiles() {
  local endpoint health_endpoint catalog profile id host port profile_version profile_list_ok
  endpoint="http://127.0.0.1:$openshell_port"
  health_endpoint="http://127.0.0.1:$openshell_health_port"
  oc port-forward -n "$namespace" "statefulset/$release" "$openshell_port:8080" "$openshell_health_port:8081" >"$temporary_dir/port-forward.log" 2>&1 &
  port_forward_pid=$!
  for _ in {1..30}; do
    if curl -fsS "$health_endpoint/readyz" >/dev/null 2>&1; then break; fi
    kill -0 "$port_forward_pid" 2>/dev/null || die "Gateway port-forward failed: $(tail -n 3 "$temporary_dir/port-forward.log")"
    sleep 1
  done
  kill -0 "$port_forward_pid" 2>/dev/null || die "Gateway port-forward exited: $(tail -n 3 "$temporary_dir/port-forward.log")"
  curl -fsS "$health_endpoint/readyz" >/dev/null || die 'Gateway readiness endpoint is unreachable through port-forward'

  sed "s/__NAMESPACE__/$namespace/g" "$repo_dir/config/bootstrap/profiles/carewright-minio.yaml.tmpl" >"$temporary_dir/carewright-minio.yaml"
  sed -e "s/__LLM_HOST__/$llm_host/g" -e "s/__LLM_PORT__/$llm_port/g" "$repo_dir/config/bootstrap/profiles/carewright-openai.yaml.tmpl" >"$temporary_dir/carewright-openai.yaml"
  catalog="$temporary_dir/profiles.json"
  profile_list_ok=0
  for _ in {1..10}; do
    if "$openshell_cli" provider profile list --gateway-endpoint "$endpoint" --workspace "$workspace" -o json >"$catalog" 2>"$temporary_dir/profile-list.err"; then
      profile_list_ok=1
      break
    fi
    sleep 1
  done
  ((profile_list_ok)) || die "OpenShell profile API unavailable: $(tail -n 3 "$temporary_dir/profile-list.err")"
  for id in carewright-minio carewright-openai; do
    profile="$temporary_dir/$id.yaml"
    if [[ "$id" == carewright-minio ]]; then host="minio.$namespace.svc.cluster.local"; port=9000; else host="$llm_host"; port="$llm_port"; fi
    if jq -e --arg id "$id" 'any(.[]; .id == $id)' "$catalog" >/dev/null; then
      jq -e --arg id "$id" --arg host "$host" --argjson port "$port" '
        any(.[]; .id == $id and any(.endpoints[]; .host == $host and .port == $port))
      ' "$catalog" >/dev/null || die "Existing profile $id has a different endpoint; review it manually"
      if [[ "$id" == carewright-minio ]]; then
        jq -e --arg id "$id" 'any(.[]; .id == $id and (
          (any(.credentials[]; (.env_vars // []) | index("ARTIFACT_STORE_ACCESS_KEY") != null) and
           any(.credentials[]; (.env_vars // []) | index("ARTIFACT_STORE_SECRET_KEY") != null)) or
          (any(.credentials[]; (.env_vars // []) | index("AWS_ACCESS_KEY_ID") != null) and
           any(.credentials[]; (.env_vars // []) | index("AWS_SECRET_ACCESS_KEY") != null))))' "$catalog" >/dev/null || die 'Existing carewright-minio profile has incompatible credential keys'
        if jq -e --arg id "$id" --arg host "$host" --argjson port "$port" 'any(.[]; .id == $id and
          any(.credentials[]; (.env_vars // []) | index("ARTIFACT_STORE_ACCESS_KEY") != null) and
          any(.credentials[]; (.env_vars // []) | index("ARTIFACT_STORE_SECRET_KEY") != null) and
          any(.credentials[]; (.env_vars // []) | index("AWS_ACCESS_KEY_ID") != null) and
          any(.credentials[]; (.env_vars // []) | index("AWS_SECRET_ACCESS_KEY") != null) and
          any(.endpoints[]; .host == $host and .port == $port and .credential_signing == "sigv4" and .signing_service == "s3" and .signing_region == "us-east-1"))' "$catalog" >/dev/null; then
          log "Reusing OpenShell profile $id"
        else
          "$openshell_cli" provider profile export "$id" --gateway-endpoint "$endpoint" --workspace "$workspace" -o json >"$temporary_dir/$id-current.json"
          profile_version="$(jq -er '.resource_version | select(type == "number" and . > 0)' "$temporary_dir/$id-current.json")" || die "Cannot determine resource version for OpenShell profile $id"
          cp "$profile" "$temporary_dir/$id-update.yaml"
          printf 'resource_version: %s\n' "$profile_version" >>"$temporary_dir/$id-update.yaml"
          "$openshell_cli" provider profile update "$id" --file "$temporary_dir/$id-update.yaml" --gateway-endpoint "$endpoint" --workspace "$workspace"
          log "Updated OpenShell profile $id for MinIO SigV4 signing"
        fi
      else
        jq -e --arg id "$id" 'any(.[]; .id == $id and
          any(.credentials[]; (.env_vars // []) | index("LLM_API_KEY") != null))' "$catalog" >/dev/null || die 'Existing carewright-openai profile lacks LLM_API_KEY'
        log "Reusing OpenShell profile $id"
      fi
    else
      "$openshell_cli" provider profile lint --file "$profile" --gateway-endpoint "$endpoint" --workspace "$workspace"
      "$openshell_cli" provider profile import --file "$profile" --gateway-endpoint "$endpoint" --workspace "$workspace"
    fi
  done
}

ensure_sonataflow_platform() {
  if exists sonataflowplatform sonataflow-platform; then
    log 'Reusing SonataFlowPlatform sonataflow-platform'
  else
    oc create -f - <<EOF
apiVersion: sonataflow.org/v1alpha08
kind: SonataFlowPlatform
metadata:
  name: sonataflow-platform
  namespace: $namespace
  labels:
    app.kubernetes.io/managed-by: carewright-bootstrap
spec:
  services:
    dataIndex:
      enabled: false
    jobService:
      enabled: false
EOF
  fi
  oc wait --for=jsonpath='{.status.conditions[?(@.type=="Succeed")].status}'=True sonataflowplatform/sonataflow-platform -n "$namespace" --timeout=300s || die 'SonataFlowPlatform did not become ready'
}

ensure_image_pull() {
  if [[ -z "$image_source_namespace" || "$image_source_namespace" == "$namespace" ]]; then return; fi
  if oc get namespace "$image_source_namespace" >/dev/null 2>&1; then
    oc policy add-role-to-user system:image-puller "system:serviceaccount:${namespace}:${release}-sandbox" -n "$image_source_namespace" >/dev/null
    oc policy add-role-to-user system:image-puller "system:serviceaccount:${namespace}:default" -n "$image_source_namespace" >/dev/null
    log "Granted sandbox and default workload image pull from $image_source_namespace"
  else
    log "Image namespace $image_source_namespace is absent; skipping internal-registry pull grant"
  fi
}

log "Target namespace: $namespace"
ensure_namespace
preflight_openshell_ownership
ensure_storage_class
ensure_network_policy_cni
ensure_agent_sandbox
ensure_sonataflow_operator
ensure_minio
ensure_openshell
ensure_service_proxy
ensure_profiles
ensure_sonataflow_platform
ensure_image_pull

log 'Prerequisites are ready. Install carewright-operator next.'
printf '\nPipeline CR inputs:\n'
printf '  spec.sandbox.gateway.endpoint: http://%s.%s.svc.cluster.local:8080\n' "$release" "$namespace"
printf '  spec.sandbox.gateway.insecure: true\n'
printf '  sandbox HTTP gateway URL: http://openshell-service-proxy.%s.svc.cluster.local:8080\n' "$namespace"
printf '  spec.sandbox.workspace: %s\n' "$workspace"
printf '  spec.artifactStore.url: http://minio.%s.svc.cluster.local:9000\n' "$namespace"
printf '  spec.artifactStore.artifactBucket: %s\n' "$artifact_bucket"
printf '  spec.artifactStore.phiBucket: %s\n' "$phi_bucket"
printf '  spec.artifactStore.credentials: {type: carewright-minio, secretRef: {name: carewright-artifact-credentials}}\n'
printf '  spec.llm.url: %s\n' "$llm_origin"
printf '  spec.llm.model: %s\n' "$llm_model"
printf '  spec.llm.credentials: {type: carewright-openai, secretRef: {name: carewright-llm-credentials}}\n'
if [[ "$expose_minio_console" == 1 ]]; then
  console_host="$(oc get route minio-console -n "$namespace" -o jsonpath='{.spec.host}')"
  printf '  MinIO console: https://%s\n' "$console_host"
fi
