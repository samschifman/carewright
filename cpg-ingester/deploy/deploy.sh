#!/usr/bin/env bash
# cpg-ingester/deploy/deploy.sh — Deploy cpg-ingester to OpenShift
#
# Usage:
#   ./cpg-ingester/deploy/deploy.sh [--skip-build] [--skip-openshell] [--tag <sha>] [--config <path>]

set -euo pipefail
[ -n "${ZSH_VERSION:-}" ] && setopt SH_WORD_SPLIT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO_ROOT/deploy/lib.sh"

SKIP_BUILD=false
SKIP_OPENSHELL=false
CONFIG_PATH="$REPO_ROOT/deploy/config/cluster.env"
TAG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-build) SKIP_BUILD=true; shift;;
        --skip-openshell) SKIP_OPENSHELL=true; shift;;
        --config) CONFIG_PATH="$2"; shift 2;;
        --tag) TAG_OVERRIDE="$2"; shift 2;;
        -h|--help)
            echo "Usage: cpg-ingester/deploy/deploy.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --skip-build      Skip image builds"
            echo "  --skip-openshell  Deploy Helm-managed pods instead of OpenShell sandboxes"
            echo "  --tag <sha>       Override image tag"
            echo "  --config <path>   Path to cluster.env"
            exit 0;;
        *) shift;;
    esac
done

load_config "$CONFIG_PATH"
[ -n "$TAG_OVERRIDE" ] && IMAGE_TAG="$TAG_OVERRIDE"

preflight

if [ -n "${MAAS_ROUTE_SEGMENT:-}" ]; then
    LLM_BASE_URL="${MAAS_GATEWAY_URL}/${MAAS_ROUTE_SEGMENT}"
else
    LLM_BASE_URL="${MAAS_GATEWAY_URL}"
fi
LLM_MODEL="${CPG_INGESTER_LLM_MODEL:-$LLM_MODEL_DEFAULT}"

OPENSHELL_MODE="true"
if [ "$SKIP_OPENSHELL" = true ]; then
    OPENSHELL_MODE="false"
fi

log_step "Deploying cpg-ingester (namespace=$NAMESPACE, tag=$IMAGE_TAG, openshellMode=$OPENSHELL_MODE)"

# --- Build ---

if [ "$SKIP_BUILD" = false ]; then
    log_step "Building cpg-ingester images"
    "$SCRIPT_DIR/setup-images.sh" --config "$CONFIG_PATH" --tag "$IMAGE_TAG"
    start_builds_parallel \
        cpg-ingester-ingestion \
        cpg-ingester-llm \
        cpg-ingester-assembly \
        cpg-ingester-delivery \
        cpg-ingester-bff \
        cpg-ingester-ui
    prune_builds "cpg-ingester"
else
    log "Skipping builds (--skip-build)"
fi

# --- Helm ---

log_step "Deploying Helm chart"
log "Installing cpg-ingester chart (timeout 120s)..."
helm_start=$SECONDS
helm upgrade --install cpg-ingester "$SCRIPT_DIR/chart" \
    -n "$NAMESPACE" \
    --set openshellMode="$OPENSHELL_MODE" \
    --set mlflow.trackingUri="$MLFLOW_TRACKING_URI" \
    --set pods.ingestion.tag="$IMAGE_TAG" \
    --set pods.llm-analysis.tag="$IMAGE_TAG" \
    --set pods.llm-analysis.env.litellmUrl="$LLM_BASE_URL" \
    --set pods.llm-analysis.env.llmModel="$LLM_MODEL" \
    --set pods.llm-analysis.env.llmRequestTimeout="${LLM_REQUEST_TIMEOUT:-600}" \
    --set pods.assembly.tag="$IMAGE_TAG" \
    --set pods.delivery.tag="$IMAGE_TAG" \
    --set pods.bff.tag="$IMAGE_TAG" \
    --set pods.ui.tag="$IMAGE_TAG" \
    --wait --timeout 120s || { log "ERROR: cpg-ingester helm install failed"; exit 1; }
log "  cpg-ingester installed ($(( SECONDS - helm_start ))s)"

# --- SonataFlow ---

log_step "Applying SonataFlow workflow"
# Props MUST be applied before (or with) the CR: they map CloudEvent channels
# to the /wait-* HTTP endpoints the async callbacks depend on.
oc apply -f "$SCRIPT_DIR/orchestrator/cpgingester-props.yaml" -n "$NAMESPACE" 2>/dev/null \
    || log "WARNING: cpgingester-props apply failed"
oc apply -f "$SCRIPT_DIR/orchestrator/cpg-ingester-workflow.yaml" -n "$NAMESPACE" 2>/dev/null \
    || log "WARNING: SonataFlow workflow apply failed"

# --- OpenShell ---

if [ "$SKIP_OPENSHELL" = false ]; then
    "$SCRIPT_DIR/openshell/deploy.sh" --config "$CONFIG_PATH" --tag "$IMAGE_TAG"
else
    log "Skipping OpenShell (--skip-openshell)"
fi

save_deploy_state "cpg-ingester" "$IMAGE_TAG"

# --- Verify ---

log_step "Verifying cpg-ingester deployment"
"$SCRIPT_DIR/verify.sh" --config "$CONFIG_PATH"

# --- Prune ---

for is in cpg-ingester-ingestion cpg-ingester-llm cpg-ingester-assembly cpg-ingester-delivery cpg-ingester-bff cpg-ingester-ui; do
    prune_image_tags "$is" 5
done

log_step "cpg-ingester deployment complete"
