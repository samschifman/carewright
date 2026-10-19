#!/usr/bin/env bash
# acp-writer/deploy/setup-images.sh — Create ImageStreams + BuildConfigs for acp-writer
#
# One-time setup: creates the OpenShift build infrastructure.
# Idempotent — safe to run repeatedly.
#
# Usage:
#   ./acp-writer/deploy/setup-images.sh [--config <cluster.env>] [--tag <sha>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO_ROOT/deploy/lib.sh"

CONFIG_PATH="$REPO_ROOT/deploy/config/cluster.env"
TAG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2;;
        --tag) TAG_OVERRIDE="$2"; shift 2;;
        -h|--help)
            echo "Usage: acp-writer/deploy/setup-images.sh [--config <path>] [--tag <sha>]"
            echo ""
            echo "Creates ImageStreams and BuildConfigs for acp-writer pod images."
            echo "Run once before first deploy; idempotent thereafter."
            exit 0;;
        *) shift;;
    esac
done

load_config "$CONFIG_PATH"
[ -n "$TAG_OVERRIDE" ] && IMAGE_TAG="$TAG_OVERRIDE"
preflight

log_step "Setting up acp-writer images (namespace=$NAMESPACE, tag=$IMAGE_TAG)"

# ImageStream names must match BuildConfig names (deployment-log lesson)
# Format: acp-writer-<pod-name>
IMAGES=(
    "acp-writer-patient-data"
    "acp-writer-llm"
    "acp-writer-decision"
    "acp-writer-fhir-gen"
    "acp-writer-fhir-srv"
    "acp-writer-bff"
    "acp-writer-ui"
    "acp-writer-mcp"
)

# Create ImageStreams
log "Creating ImageStreams..."
for is_name in "${IMAGES[@]}"; do
    oc create imagestream "$is_name" -n "$NAMESPACE" 2>/dev/null \
        && log "  Created $is_name" \
        || log "  $is_name already exists"
done

# Also need the decision-service (Java/Kogito)
oc create imagestream "decision-service" -n "$NAMESPACE" 2>/dev/null \
    && log "  Created decision-service" \
    || log "  decision-service already exists"

# BuildConfig helper
create_bc() {
    local name="$1"
    local containerfile="$2"
    local cpu_limit="${3:-1}"
    local mem_limit="${4:-2Gi}"
    local context_dir="${5:-}"

    local context_yaml=""
    if [ -n "$context_dir" ]; then
        context_yaml="    contextDir: $context_dir"
    fi

    oc apply -f - <<EOF
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: $name
  namespace: $NAMESPACE
spec:
  source:
    type: Git
    git:
      uri: $GIT_REPO
      ref: $GIT_BRANCH
${context_yaml}
  strategy:
    type: Docker
    dockerStrategy:
      dockerfilePath: $containerfile
  output:
    to:
      kind: DockerImage
      name: quay.io/carewright/$name:$IMAGE_TAG
    pushSecret:
      name: carewright-quay-push-secret
  resources:
    limits:
      cpu: "$cpu_limit"
      memory: $mem_limit
  failedBuildsHistoryLimit: 3
  successfulBuildsHistoryLimit: 3
EOF
    log "  $name → $containerfile${context_dir:+ (context: $context_dir)} → $name:$IMAGE_TAG (${cpu_limit} CPU / ${mem_limit})"
}

log "Creating BuildConfigs..."
create_bc "acp-writer-patient-data" "acp-writer/deploy/pods/Containerfile.patient-data"
create_bc "acp-writer-llm"          "acp-writer/deploy/pods/Containerfile.llm-reasoning"
create_bc "acp-writer-decision"     "acp-writer/deploy/pods/Containerfile.decision-engine"
create_bc "acp-writer-fhir-gen"     "acp-writer/deploy/pods/Containerfile.fhir-generation"
create_bc "acp-writer-fhir-srv"     "acp-writer/deploy/pods/Containerfile.fhir-server"
create_bc "acp-writer-bff"          "acp-writer/deploy/pods/Containerfile.bff"
create_bc "acp-writer-ui"           "acp-writer/ui/Containerfile"
create_bc "acp-writer-mcp"          "acp-writer/deploy/pods/Containerfile.mcp"
create_bc "decision-service"        "deploy/Containerfile" "2" "4Gi" "acp-writer/decision-service"

log_step "acp-writer image setup complete"
log "To build: oc start-build <name> -n $NAMESPACE"
log "Or use: acp-writer/deploy/deploy.sh (builds automatically)"
