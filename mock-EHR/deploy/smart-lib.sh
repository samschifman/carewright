#!/usr/bin/env bash
# mock-EHR/deploy/smart-lib.sh — helpers for the SMART launch settings.
# Sourced by deploy.sh; no side effects on load.

# validate_redirect_origins "<comma-separated origins>"
# Each entry must be an exact origin: scheme://host[:port], nothing else.
# An empty list is valid (it refuses every SMART launch). Prints the offending
# entry to stderr and returns 1 on the first bad one.
validate_redirect_origins() {
    local value="$1" origin
    [ -z "$value" ] && return 0
    case "$value" in
        ,*|*,|*,,*) echo "SMART_REDIRECT_ORIGINS has an empty entry" >&2; return 1;;
    esac
    local IFS=','
    for origin in $value; do
        if ! [[ "$origin" =~ ^https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]]; then
            echo "SMART_REDIRECT_ORIGINS entry is not an exact origin (scheme://host[:port]): $origin" >&2
            return 1
        fi
    done
}

# helm_escape_commas "<value>" — escape commas for helm --set-string.
helm_escape_commas() {
    printf '%s' "${1//,/\\,}"
}

# deployments_referencing_secret <secret-name>
# Reads `oc get deployments -o json` on stdin and prints the name of every
# Deployment whose pod template uses the Secret (env secretKeyRef, envFrom
# secretRef, or a secret/projected volume), one per line. Returns 2 when the
# input is not a Deployment list, so a failed lookup is never read as "none".
deployments_referencing_secret() {
    python3 -c '
import json, sys

secret = sys.argv[1]
try:
    items = json.load(sys.stdin)["items"]
except Exception:
    # An unreadable list is not "no consumers": the caller must be able to tell.
    sys.exit(2)
for item in items:
    spec = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
    names = set()
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for env in container.get("env") or []:
            names.add((((env.get("valueFrom") or {}).get("secretKeyRef")) or {}).get("name"))
        for source in container.get("envFrom") or []:
            names.add((source.get("secretRef") or {}).get("name"))
    for volume in spec.get("volumes") or []:
        names.add((volume.get("secret") or {}).get("secretName"))
        for source in (volume.get("projected") or {}).get("sources") or []:
            names.add((source.get("secret") or {}).get("name"))
    if secret in names:
        print(item["metadata"]["name"])
' "$1"
}

# default_writer_origin <namespace> <cluster-domain>
# The script/Helm-deployed writer UI route. Empty when the domain is unknown.
default_writer_origin() {
    [ -n "$1" ] && [ -n "$2" ] && printf 'https://acp-ui-%s.%s' "$1" "$2"
    return 0
}

# origin_in_list <origin> <comma-separated origins>
origin_in_list() {
    local origin
    local IFS=','
    for origin in $2; do
        [ "$origin" = "$1" ] && return 0
    done
    return 1
}

# resolve_smart_origins <namespace> <cluster-domain>
# Settles the one writer origin used for both the SMART client the loader
# registers and the OAuth page's redirect allowlist. Reads and then sets:
#   SMART_WRITER_ORIGIN     explicit, else the default writer UI route
#   SMART_REDIRECT_ORIGINS  explicit (or VITE_SMART_REDIRECT_ORIGINS), else the
#                           writer origin; an explicit list must include it
# Returns 1 with a message on stderr when a value is malformed or inconsistent.
resolve_smart_origins() {
    SMART_WRITER_ORIGIN="${SMART_WRITER_ORIGIN:-}"
    SMART_WRITER_ORIGIN="${SMART_WRITER_ORIGIN%/}"
    [ -z "$SMART_WRITER_ORIGIN" ] && SMART_WRITER_ORIGIN="$(default_writer_origin "$1" "$2")"
    case "$SMART_WRITER_ORIGIN" in
        *,*) echo "SMART_WRITER_ORIGIN must be a single origin: $SMART_WRITER_ORIGIN" >&2; return 1;;
    esac
    if ! validate_redirect_origins "$SMART_WRITER_ORIGIN" 2>/dev/null; then
        echo "SMART_WRITER_ORIGIN is not an exact origin (scheme://host[:port]): $SMART_WRITER_ORIGIN" >&2
        return 1
    fi
    SMART_REDIRECT_ORIGINS="${SMART_REDIRECT_ORIGINS:-${VITE_SMART_REDIRECT_ORIGINS:-$SMART_WRITER_ORIGIN}}"
    validate_redirect_origins "$SMART_REDIRECT_ORIGINS" || return 1
    if [ -n "$SMART_WRITER_ORIGIN" ] && ! origin_in_list "$SMART_WRITER_ORIGIN" "$SMART_REDIRECT_ORIGINS"; then
        echo "SMART_REDIRECT_ORIGINS must include the writer origin $SMART_WRITER_ORIGIN: the loader registers that origin's callback, so the EHR would refuse the launch it is configured for" >&2
        return 1
    fi
}

# smart_secret_version <namespace>
# Prints the resourceVersion of the smart-client-credentials Secret, or nothing
# when the Secret does not exist. Returns 1 when the lookup itself failed.
smart_secret_version() {
    oc get secret smart-client-credentials -n "$1" --ignore-not-found \
        -o jsonpath='{.metadata.resourceVersion}' 2>/dev/null
}

# restart_smart_secret_consumers <namespace> <secret version before the loader ran>
#
# The loader registers a new SMART client on every run and rewrites the Secret.
# Workloads read it as environment at start, so a running consumer keeps the old
# client until it is restarted. This restarts the Deployments that reference the
# Secret, but only after checking that the Secret really changed: the loader
# logs a warning and still completes when it cannot store the credentials, and
# an existing Secret from an earlier run would otherwise look like a refresh.
#
# Pass "unknown" as the version when it could not be read before the loader.
# Uses `oc` and `log`. Returns 0 when the refresh was verified and every
# consumer was restarted (or none exists yet); 1 for anything it could not
# verify or complete, each with its own message.
restart_smart_secret_consumers() {
    local namespace="$1" before="$2" after deployments consumers deployment status=0

    if ! after="$(smart_secret_version "$namespace")"; then
        log "WARNING: could not read the smart-client-credentials Secret — SMART credential refresh NOT verified, nothing restarted"
        return 1
    fi
    if [ -z "$after" ]; then
        log "WARNING: SMART credentials Secret not found after the loader ran — SMART launch will fail"
        return 1
    fi
    if [ "$before" = "$after" ]; then
        log "WARNING: the smart-client-credentials Secret was NOT updated by this deploy (still at the version from before the loader ran)."
        log "         The loader registers a new SMART client each run; it either did not run or failed to store the new credentials."
        log "         Nothing was restarted. Check the loader output, then re-run this deploy:"
        log "         oc logs job/cpg-mock-ehr-loader -n $namespace"
        return 1
    fi
    if [ "$before" = "unknown" ]; then
        log "WARNING: could not read the Secret before the loader ran, so its refresh is NOT verified; restarting its consumers anyway"
        status=1
    else
        log "SMART credentials Secret updated by the loader"
    fi

    if ! deployments="$(oc get deployments -n "$namespace" -o json 2>/dev/null)"; then
        log "WARNING: could not list Deployments — cannot tell which workloads use the refreshed SMART credentials."
        log "         Restart the writer BFF yourself: oc rollout restart deployment/<writer-bff> -n $namespace"
        return 1
    fi
    if ! consumers="$(printf '%s' "$deployments" | deployments_referencing_secret smart-client-credentials)"; then
        log "WARNING: could not read the Deployment list — cannot tell which workloads use the refreshed SMART credentials."
        log "         Restart the writer BFF yourself: oc rollout restart deployment/<writer-bff> -n $namespace"
        return 1
    fi
    if [ -z "$consumers" ]; then
        log "No running Deployment consumes the SMART credentials yet (the writer BFF reads them when it starts)"
        return "$status"
    fi
    for deployment in $consumers; do
        log "Restarting deployment/$deployment to load the refreshed SMART credentials"
        if ! oc rollout restart "deployment/$deployment" -n "$namespace" >/dev/null 2>&1; then
            log "WARNING: could not restart deployment/$deployment — SMART launch will fail until it is restarted:"
            log "         oc rollout restart deployment/$deployment -n $namespace"
            status=1
        elif ! oc rollout status "deployment/$deployment" -n "$namespace" --timeout=120s >/dev/null 2>&1; then
            log "WARNING: deployment/$deployment did not finish rolling out within 120s — check it before launching"
            status=1
        fi
    done
    return "$status"
}
