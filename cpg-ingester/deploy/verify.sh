#!/usr/bin/env bash
# cpg-ingester/deploy/verify.sh — Verify cpg-ingester deployment
#
# Checks pods, images, supervision, routed health, and BFF dependencies.
# Retries sandbox checks for up to 90s to handle startup races.
#
# Usage:
#   ./cpg-ingester/deploy/verify.sh [--config <path>] [--tag <sha>] [--run-id <id>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/deploy/lib.sh"

CONFIG_PATH="$REPO_ROOT/deploy/config/cluster.env"
TAG_OVERRIDE=""
RUN_ID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2;;
        --tag) TAG_OVERRIDE="$2"; shift 2;;
        --run-id) RUN_ID="$2"; shift 2;;
        *) shift;;
    esac
done
load_config "$CONFIG_PATH"
IMAGE_TAG=$(resolve_deploy_tag "cpg-ingester" "$TAG_OVERRIDE")

log_step "Verifying cpg-ingester deployment (tag: ${IMAGE_TAG})"
ERRORS=0

# Sandbox checks with retry
SANDBOXES=(sb-ingestion sb-llm-analysis sb-assembly sb-delivery)
verify_sandboxes "$IMAGE_TAG" "${SANDBOXES[@]}" || ERRORS=$?

# BFF health — must have minio and sonataflow connected (Fix 4)
echo ""
log "BFF dependency check:"
bff_health=$(oc exec deployment/cpg-ingester-bff -n "$NAMESPACE" -- \
    python3 -c 'import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://localhost:8080/health", timeout=5))))' \
    2>/dev/null || echo "{}")
bff_minio=$(echo "$bff_health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('minio',False))" 2>/dev/null || echo "False")
bff_sonataflow=$(echo "$bff_health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sonataflow',False))" 2>/dev/null || echo "False")

if [ "$bff_minio" = "True" ] && [ "$bff_sonataflow" = "True" ]; then
    echo "  ✓ BFF: minio=true, sonataflow=true"
else
    echo "  ✗ BFF dependencies missing: $bff_health"
    ERRORS=$((ERRORS + 1))
fi

if [[ -n "$RUN_ID" ]]; then
    echo ""
    log "Published BPMN artifact check for run $RUN_ID:"
    if run_detail=$(oc exec deployment/cpg-ingester-bff -n "$NAMESPACE" -- \
        python3 -c 'import json,sys,urllib.request; url="http://localhost:8080/api/v1/runs/"+sys.argv[1]; print(json.dumps(json.load(urllib.request.urlopen(url, timeout=15))))' \
        "$RUN_ID" 2>/dev/null); then
        bpmn_count=$(printf '%s' "$run_detail" | python3 -c 'import json,sys; detail=json.load(sys.stdin); artifacts=(detail.get("deliveryStatus") or {}).get("artifacts") or []; print(sum(1 for item in artifacts if item.get("type")=="bpmn" and "/published/" in str(item.get("ref", "")) and "/bpmn/" in str(item.get("ref", ""))))')
        if (( bpmn_count > 0 )); then
            echo "  ✓ $bpmn_count published BPMN artifact reference(s) under published/*/bpmn/"
        else
            echo "  ✗ no published/*/bpmn/ artifact references in run delivery status"
            ERRORS=$((ERRORS + 1))
        fi
    else
        echo "  ✗ could not read run $RUN_ID from the BFF"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "  Published BPMN artifact check skipped (pass --run-id after a pipeline run)."
fi

echo ""
if [ $ERRORS -eq 0 ]; then
    log "cpg-ingester verification: ALL CHECKS PASSED"
else
    log "cpg-ingester verification: $ERRORS CHECK(S) FAILED"
    exit 1
fi
