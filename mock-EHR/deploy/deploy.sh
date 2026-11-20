#!/usr/bin/env bash
# mock-EHR/deploy/deploy.sh — Deploy mock-EHR (Medplum) to OpenShift
#
# No OpenShell sandboxes — Medplum runs as standard Helm-deployed pods.
#
# Usage:
#   ./mock-EHR/deploy/deploy.sh [--skip-build] [--tag <sha>] [--config <path>]
#
# Environment (or deploy/config/cluster.env):
#   SMART_WRITER_ORIGIN     The writer UI origin (scheme://host[:port]) as the
#                           browser sees it. One value drives both the SMART
#                           client the loader registers in the EHR and the
#                           authorization page's redirect allowlist. This is
#                           the EHR side only: a launch also needs a writer BFF
#                           with the SMART client credentials and the same
#                           public origin, which today is the operator-managed
#                           writer (spec.smart.publicOrigin). Set this to that
#                           writer's UI route. Unset, it falls back to
#                           https://acp-ui-$NAMESPACE.$CLUSTER_DOMAIN, the
#                           script-deployed UI route, where the writer cannot
#                           complete a launch.
#   SMART_REDIRECT_ORIGINS  Optional extra allowed origins (comma-separated).
#                           Defaults to SMART_WRITER_ORIGIN and must include it.
#                           Falls back to VITE_SMART_REDIRECT_ORIGINS.
# Both apply at deploy time through /config.json and the loader job, so they
# work with published images (--skip-build --tag).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$REPO_ROOT/deploy/lib.sh"
source "$SCRIPT_DIR/smart-lib.sh"

SKIP_BUILD=false
CONFIG_PATH="$REPO_ROOT/deploy/config/cluster.env"
TAG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-build) SKIP_BUILD=true; shift;;
        --config) CONFIG_PATH="$2"; shift 2;;
        --tag) TAG_OVERRIDE="$2"; shift 2;;
        -h|--help)
            echo "Usage: mock-EHR/deploy/deploy.sh [--skip-build] [--tag <sha>] [--config <path>]"
            exit 0;;
        *) shift;;
    esac
done

load_config "$CONFIG_PATH"
[ -n "$TAG_OVERRIDE" ] && IMAGE_TAG="$TAG_OVERRIDE"
preflight

SMART_WRITER_ORIGIN_SET="${SMART_WRITER_ORIGIN:-}"
if ! resolve_smart_origins "$NAMESPACE" "${CLUSTER_DOMAIN:-}"; then
    log "ERROR: invalid SMART launch settings — nothing was deployed"
    exit 1
fi

log_step "Deploying mock-EHR (namespace=$NAMESPACE, tag=$IMAGE_TAG)"
if [ -z "$SMART_WRITER_ORIGIN" ]; then
    log "WARNING: no writer origin is known (set SMART_WRITER_ORIGIN or CLUSTER_DOMAIN) — the EHR will refuse every SMART launch."
else
    log "  SMART writer origin (registered callback and redirect allowlist): $SMART_WRITER_ORIGIN"
    [ "$SMART_REDIRECT_ORIGINS" != "$SMART_WRITER_ORIGIN" ] && log "  SMART redirect allowlist: $SMART_REDIRECT_ORIGINS"
    log "  This configures the EHR only. Launch also needs a writer BFF with the SMART client credentials"
    log "  and this same public origin (operator-managed writer: spec.smart.publicOrigin)."
    if [ -z "${SMART_WRITER_ORIGIN_SET:-}" ]; then
        log "  NOTE: SMART_WRITER_ORIGIN was not set, so this is the script-deployed UI route, whose writer"
        log "        does not receive SMART settings. Set SMART_WRITER_ORIGIN to the operator-managed writer's UI route."
    fi
fi

if [ "$SKIP_BUILD" = false ]; then
    log_step "Building mock-EHR images"
    "$SCRIPT_DIR/setup-openshift.sh" \
        --namespace "$NAMESPACE" \
        --branch "$GIT_BRANCH" \
        --tag "$IMAGE_TAG"
else
    log "Skipping builds (--skip-build)"
fi

log_step "Deploying Helm chart"

# Check if medplum-user-credentials secret exists → enable loader
LOADER_ENABLED="false"
if oc get secret medplum-user-credentials -n "$NAMESPACE" &>/dev/null; then
    LOADER_ENABLED="true"
    log "  medplum-user-credentials secret found — loader job enabled"
else
    log "  medplum-user-credentials secret not found — loader job disabled"
fi

# Remember the SMART credentials Secret's version so a refresh by the loader
# can be told apart from a Secret left over from an earlier run.
SMART_REFRESH_OK=true
SMART_SECRET_VERSION_BEFORE="unknown"
if version="$(smart_secret_version "$NAMESPACE")"; then
    SMART_SECRET_VERSION_BEFORE="${version:-absent}"
fi

log "Installing mock-EHR chart (timeout 300s)..."
helm_start=$SECONDS
helm upgrade --install cpg-mock-ehr "$SCRIPT_DIR/chart" \
    -n "$NAMESPACE" \
    --set clusterDomain="$CLUSTER_DOMAIN" \
    --set mockEhrApp.image.tag="$IMAGE_TAG" \
    --set-string acpWriterOrigin="$SMART_WRITER_ORIGIN" \
    --set-string mockEhrApp.smartRedirectOrigins="$(helm_escape_commas "$SMART_REDIRECT_ORIGINS")" \
    --set loader.enabled="$LOADER_ENABLED" \
    --set loader.image.tag="$IMAGE_TAG" \
    --wait --timeout 300s || { log "ERROR: mock-EHR helm install failed"; exit 1; }
log "  mock-EHR installed ($(( SECONDS - helm_start ))s)"

# The loader (a post-install/post-upgrade hook) registers a new SMART client on
# every run and rewrites the smart-client-credentials Secret. Restart the
# Deployments that consume it, but only once the Secret is seen to have changed
# since before this deploy (see restart_smart_secret_consumers).
if [ "$LOADER_ENABLED" = "true" ]; then
    log "Waiting for loader job to complete..."
    oc wait --for=condition=Complete job -l app.kubernetes.io/instance=cpg-mock-ehr -n "$NAMESPACE" --timeout=120s >/dev/null 2>&1 \
        || log "WARNING: the loader job did not report completion within 120s"
    if ! restart_smart_secret_consumers "$NAMESPACE" "$SMART_SECRET_VERSION_BEFORE"; then
        SMART_REFRESH_OK=false
    fi
fi

log_step "Deploying MCP server"
render_template "$SCRIPT_DIR/mcp/mock-ehr-mcp.yaml.tmpl" "$REPO_ROOT/deploy/.rendered/mock-ehr-mcp.yaml"
oc apply -f "$REPO_ROOT/deploy/.rendered/mock-ehr-mcp.yaml" -n "$NAMESPACE" 2>/dev/null \
    || log "WARNING: MCP server deploy failed"
oc apply -f "$SCRIPT_DIR/mcp/registration.yaml" -n "$NAMESPACE" 2>/dev/null \
    || log "WARNING: MCP registration apply failed"

save_deploy_state "mock-ehr" "$IMAGE_TAG"

log_step "Verifying mock-EHR"
"$SCRIPT_DIR/verify.sh" --config "$CONFIG_PATH"

log_step "mock-EHR deployment complete"
if [ "$SMART_REFRESH_OK" != true ]; then
    log "WARNING: the SMART credentials were not verified as refreshed and loaded by the writer."
    log "         See the warnings above before using SMART launch."
fi
