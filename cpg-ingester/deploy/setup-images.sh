#!/usr/bin/env bash
# cpg-ingester/deploy/setup-images.sh — Create ImageStreams + BuildConfigs for cpg-ingester
#
# One-time setup: creates the OpenShift build infrastructure.
# Idempotent — safe to run repeatedly.
#
# Usage:
#   ./cpg-ingester/deploy/setup-images.sh [--config <cluster.env>] [--tag <sha>]

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
            echo "Usage: cpg-ingester/deploy/setup-images.sh [--config <path>] [--tag <sha>]"
            exit 0;;
        *) shift;;
    esac
done

load_config "$CONFIG_PATH"
[ -n "$TAG_OVERRIDE" ] && IMAGE_TAG="$TAG_OVERRIDE"
preflight

log_step "Setting up cpg-ingester images (namespace=$NAMESPACE, tag=$IMAGE_TAG)"

IMAGES=(
    "cpg-ingester-ingestion"
    "cpg-ingester-llm"
    "cpg-ingester-assembly"
    "cpg-ingester-delivery"
    "cpg-ingester-bff"
    "cpg-ingester-ui"
)

log "Creating ImageStreams..."
for is_name in "${IMAGES[@]}"; do
    oc create imagestream "$is_name" -n "$NAMESPACE" 2>/dev/null \
        && log "  Created $is_name" \
        || log "  $is_name already exists"
done

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
      name: quay.io/cpgtoacp/$name:$IMAGE_TAG
    pushSecret:
      name: cpgtoacp-cpgtoacpbot-pull-secret
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
# Ingestion gets extra resources for Docling model download
create_bc "cpg-ingester-ingestion" "cpg-ingester/deploy/pods/Containerfile.ingestion" "2" "8Gi"
create_bc "cpg-ingester-llm"       "cpg-ingester/deploy/pods/Containerfile.llm-analysis"
create_bc "cpg-ingester-assembly"  "cpg-ingester/deploy/pods/Containerfile.assembly"
create_bc "cpg-ingester-delivery"  "cpg-ingester/deploy/pods/Containerfile.delivery"
create_bc "cpg-ingester-bff"       "cpg-ingester/deploy/pods/Containerfile.bff"
create_bc "cpg-ingester-ui"        "cpg-ingester/ui/Containerfile" "1" "2Gi"

log_step "cpg-ingester image setup complete"
