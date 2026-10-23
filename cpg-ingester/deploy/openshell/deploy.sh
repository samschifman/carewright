#!/usr/bin/env bash
# cpg-ingester/deploy/openshell/deploy.sh — Create cpg-ingester OpenShell sandboxes
#
# Creates 4 sandboxes: ingestion, llm-analysis, assembly, delivery.
# Each runs supervised under OpenShell with its own security policy.
#
# Usage:
#   ./cpg-ingester/deploy/openshell/deploy.sh [--config <cluster.env>] [--tag <sha>]
#   ./cpg-ingester/deploy/openshell/deploy.sh teardown

set -euo pipefail
[ -n "${ZSH_VERSION:-}" ] && setopt SH_WORD_SPLIT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPONENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$COMPONENT_DIR/../.." && pwd)"

# shellcheck disable=SC1091
source "$REPO_ROOT/deploy/lib.sh"

ACTION="deploy"
CONFIG_PATH="$REPO_ROOT/deploy/config/cluster.env"
TAG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        deploy|teardown) ACTION="$1"; shift;;
        --config) CONFIG_PATH="$2"; shift 2;;
        --tag) TAG_OVERRIDE="$2"; shift 2;;
        -h|--help)
            echo "Usage: cpg-ingester/deploy/openshell/deploy.sh [deploy|teardown] [--config <path>] [--tag <sha>]"
            exit 0;;
        *) shift;;
    esac
done

load_config "$CONFIG_PATH"
[ -n "$TAG_OVERRIDE" ] && IMAGE_TAG="$TAG_OVERRIDE"

preflight
preflight_openshell

IMAGE_REGISTRY="quay.io/carewright"

RENDERED_POLICY_DIR="$REPO_ROOT/deploy/.rendered/cpg-ingester-policies"
render_templates_dir "$SCRIPT_DIR/policies" "$RENDERED_POLICY_DIR"
POLICY_DIR="$RENDERED_POLICY_DIR"

if [ -n "${MAAS_ROUTE_SEGMENT:-}" ]; then
    LLM_BASE_URL="${MAAS_GATEWAY_URL}/${MAAS_ROUTE_SEGMENT}"
else
    LLM_BASE_URL="${MAAS_GATEWAY_URL}"
fi
LLM_MODEL="${CPG_INGESTER_LLM_MODEL:-$LLM_MODEL_DEFAULT}"

# Docling/figure knobs (defaults match the code; override via cluster.env).
INGESTION_OCR_ENABLED="${CPG_INGESTER_OCR_ENABLED:-true}"
FIGURE_INTERPRETATION_ENABLED="${CPG_INGESTER_FIGURE_INTERPRETATION_ENABLED:-true}"
FIGURE_INTERPRETATION_MAX_FIGURES="${CPG_INGESTER_FIGURE_MAX:-100}"

{ set +x; } 2>/dev/null
LLM_API_KEY=$(read_secret llm-credentials LLM_API_KEY)
MINIO_ACCESS=$(read_secret minio-credentials ARTIFACT_STORE_ACCESS_KEY)
MINIO_SECRET=$(read_secret minio-credentials ARTIFACT_STORE_SECRET_KEY)

SANDBOXES=(sb-ingestion sb-llm-analysis sb-assembly sb-delivery)

common_env=(
    "MLFLOW_TRACKING_URI=${MLFLOW_TRACKING_URI}"
    "ARTIFACT_STORE_URL=http://minio:9000"
    "ARTIFACT_STORE_ACCESS_KEY=${MINIO_ACCESS}"
    "ARTIFACT_STORE_SECRET_KEY=${MINIO_SECRET}"
)

teardown_sandboxes() {
    log_step "Tearing down cpg-ingester sandboxes"
    for sb in "${SANDBOXES[@]}"; do
        log "Deleting $sb..."
        openshell sandbox delete "$sb" 2>/dev/null || true
    done
}

deploy_sandboxes() {
    log_step "Creating cpg-ingester OpenShell sandboxes (IMAGE_TAG=$IMAGE_TAG)"

    create_ing_sandbox() {
        local name="$1" image="$2" policy="$3" k8s_name="$4" command="$5"
        shift 5
        local env_args=("$@")

        log "Creating $name from ${image}:${IMAGE_TAG}..."
        local args=(
            --name "$name"
            --from "${IMAGE_REGISTRY}/${image}:${IMAGE_TAG}"
            --policy "${POLICY_DIR}/${policy}"
        )
        for e in "${env_args[@]}"; do
            args+=(--env "$e")
        done

        openshell sandbox create "${args[@]}" -- sh -c "$command" &
        local pid=$!
        sleep 2

        wait_for_pod_ready "$name" 90 || true
        label_pod "$name" "$k8s_name" "cpg-ingester"
        expose_service "$name"
        log "Done: $name"
    }

    # Ingestion (Docling — needs extra env for offline model loading)
    create_ing_sandbox "sb-ingestion" "cpg-ingester-ingestion" "cpg-ingester-ingestion.yaml" \
        "cpg-ingester-ingestion" \
        "uvicorn cpg_ingester.services.ingestion:app --host 0.0.0.0 --port 8080" \
        "${common_env[@]}" \
        "LOG_LEVEL=DEBUG" \
        "DOCLING_LOG_LEVEL=DEBUG" \
        "PYTHONUNBUFFERED=1" \
        "HF_HUB_OFFLINE=1" \
        "DOCLING_ARTIFACTS_PATH=/app/.cache/docling/models" \
        "INGESTION_OCR_ENABLED=${INGESTION_OCR_ENABLED}"

    # LLM Analysis
    create_ing_sandbox "sb-llm-analysis" "cpg-ingester-llm" "cpg-ingester-llm.yaml" \
        "cpg-ingester-llm-analysis" \
        "uvicorn cpg_ingester.services.llm_analysis:app --host 0.0.0.0 --port 8080" \
        "${common_env[@]}" \
        "PYTHONPATH=/app/src" \
        "LITELLM_URL=${LLM_BASE_URL}" \
        "LLM_MODEL=${LLM_MODEL}" \
        "LLM_API_KEY=${LLM_API_KEY}" \
        "LLM_REQUEST_TIMEOUT=${LLM_REQUEST_TIMEOUT:-600}" \
        "FIGURE_INTERPRETATION_ENABLED=${FIGURE_INTERPRETATION_ENABLED}" \
        "FIGURE_INTERPRETATION_MAX_FIGURES=${FIGURE_INTERPRETATION_MAX_FIGURES}"

    # Assembly
    create_ing_sandbox "sb-assembly" "cpg-ingester-assembly" "cpg-ingester-assembly.yaml" \
        "cpg-ingester-assembly" \
        "uvicorn cpg_ingester.services.assembly_svc:app --host 0.0.0.0 --port 8080" \
        "${common_env[@]}" \
        "PYTHONPATH=/app/src"

    # Delivery (MinIO-only after PR #115 decoupling — no acp-writer dependency)
    create_ing_sandbox "sb-delivery" "cpg-ingester-delivery" "cpg-ingester-delivery.yaml" \
        "cpg-ingester-delivery" \
        "uvicorn cpg_ingester.services.delivery_svc:app --host 0.0.0.0 --port 8080" \
        "${common_env[@]}" \
        "PYTHONPATH=/app/src"

    sleep 5
}

case "$ACTION" in
    deploy)
        teardown_sandboxes
        deploy_sandboxes
        log_step "cpg-ingester OpenShell deployment complete"
        ;;
    teardown)
        teardown_sandboxes
        log_step "cpg-ingester sandboxes removed"
        ;;
    *)
        echo "Unknown action: $ACTION. Use deploy or teardown."
        exit 1
        ;;
esac
