#!/usr/bin/env bash
# Tests for mock-EHR/deploy/smart-lib.sh and the chart's deploy-time SMART
# settings. No cluster needed; the chart checks run only when helm is installed.
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$DIR/smart-lib.sh"

failures=0
check() {  # check <description> <expected status> <actual status>
    if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected $2, got $3)"; failures=$((failures + 1)); fi
}

for value in "" "https://writer.example" "http://localhost:3001" \
    "https://a.example,https://b.example:8443" "https://writer-ns.apps.cluster.example"; do
    validate_redirect_origins "$value" 2>/dev/null; check "accepts '$value'" 0 $?
done

for value in "writer.example" "https://writer.example/" "https://writer.example/api/v1/launch/callback" \
    "https://writer.example?x=1" "https://user@writer.example" "https://writer.example:port" \
    "javascript:alert(1)" "ftp://writer.example" "https://a.example, https://b.example" \
    "https://a.example,,https://b.example" ",https://a.example" "https://a.example," \
    "https://*.example" "https://-bad.example" 'https://a.example";x="'; do
    validate_redirect_origins "$value" 2>/dev/null; check "rejects '$value'" 1 $?
done

[ "$(helm_escape_commas 'https://a.example,https://b.example')" = 'https://a.example\,https://b.example' ]
check "escapes commas for helm" 0 $?

# --- one writer origin for the registered callback and the allowlist ---------
resolve() {  # resolve <ns> <domain> [VAR=value ...] -> "writer|allowlist" or "ERR"
    local ns="$1" domain="$2"; shift 2
    (
        unset SMART_WRITER_ORIGIN SMART_REDIRECT_ORIGINS VITE_SMART_REDIRECT_ORIGINS
        for assignment in "$@"; do export "$assignment"; done
        if resolve_smart_origins "$ns" "$domain" 2>/dev/null; then
            printf '%s|%s' "$SMART_WRITER_ORIGIN" "$SMART_REDIRECT_ORIGINS"
        else
            printf 'ERR'
        fi
    )
}
expect() {  # expect <description> <expected> <actual>
    if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected '$2', got '$3')"; failures=$((failures + 1)); fi
}
D='https://acp-ui-ns.apps.c.example'
expect "standard deploy derives one origin for both" "$D|$D" "$(resolve ns apps.c.example)"
expect "explicit writer origin drives both (operator route)" \
    "https://care-plan-writer-ui-ns.apps.c.example|https://care-plan-writer-ui-ns.apps.c.example" \
    "$(resolve ns apps.c.example SMART_WRITER_ORIGIN=https://care-plan-writer-ui-ns.apps.c.example)"
expect "trailing slash on the writer origin is dropped" "https://w.example|https://w.example" \
    "$(resolve ns apps.c.example SMART_WRITER_ORIGIN=https://w.example/)"
expect "extra allowed origins are kept when they include the writer" "$D|$D,http://localhost:3001" \
    "$(resolve ns apps.c.example "SMART_REDIRECT_ORIGINS=$D,http://localhost:3001")"
expect "legacy VITE variable still supplies the allowlist" "$D|$D" \
    "$(resolve ns apps.c.example "VITE_SMART_REDIRECT_ORIGINS=$D")"
expect "allowlist that omits the registered writer origin is refused" "ERR" \
    "$(resolve ns apps.c.example SMART_REDIRECT_ORIGINS=https://other.example)"
expect "writer origin must be a single origin" "ERR" \
    "$(resolve ns apps.c.example SMART_WRITER_ORIGIN=https://a.example,https://b.example)"
expect "writer origin with a path is refused" "ERR" \
    "$(resolve ns apps.c.example SMART_WRITER_ORIGIN=https://w.example/app)"
expect "malformed allowlist is refused" "ERR" \
    "$(resolve ns apps.c.example "SMART_REDIRECT_ORIGINS=$D,not-an-origin")"
expect "no domain and no writer origin leaves both empty" "|" "$(resolve ns "")"
expect "no domain but an explicit allowlist is honoured" "|https://w.example" \
    "$(resolve ns "" SMART_REDIRECT_ORIGINS=https://w.example)"

deployments='{"items":[
 {"metadata":{"name":"writer-bff"},"spec":{"template":{"spec":{"containers":[{"name":"bff","env":[
   {"name":"PLAIN","value":"x"},
   {"name":"SMART_CLIENT_ID","valueFrom":{"secretKeyRef":{"name":"smart-client-credentials","key":"SMART_CLIENT_ID"}}}]}]}}}},
 {"metadata":{"name":"uses-envfrom"},"spec":{"template":{"spec":{"containers":[{"name":"c","envFrom":[{"secretRef":{"name":"smart-client-credentials"}}]}]}}}},
 {"metadata":{"name":"uses-volume"},"spec":{"template":{"spec":{"containers":[{"name":"c"}],"volumes":[{"name":"v","secret":{"secretName":"smart-client-credentials"}}]}}}},
 {"metadata":{"name":"uses-init"},"spec":{"template":{"spec":{"containers":[{"name":"c"}],"initContainers":[{"name":"i","env":[
   {"name":"S","valueFrom":{"secretKeyRef":{"name":"smart-client-credentials","key":"k"}}}]}]}}}},
 {"metadata":{"name":"acp-ui"},"spec":{"template":{"spec":{"containers":[{"name":"ui","env":[{"name":"A","value":"b"}]}]}}}},
 {"metadata":{"name":"other-secret"},"spec":{"template":{"spec":{"containers":[{"name":"c","env":[
   {"name":"K","valueFrom":{"secretKeyRef":{"name":"llm-credentials","key":"k"}}},
   {"name":"C","valueFrom":{"configMapKeyRef":{"name":"smart-client-credentials","key":"k"}}}]}]}}}},
 {"metadata":{"name":"empty"},"spec":{}}
]}'
found="$(printf '%s' "$deployments" | deployments_referencing_secret smart-client-credentials | tr '\n' ' ')"
[ "$found" = "writer-bff uses-envfrom uses-volume uses-init " ]
check "finds exactly the Deployments that use the Secret (not the UI, not a same-named ConfigMap)" 0 $?
printf 'not json' | deployments_referencing_secret smart-client-credentials >/dev/null 2>&1
check "unreadable deployment list is an error, not 'no consumers'" 2 $?
printf '{"kind":"Status"}' | deployments_referencing_secret smart-client-credentials >/dev/null 2>&1
check "a response that is not a Deployment list is an error" 2 $?
[ -z "$(printf '{"items":[]}' | deployments_referencing_secret smart-client-credentials)" ]
check "no deployments yields no consumers" 0 $?

# --- credential refresh: verified before anything is restarted ---------------
SCRATCH="$(mktemp -d)"
OC_LOG="$SCRATCH/oc.log"; LOG_OUT="$SCRATCH/log.out"
oc() {  # a stand-in for the cluster client, driven by the OC_* variables
    case "$1 $2" in
        "get secret") [ "${OC_SECRET_FAIL:-}" = 1 ] && return 1; printf '%s' "${OC_SECRET_VERSION:-}";;
        "get deployments") [ "${OC_DEPLOY_FAIL:-}" = 1 ] && return 1; printf '%s' "${OC_DEPLOYMENTS:-}";;
        "rollout restart") echo "restart $3" >> "$OC_LOG"; [ "$3" != "deployment/${OC_RESTART_FAILS:-}" ];;
        "rollout status") echo "status $3" >> "$OC_LOG"; [ "$3" != "deployment/${OC_STATUS_FAILS:-}" ];;
        *) echo "unexpected oc $*" >> "$OC_LOG"; return 1;;
    esac
}
log() { echo "$*" >> "$LOG_OUT"; }
ONE_CONSUMER='{"items":[{"metadata":{"name":"writer-bff"},"spec":{"template":{"spec":{"containers":[{"name":"bff","envFrom":[{"secretRef":{"name":"smart-client-credentials"}}]}]}}}},{"metadata":{"name":"acp-ui"},"spec":{"template":{"spec":{"containers":[{"name":"ui"}]}}}}]}'
scenario() {  # scenario <description> <before> <expected status> <expected oc calls> <expected log text> [OC_VAR=value ...]
    local description="$1" before="$2" want_status="$3" want_calls="$4" want_log="$5"; shift 5
    : > "$OC_LOG"; : > "$LOG_OUT"
    (
        unset OC_SECRET_FAIL OC_SECRET_VERSION OC_DEPLOY_FAIL OC_DEPLOYMENTS OC_RESTART_FAILS OC_STATUS_FAILS
        OC_DEPLOYMENTS="$ONE_CONSUMER"
        for assignment in "$@"; do export "$assignment"; done
        restart_smart_secret_consumers ns "$before"
    ); local got_status=$?
    local got_calls; got_calls="$(tr '\n' ';' < "$OC_LOG")"
    if [ "$got_status" = "$want_status" ] && [ "$got_calls" = "$want_calls" ] && grep -q -- "$want_log" "$LOG_OUT"; then
        echo "ok   - refresh: $description"
    else
        echo "FAIL - refresh: $description (status $got_status, calls '$got_calls', log: $(tr '\n' '|' < "$LOG_OUT"))"
        failures=$((failures + 1))
    fi
}
RESTARTED='restart deployment/writer-bff;status deployment/writer-bff;'
scenario "stale existing Secret is not reported as a refresh and nothing restarts" 100 1 "" "was NOT updated by this deploy" OC_SECRET_VERSION=100
scenario "updated Secret restarts its consumer, and only its consumer" 100 0 "$RESTARTED" "Secret updated by the loader" OC_SECRET_VERSION=101
scenario "Secret created by this deploy restarts its consumer" absent 0 "$RESTARTED" "Secret updated by the loader" OC_SECRET_VERSION=5
scenario "Secret absent after the loader is its own failure" absent 1 "" "Secret not found after the loader ran"
scenario "Secret lookup failure is its own failure" 100 1 "" "could not read the smart-client-credentials Secret" OC_SECRET_FAIL=1
scenario "Deployment lookup failure is not 'no consumers'" 100 1 "" "could not list Deployments" OC_SECRET_VERSION=101 OC_DEPLOY_FAIL=1
scenario "unreadable Deployment list is not 'no consumers'" 100 1 "" "could not read the Deployment list" OC_SECRET_VERSION=101 OC_DEPLOYMENTS=garbage
scenario "no consumer yet is reported as such and succeeds" 100 0 "" "No running Deployment consumes" OC_SECRET_VERSION=101 'OC_DEPLOYMENTS={"items":[]}'
scenario "a failed restart is reported with the manual command" 100 1 "restart deployment/writer-bff;" "oc rollout restart deployment/writer-bff -n ns" OC_SECRET_VERSION=101 OC_RESTART_FAILS=writer-bff
scenario "a rollout that does not finish is reported" 100 1 "$RESTARTED" "did not finish rolling out" OC_SECRET_VERSION=101 OC_STATUS_FAILS=writer-bff
scenario "unknown earlier version restarts but does not claim a verified refresh" unknown 1 "$RESTARTED" "refresh is NOT verified" OC_SECRET_VERSION=7
if grep -q "Secret updated by the loader" "$LOG_OUT"; then echo "FAIL - refresh: unverified case claimed an update"; failures=$((failures + 1)); fi
unset -f oc log
rm -rf "$SCRATCH"

if command -v helm >/dev/null 2>&1; then
    render() {  # render <origins> -> "value=<smartRedirectOrigins from the rendered config.json>"
        helm template t "$DIR/chart" \
            --set-string mockEhrApp.smartRedirectOrigins="$(helm_escape_commas "$1")" \
            --show-only templates/mock-ehr-app-configmap.yaml 2>/dev/null \
        | python3 -c '
import json, sys
lines = [line.strip() for line in sys.stdin]
block = lines[lines.index("{"):lines.index("}") + 1]
print("value=" + json.loads("".join(block))["smartRedirectOrigins"])'
    }
    two='https://a.example,https://b.example:8443'
    [ "$(render "$two")" = "value=$two" ]; check "chart renders two origins as valid JSON" 0 $?
    [ "$(render "")" = "value=" ]; check "chart default renders an empty allowlist" 0 $?
    default="$(helm template t "$DIR/chart" --show-only templates/mock-ehr-app-configmap.yaml 2>/dev/null | grep -c '"smartRedirectOrigins": ""')"
    check "chart with no value set renders an empty allowlist" 1 "$default"
    checksum() {
        helm template t "$DIR/chart" --set-string mockEhrApp.smartRedirectOrigins="$1" \
            --show-only templates/mock-ehr-app-deployment.yaml 2>/dev/null | grep 'checksum/config'
    }
    [ -n "$(checksum "")" ] && [ "$(checksum "")" != "$(checksum "https://a.example")" ]
    check "changing the allowlist rolls the pod (checksum annotation changes)" 0 $?

    # The loader's registered launch/redirect URIs and the allowlist share one origin.
    pair() {  # pair <helm args...> -> "launch|redirect|allowlist", or "ERR" when the chart refuses
        local job config
        job="$(helm template t "$DIR/chart" -n ns --set loader.enabled=true "$@" \
            --show-only templates/loader-job.yaml 2>/dev/null)" || { printf 'ERR'; return; }
        config="$(helm template t "$DIR/chart" -n ns --set loader.enabled=true "$@" \
            --show-only templates/mock-ehr-app-configmap.yaml 2>/dev/null)" || { printf 'ERR'; return; }
        JOB="$job" CONFIG="$config" python3 -c '
import json, os, re
job = os.environ["JOB"]
def env(name):
    return re.search(r"- name: %s\n\s+value: (\S+)" % name, job).group(1)
lines = [line.strip() for line in os.environ["CONFIG"].splitlines()]
block = lines[lines.index("{"):lines.index("}") + 1]
print("|".join([env("ACP_WRITER_LAUNCH_URI"), env("ACP_WRITER_REDIRECT_URI"), json.loads("".join(block))["smartRedirectOrigins"]]))'
    }
    expect "chart: standard deploy registers and allows the same origin" \
        "$D/api/v1/launch|$D/api/v1/launch/callback|$D" "$(pair --set clusterDomain=apps.c.example)"
    W='https://care-plan-writer-ui-ns.apps.c.example'
    expect "chart: explicit writer origin drives the callback and the allowlist" \
        "$W/api/v1/launch|$W/api/v1/launch/callback|$W" \
        "$(pair --set clusterDomain=apps.c.example --set-string acpWriterOrigin=$W)"
    expect "chart: extra allowed origins keep the writer origin" \
        "$W/api/v1/launch|$W/api/v1/launch/callback|$W,http://localhost:3001" \
        "$(pair --set-string acpWriterOrigin=$W --set-string "mockEhrApp.smartRedirectOrigins=$W\\,http://localhost:3001")"
    expect "chart: an allowlist without the registered writer origin does not render" "ERR" \
        "$(pair --set-string acpWriterOrigin=$W --set-string mockEhrApp.smartRedirectOrigins=https://other.example)"
    # What deploy.sh passes for a standard deploy, exactly.
    ( unset SMART_WRITER_ORIGIN SMART_REDIRECT_ORIGINS VITE_SMART_REDIRECT_ORIGINS
      resolve_smart_origins ns apps.c.example
      pair --set clusterDomain=apps.c.example --set-string acpWriterOrigin="$SMART_WRITER_ORIGIN" \
           --set-string mockEhrApp.smartRedirectOrigins="$(helm_escape_commas "$SMART_REDIRECT_ORIGINS")" ) > /tmp/smart-lib-pair.$$ 2>/dev/null
    expect "chart: deploy.sh's standard arguments give a launchable configuration" \
        "$D/api/v1/launch|$D/api/v1/launch/callback|$D" "$(cat /tmp/smart-lib-pair.$$)"; rm -f /tmp/smart-lib-pair.$$
else
    echo "skip - helm not installed; chart rendering not checked"
fi

[ "$failures" -eq 0 ] && echo "All smart-lib tests passed" || { echo "$failures failure(s)"; exit 1; }
