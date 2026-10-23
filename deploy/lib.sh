#!/usr/bin/env bash
# deploy/lib.sh — Minimal shared helpers for the deploy framework.
#
# Changes here are cross-team changes. Keep it small and stable.
# Never put component-specific knowledge in this file.
#
# Source this file: source "$(dirname "$0")/../deploy/lib.sh"
#                   (or adjust the path for your script's location)

# Fail on errors, undefined vars, pipe failures
set -euo pipefail

# --- Configuration loading ---

load_config() {
    local config_path="${1:-deploy/config/cluster.env}"
    if [ ! -f "$config_path" ]; then
        echo "ERROR: Config file not found: $config_path"
        echo "  Copy deploy/config/cluster.env.template to deploy/config/cluster.env and fill in values."
        exit 1
    fi
    # Export all sourced variables so envsubst and child processes can see them
    set -a
    # shellcheck disable=SC1090
    source "$config_path"
    set +a

    # Compute derived values
    if [ -n "${MAAS_ROUTE_SEGMENT:-}" ]; then
        export LLM_BASE_URL="${MAAS_GATEWAY_URL}/${MAAS_ROUTE_SEGMENT}"
    else
        export LLM_BASE_URL="${MAAS_GATEWAY_URL}"
    fi
    export IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo 'latest')}"
    # Extract MLflow hostname for use in OpenShell policy templates
    export MLFLOW_HOST
    MLFLOW_HOST=$(echo "${MLFLOW_TRACKING_URI:-}" | sed 's|^[^:]*://||' | sed 's|/.*||' | sed 's|:[0-9]*$||')
    # Extract LLM gateway hostname for use in OpenShell policy templates
    export LLM_HOST
    LLM_HOST=$(echo "${MAAS_GATEWAY_URL:-}" | sed 's|^[^:]*://||' | sed 's|/.*||' | sed 's|:[0-9]*$||')
}

# --- Preflight validation ---

preflight() {
    # Call this first in every entry-point script.
    # Validates prerequisites, failing with a one-line fix per problem.
    local errors=0

    if ! oc whoami &>/dev/null; then
        echo "ERROR: Not logged into OpenShift. Run: oc login <cluster-url>"
        errors=$((errors + 1))
    fi

    if [ -z "${NAMESPACE:-}" ]; then
        echo "ERROR: NAMESPACE not set. Source deploy/config/cluster.env or pass --config."
        errors=$((errors + 1))
    else
        local current_project
        current_project=$(oc project -q 2>/dev/null || echo "")
        if [ "$current_project" != "$NAMESPACE" ]; then
            echo "WARNING: Current project '$current_project' != configured NAMESPACE '$NAMESPACE'"
            echo "  Switching to $NAMESPACE..."
            oc project "$NAMESPACE" 2>/dev/null || {
                echo "ERROR: Cannot switch to namespace $NAMESPACE"
                errors=$((errors + 1))
            }
        fi
    fi

    for var in MAAS_GATEWAY_URL LLM_MODEL_DEFAULT GIT_REPO CLUSTER_DOMAIN; do
        if [ -z "${!var:-}" ]; then
            echo "ERROR: Required config variable $var is empty. Check deploy/config/cluster.env."
            errors=$((errors + 1))
        fi
    done

    for tool in oc helm envsubst python3; do
        if ! command -v "$tool" &>/dev/null; then
            echo "ERROR: Required tool '$tool' not found on PATH."
            errors=$((errors + 1))
        fi
    done

    if [ $errors -gt 0 ]; then
        echo ""
        echo "$errors preflight error(s). Fix the above and retry."
        exit 1
    fi
}

preflight_openshell() {
    # Additional check for scripts that need the openshell CLI.
    if ! command -v openshell &>/dev/null; then
        echo "ERROR: 'openshell' CLI not found on PATH."
        echo "  Install from: https://docs.openshell.dev/install"
        exit 1
    fi
    ensure_openshell_portforward
}

# --- OpenShell port-forward management ---

ensure_openshell_portforward() {
    # The openshell CLI needs localhost:18080 → openshell-0:8080.
    # Always verifies the forward targets the current NAMESPACE — a stale
    # forward to a different namespace's gateway silently routes sandbox
    # operations to the wrong OpenShell instance.
    local existing_ns
    existing_ns=$(ps aux | grep "port-forward.*openshell.*18080" | grep -v grep | grep -oE -- "-n [^ ]+" | head -1 | sed 's/-n //' || true)

    if [ -n "$existing_ns" ] && [ "$existing_ns" = "$NAMESPACE" ]; then
        if curl -s --max-time 2 http://localhost:18080/ &>/dev/null; then
            return 0
        fi
    fi

    # Kill any existing forward (wrong namespace or unresponsive)
    pkill -f "port-forward.*openshell.*18080" 2>/dev/null || true
    sleep 1

    log "  Starting port-forward to openshell-0 in $NAMESPACE..."
    oc port-forward pod/openshell-0 18080:8080 -n "$NAMESPACE" &>/dev/null &
    local pf_pid=$!
    sleep 3

    if ! curl -s --max-time 2 http://localhost:18080/ &>/dev/null; then
        log "ERROR: Failed to establish openshell port-forward (PID $pf_pid)"
        log "  Check: oc get pod openshell-0 -n $NAMESPACE"
        exit 1
    fi
}

# --- Secret reading ---

read_secret() {
    # Read a key from a K8s Secret into stdout (for command substitution).
    #
    # Usage: local val; val=$(read_secret <secret-name> <key>)
    #
    # SECURITY: NEVER echo, log, or tee the return value.
    # This helper enforces set +x for its body to prevent trace leaks.
    # Callers must never run under set -x while holding the result.
    local secret_name="$1"
    local key="$2"

    # Suppress trace output while handling secret material
    { set +x; } 2>/dev/null

    local value
    value=$(oc get secret "$secret_name" -n "$NAMESPACE" -o jsonpath="{.data.$key}" 2>/dev/null | base64 -d)

    if [ -z "$value" ]; then
        echo "ERROR: Could not read key '$key' from secret '$secret_name' in namespace '$NAMESPACE'" >&2
        exit 1
    fi

    printf '%s' "$value"
}

read_secret_optional() {
    # Like read_secret but returns empty string if the secret or key is absent.
    # Use for credentials that may legitimately not exist (e.g. FHIR client in dev mode).
    #
    # SECURITY: same rules as read_secret — NEVER echo, log, or tee.
    local secret_name="$1"
    local key="$2"

    { set +x; } 2>/dev/null

    local value
    value=$(oc get secret "$secret_name" -n "$NAMESPACE" -o jsonpath="{.data.$key}" 2>/dev/null | base64 -d 2>/dev/null || echo "")

    printf '%s' "$value"
}

# --- Template rendering ---

render_template() {
    # Render a .tmpl file with envsubst, writing to deploy/.rendered/
    # Usage: render_template <input.yaml.tmpl> <output.yaml>
    local input="$1"
    local output="$2"

    mkdir -p "$(dirname "$output")"
    envsubst < "$input" > "$output"
}

render_templates_dir() {
    # Render all .tmpl files in a directory
    # Usage: render_templates_dir <input-dir> <output-dir>
    local input_dir="$1"
    local output_dir="$2"

    mkdir -p "$output_dir"
    for tmpl in "$input_dir"/*.tmpl; do
        [ -f "$tmpl" ] || continue
        local basename
        basename=$(basename "$tmpl" .tmpl)
        envsubst < "$tmpl" > "$output_dir/$basename"
    done
}

# --- Pod lifecycle helpers ---

wait_for_pod_ready() {
    # Wait for a pod to reach Running + Ready state, with periodic status.
    # Usage: wait_for_pod_ready <pod-name> [timeout-seconds]
    local pod_name="$1"
    local timeout="${2:-90}"
    local last_phase=""

    for i in $(seq 1 "$timeout"); do
        local phase
        phase=$(oc get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
        if [ "$phase" = "Running" ]; then
            local ready
            ready=$(oc get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo "false")
            if [ "$ready" = "true" ]; then
                log "  $pod_name ready after ${i}s"
                return 0
            fi
        fi
        # Log phase changes and periodic heartbeat
        if [ "$phase" != "$last_phase" ]; then
            log "  $pod_name: $phase (${i}s)"
            last_phase="$phase"
        elif [ $((i % 15)) -eq 0 ]; then
            log "  $pod_name: still $phase (${i}s)"
        fi
        sleep 1
    done

    log "ERROR: Pod $pod_name not ready after ${timeout}s (phase: ${last_phase:-unknown})"
    return 1
}

label_pod() {
    # Apply K8s labels to a pod. Call after wait_for_pod_ready.
    # Retries transient failures (the sandbox controller updates pod metadata
    # concurrently right after readiness, so oc label can hit a conflict); a
    # persistent failure warns and continues — under set -e an unguarded
    # failure here silently aborted the whole deploy mid-sandbox-creation
    # (observed repeatedly 2026-08-27), which is far worse than a missing
    # cosmetic label.
    # Usage: label_pod <pod-name> <app-name> <instance>
    local pod_name="$1"
    local app_name="$2"
    local instance="$3"

    for _ in 1 2 3; do
        if oc label pod "$pod_name" -n "$NAMESPACE" \
            "app.kubernetes.io/name=$app_name" \
            "app.kubernetes.io/instance=$instance" \
            --overwrite 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    log "WARNING: could not label pod $pod_name after 3 attempts — continuing"
    return 0
}

expose_service() {
    # Expose a sandbox's port as a K8s Service, retrying transient failures.
    # A failure here silently breaks the pod-split path (no in-cluster DNS name
    # for the pod), so on persistent failure we surface the real error and fail
    # the deploy rather than swallowing it. Usage:
    #   expose_service <sandbox-name> [port] [scheme]
    local name="$1"
    local port="${2:-8080}"
    local scheme="${3:-http}"
    local attempts=3
    local out=""

    for attempt in $(seq 1 "$attempts"); do
        if out=$(openshell service expose "$name" "$port" "$scheme" 2>&1); then
            return 0
        fi
        # An already-exposed service is success, not a failure to retry.
        if echo "$out" | grep -qiE 'already ex[il]st'; then
            return 0
        fi
        log "  service expose for $name failed (attempt ${attempt}/${attempts}); retrying in 3s..."
        sleep 3
    done

    echo "ERROR: could not expose service for '$name' on port ${port} after ${attempts} attempts:" >&2
    echo "$out" >&2
    return 1
}

# --- Build helpers ---

start_build_and_wait() {
    # Start a build and wait for completion. Returns 0 on success.
    # Usage: start_build_and_wait <buildconfig-name>
    local bc_name="$1"

    local build_name
    build_name=$(oc start-build "$bc_name" -n "$NAMESPACE" -o name 2>/dev/null)

    if [ -z "$build_name" ]; then
        echo "ERROR: Failed to start build for $bc_name"
        return 1
    fi

    echo "  Started: $build_name"
    oc wait "$build_name" -n "$NAMESPACE" --for=jsonpath='{.status.phase}'=Complete --timeout=600s 2>/dev/null
}

start_builds_parallel() {
    # Start multiple builds in parallel, then poll with progress every 30s.
    # Usage: start_builds_parallel bc1 bc2 bc3 ...
    local build_names=()
    local failed=0

    for bc in "$@"; do
        local build_name
        build_name=$(oc start-build "$bc" -n "$NAMESPACE" -o name 2>/dev/null || echo "")
        if [ -n "$build_name" ]; then
            log "  Started: $build_name"
            build_names+=("$build_name")
        else
            log "  ERROR: Failed to start build for $bc"
            failed=$((failed + 1))
        fi
    done

    local total=${#build_names[@]}
    local timeout="${BUILD_TIMEOUT:-1200}"
    local timeout_min=$((timeout / 60))
    log "  Waiting for $total builds (polling every 30s, timeout ${timeout_min}m)..."
    local start_time=$SECONDS

    while true; do
        local completed=0
        local running=0
        local pending=0
        local build_failed=0

        for build_name in "${build_names[@]}"; do
            local phase
            phase=$(oc get "$build_name" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
            case "$phase" in
                Complete) completed=$((completed + 1));;
                Failed|Error|Cancelled) build_failed=$((build_failed + 1));;
                Running) running=$((running + 1));;
                *) pending=$((pending + 1));;
            esac
        done

        local elapsed=$(( SECONDS - start_time ))
        log "  [${elapsed}s] Builds: $completed/$total complete, $running running, $pending pending, $build_failed failed"

        if [ $((completed + build_failed)) -ge $total ]; then
            break
        fi

        if [ $elapsed -ge $timeout ]; then
            log "  ERROR: Build timeout after ${timeout}s"
            break
        fi

        sleep 30
    done

    # Report results
    for build_name in "${build_names[@]}"; do
        local phase
        phase=$(oc get "$build_name" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
        local duration
        duration=$(oc get "$build_name" -n "$NAMESPACE" -o jsonpath='{.status.duration}' 2>/dev/null || echo "?")
        if [ "$phase" = "Complete" ]; then
            log "  ✓ $build_name ($phase, ${duration})"
        else
            log "  ✗ $build_name ($phase)"
            oc logs "$build_name" -n "$NAMESPACE" --tail=10 2>/dev/null || true
            failed=$((failed + 1))
        fi
    done

    if [ $failed -gt 0 ]; then
        log "  $failed build(s) failed"
        return 1
    fi
    return 0
}

# --- Pruning ---

prune_builds() {
    # Delete completed/failed build pods for a component prefix.
    # Usage: prune_builds <prefix>  (e.g., "acp-writer")
    local prefix="$1"
    oc get builds -n "$NAMESPACE" --no-headers | \
        grep "^${prefix}" | \
        grep -E "Complete|Failed|Cancelled" | \
        awk '{print $1}' | \
        xargs -r oc delete build -n "$NAMESPACE" 2>/dev/null || true
}

prune_image_tags() {
    # Keep the last N SHA tags on an ImageStream, delete older ones.
    # Sorted by creation timestamp (newest first). Never deletes the current IMAGE_TAG.
    # Usage: prune_image_tags <imagestream> [keep-count]
    local is_name="$1"
    local keep="${2:-5}"

    # Get SHA tags sorted by creation timestamp (newest first)
    local sorted_sha_tags
    sorted_sha_tags=$(oc get is "$is_name" -n "$NAMESPACE" -o json 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    tags = []
    for t in data.get('status', {}).get('tags', []):
        name = t.get('tag', '')
        # SHA tags are 7-char hex
        if len(name) == 7 and all(c in '0123456789abcdef' for c in name):
            items = t.get('items', [])
            created = items[0].get('created', '') if items else ''
            tags.append((created, name))
    # Sort by creation timestamp, newest first
    tags.sort(reverse=True)
    for _, name in tags:
        print(name)
except:
    pass
" 2>/dev/null || echo "")

    if [ -z "$sorted_sha_tags" ]; then
        return 0
    fi

    local count=0
    for tag in $sorted_sha_tags; do
        count=$((count + 1))
        # Never delete the currently-deployed tag
        if [ "$tag" = "${IMAGE_TAG:-}" ]; then
            continue
        fi
        if [ $count -gt "$keep" ]; then
            echo "  Pruning $is_name:$tag"
            oc tag -d "$is_name:$tag" -n "$NAMESPACE" 2>/dev/null || true
        fi
    done
}

# --- Sandbox verification with retry ---

verify_sandboxes() {
    # Verify sandboxes are Running with the correct image tag and routed health.
    # Retries for up to VERIFY_TIMEOUT seconds (default 90) to handle startup races.
    # Usage: verify_sandboxes <image_tag> <sandbox1> <sandbox2> ...
    # Returns: number of errors (0 = all passed)
    local tag="$1"; shift
    local sandboxes=("$@")
    local timeout="${VERIFY_TIMEOUT:-90}"
    local interval=10
    local errors=0
    local start=$SECONDS

    while true; do
        errors=0
        local elapsed=$(( SECONDS - start ))
        local all_ok=true

        for sb in "${sandboxes[@]}"; do
            local phase
            phase=$(oc get pod "$sb" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
            if [ "$phase" != "Running" ]; then
                all_ok=false
                continue
            fi

            local img
            img=$(oc get pod "$sb" -n "$NAMESPACE" -o jsonpath='{.spec.containers[0].image}' 2>/dev/null || echo "")
            if ! echo "$img" | grep -q ":${tag}"; then
                all_ok=false
                continue
            fi

            local code
            code=$(oc exec deployment/openshell-router -n "$NAMESPACE" -- \
                curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
                -H "Host: ${sb}--http.openshell.localhost" \
                http://openshell-http:8080/health 2>/dev/null || echo "000")
            if [ "$code" != "200" ]; then
                all_ok=false
                continue
            fi
        done

        if [ "$all_ok" = true ]; then
            break
        fi

        if [ $elapsed -ge "$timeout" ]; then
            break
        fi

        log "  Sandboxes not ready yet (${elapsed}s / ${timeout}s) — retrying in ${interval}s..."
        sleep "$interval"
    done

    # Final check with full reporting
    for sb in "${sandboxes[@]}"; do
        local phase
        phase=$(oc get pod "$sb" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
        if [ "$phase" != "Running" ]; then
            echo "  ✗ $sb: not running (phase: $phase)"
            errors=$((errors + 1))
            continue
        fi

        local img
        img=$(oc get pod "$sb" -n "$NAMESPACE" -o jsonpath='{.spec.containers[0].image}' 2>/dev/null || echo "")
        if echo "$img" | grep -q ":${tag}"; then
            echo "  ✓ $sb: Running, image :${tag}"
        else
            echo "  ✗ $sb: wrong image tag (expected :${tag}, got ${img})"
            errors=$((errors + 1))
        fi

        local user
        user=$(oc exec "$sb" -n "$NAMESPACE" -- ps aux 2>/dev/null | grep uvicorn | grep -v grep | awk '{print $1}' | head -1 || true)
        if [ "$user" = "default" ]; then
            echo "  ✓ $sb: supervised (user: default)"
        elif [ -n "$user" ]; then
            echo "  ✗ $sb: uvicorn running as '$user' (expected: default)"
            errors=$((errors + 1))
        else
            echo "  ✗ $sb: no uvicorn process found"
            errors=$((errors + 1))
        fi
    done

    echo ""
    log "Routed health checks:"
    for sb in "${sandboxes[@]}"; do
        local code
        code=$(oc exec deployment/openshell-router -n "$NAMESPACE" -- \
            curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
            -H "Host: ${sb}--http.openshell.localhost" \
            http://openshell-http:8080/health 2>/dev/null || echo "000")
        if [ "$code" = "200" ]; then
            echo "  ✓ $sb routed: HTTP 200"
        else
            echo "  ✗ $sb routed: HTTP $code"
            errors=$((errors + 1))
        fi
    done

    return $errors
}

# --- Deploy state tracking ---

save_deploy_state() {
    # Write a ConfigMap recording which tag was deployed for this component.
    # verify.sh reads this instead of requiring a global IMAGE_TAG.
    # Usage: save_deploy_state <component> <tag>
    local component="$1"
    local tag="$2"
    oc apply -f - <<EOF || log "WARNING: failed to save deploy-state for ${component} (verify may use a stale tag)"
apiVersion: v1
kind: ConfigMap
metadata:
  name: deploy-state-${component}
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/managed-by: deploy-framework
    app.kubernetes.io/component: deploy-state
data:
  component: "${component}"
  tag: "${tag}"
  timestamp: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
EOF
}

read_deploy_tag() {
    # Read the deployed tag for a component. Returns empty if not set.
    # Usage: local tag; tag=$(read_deploy_tag <component>)
    local component="$1"
    oc get configmap "deploy-state-${component}" -n "$NAMESPACE" -o jsonpath='{.data.tag}' 2>/dev/null || echo ""
}

resolve_deploy_tag() {
    # Resolve the image tag for verification. Precedence:
    #   1. Explicit --tag argument (caller passes as $2)
    #   2. deploy-state ConfigMap for the component
    #   3. IMAGE_TAG from load_config (git HEAD)
    # Usage: IMAGE_TAG=$(resolve_deploy_tag <component> "${TAG_OVERRIDE:-}")
    local component="$1"
    local explicit="${2:-}"
    if [ -n "$explicit" ]; then
        echo "$explicit"
        return
    fi
    local cm_tag
    cm_tag=$(read_deploy_tag "$component")
    if [ -n "$cm_tag" ]; then
        echo "$cm_tag"
        return
    fi
    echo "${IMAGE_TAG:-}"
}

# --- Logging ---

log() { echo "[deploy] $*"; }
log_step() { echo ""; echo "=== $* ==="; }
