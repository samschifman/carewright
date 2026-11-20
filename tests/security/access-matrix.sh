#!/usr/bin/env bash
# Peer-workload boundary evidence. Dry-run never claims a verified boundary.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
dry_run=false
if [[ ${1:-} == --dry-run ]]; then dry_run=true; shift; fi
[[ $# == 0 ]] || { echo "Usage: $0 [--dry-run]" >&2; exit 2; }
: "${NAMESPACE:?Set NAMESPACE}"
: "${PROBE_NAMESPACE:?Set a separate, existing PROBE_NAMESPACE}"
: "${ENVIRONMENT:?Set ENVIRONMENT to openshift or kind}"
: "${UI_ORIGIN:?Set the writer UI origin}"
[[ $NAMESPACE != "$PROBE_NAMESPACE" ]] || { echo 'Probe namespace must differ' >&2; exit 2; }
[[ $ENVIRONMENT == openshift || $ENVIRONMENT == kind ]] || exit 2
[[ $NAMESPACE =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && $PROBE_NAMESPACE =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || exit 2
cli=${KUBECTL:-kubectl}
probe_image=${PROBE_IMAGE:-quay.io/carewright/curl:dry-run-only}
if ! $dry_run; then
  : "${PROBE_IMAGE:?Set PROBE_IMAGE to a published quay.io/carewright image containing curl and sh}"
  : "${INGESTER_UI_ORIGIN:?Set INGESTER_UI_ORIGIN}"
  : "${INGESTER_CR_NAME:?Set INGESTER_CR_NAME}"
  : "${EHR_ORIGINS:?Comma-separated declared Medplum API/app origins}"
fi
[[ $probe_image == quay.io/carewright/* ]] || { echo 'Probe image must use quay.io/carewright' >&2; exit 2; }
ingester_origin=${INGESTER_UI_ORIGIN:-https://ingester.example.test}
failures=0
pending=0
counter=0
temporary_dir=$(mktemp -d)
created_namespaces=()
created_pods=()
cleanup() {
  local i
  if ! $dry_run; then
    for ((i=0;i<${#created_pods[@]};i++)); do
      "$cli" -n "${created_namespaces[$i]}" delete pod "${created_pods[$i]}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    done
  fi
  rm -rf "$temporary_dir"
}
trap cleanup EXIT
row() {
  local label=$1 state=$2 observed=$3
  observed=${observed//$'\n'/ }
  observed=${observed//|/;}
  printf '| %s | %s | %s |\n' "$label" "$state" "$observed"
  if [[ $state == FAIL ]]; then failures=$((failures+1)); fi
  if [[ $state == PENDING* ]]; then pending=$((pending+1)); fi
}
print_command() { printf 'DRY-RUN: ' >&2; printf '%q ' "$@" >&2; printf '\n' >&2; }
probe() {
  local namespace=$1 url=$2 method=${3:-GET}
  counter=$((counter+1))
  local name="cw-access-$$-$counter"
  if $dry_run; then
    print_command "$cli" -n "$namespace" run "$name" --rm -i --restart=Never --image="$probe_image" --command -- curl --connect-timeout 5 --max-time 5 "$url"
    PROBE_EXIT=7; PROBE_HTTP=000; return
  fi
  created_namespaces+=("$namespace")
  created_pods+=("$name")
  local output
  # Inputs are argv, never interpolated into shell code. Kubernetes startup errors
  # cannot be interpreted as network isolation success: only a curl result counts.
  output=$("$cli" -n "$namespace" run "$name" --rm -i --restart=Never --pod-running-timeout=60s --image="$probe_image" --image-pull-policy=Always --command -- sh -c '
    status=$(curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 5 -X "$2" -H "Content-Type: application/json" --data "{}" "$1" 2>/dev/null)
    code=$?
    printf "\n__CW_RESULT__%s:%s\n" "$code" "$status"
  ' probe "$url" "$method" </dev/null 2>&1) || true
  local marker
  marker=$(printf '%s\n' "$output" | sed -n 's/^__CW_RESULT__//p' | tail -1)
  if [[ ! $marker =~ ^[0-9]+:[0-9]{3}$ ]]; then PROBE_EXIT=999; PROBE_HTTP=000; return; fi
  PROBE_EXIT=${marker%%:*}; PROBE_HTTP=${marker#*:}
}
blocked_targets() {
  local namespace=$1 label=$2 accepted=$3 name url refused=0 admitted=0 setup=0 permitted=0
  while IFS=$'\t' read -r name url; do
    probe "$namespace" "$url"
    if [[ $PROBE_EXIT == 7 || $PROBE_EXIT == 28 ]]; then
      refused=$((refused+1))
    elif [[ $PROBE_EXIT == 6 ]]; then
      setup=$((setup+1)); printf 'DNS lookup failed for %s\n' "$name" >&2
    elif [[ $PROBE_EXIT == 999 ]]; then
      setup=$((setup+1)); printf 'Probe pod failed for %s (no curl result)\n' "$name" >&2
    elif [[ $accepted == true && ",$NAMESPACE_WIDE_ACCEPTED_TARGETS," == *",$name,"* ]]; then
      permitted=$((permitted+1)); printf 'Accepted namespace-wide target: %s\n' "$name" >&2
    else
      admitted=$((admitted+1)); printf 'Unexpected reachability: %s curl=%s HTTP=%s\n' "$name" "$PROBE_EXIT" "$PROBE_HTTP" >&2
    fi
  done <"$temporary_dir/targets.tsv"
  if ((admitted+setup)); then row "$label" FAIL "refused=$refused reached=$admitted probe-errors=$setup"
  elif ((permitted)); then row "$label" ACCEPTED "refused=$refused explicitly-accepted=$permitted"
  else row "$label" "$($dry_run && echo DRY-RUN || echo PASS)" "refused=$refused"; fi
}
NAMESPACE_WIDE_ACCEPTED_TARGETS=${NAMESPACE_WIDE_ACCEPTED_TARGETS:-}
printf '| Probe | Result | Observed |\n| --- | --- | --- |\n'
if $dry_run; then
  printf 'gateway/dns\thttp://gateway.%s.svc:8080/\ngateway/pod\thttp://10.0.0.10:8080/\nwriter/sandbox\thttp://proxy.%s.svc/writer/\n' "$NAMESPACE" "$NAMESPACE" >"$temporary_dir/targets.tsv"
  print_command "$cli" -n "$NAMESPACE" get services,pods,sandboxrequests -o json
else
  "$cli" -n "$NAMESPACE" get services,pods,sandboxrequests -o json >"$temporary_dir/private.json"
  python3 "$script_dir/inventory.py" targets "$temporary_dir/private.json" "$NAMESPACE" >"$temporary_dir/targets.tsv"
  if [[ -n ${EXTRA_TARGETS_FILE:-} ]]; then
    python3 - "$EXTRA_TARGETS_FILE" >>"$temporary_dir/targets.tsv" <<'PYEXTRA'
import sys
from urllib.parse import urlsplit
for line in open(sys.argv[1]):
    name,url=line.rstrip('\n').split('\t')
    parsed=urlsplit(url)
    if not name or parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or any(c in name+url for c in '\r\n\t'):
        raise ValueError('Invalid extra target')
    print(name+'\t'+url)
PYEXTRA
  fi
  # Read this inventory against docs/security.md: omitted dependencies are an error.
  : "${REQUIRED_TARGET_PATTERNS:=openshell,medplum,postgres,redis,decision,mlflow,minio|odf|s3,litellm|model|inference}"
  IFS=, read -r -a patterns <<<"$REQUIRED_TARGET_PATTERNS"
  for pattern in "${patterns[@]}"; do
    if ! grep -Eq "$pattern" "$temporary_dir/targets.tsv"; then row 'Inventory completeness' FAIL "No target matching $pattern"; fi
  done
fi
blocked_targets "$PROBE_NAMESPACE" 'Other namespace: DNS, pod IP, sandbox endpoints' false
blocked_targets "$NAMESPACE" 'Unlabelled same-namespace pod' true
if [[ -n ${GATEWAY_CREATE_URL:-} ]]; then
  probe "$NAMESPACE" "$GATEWAY_CREATE_URL" POST
  if [[ $PROBE_EXIT == 0 && ( $PROBE_HTTP == 401 || $PROBE_HTTP == 403 ) ]]; then row 'Gateway creation without credential' PASS "HTTP $PROBE_HTTP"
  else row 'Gateway creation without credential' FAIL "curl=$PROBE_EXIT HTTP=$PROBE_HTTP (network refusal is insufficient)"; fi
else
  row 'Gateway creation without credential' PENDING-A3 'Set GATEWAY_CREATE_URL to the A3 creation API; do not count 404 as authentication'
fi
for origin in "$UI_ORIGIN" "$ingester_origin"; do
  for path in / /health; do
    if $dry_run; then print_command curl --max-time 10 -o /dev/null -w '%{http_code}' "$origin$path"; row "UI $origin$path" DRY-RUN 'expected HTTP 200'
    else
      status=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$origin$path") || status=000
      if [[ $status == 200 ]]; then row "UI $origin$path" PASS "HTTP $status"; else row "UI $origin$path" FAIL "HTTP $status"; fi
    fi
  done
  for probe_case in 'GET /api/v1/runs 401' 'POST /api/v1/runs/security-probe/review/careplan 401' 'POST /api/v1/notify-review 401,503' 'POST /api/v1/notifications/artifacts-available 401,503'; do
    if [[ $origin == "$ingester_origin" && $probe_case == *notifications/artifacts-available* ]]; then continue; fi
    read -r method path expected <<<"$probe_case"
    if [[ $origin == "$ingester_origin" ]]; then path=${path%careplan}; [[ $path != */review/ ]] || path+=manifest; fi
    if $dry_run; then print_command curl -X "$method" "$origin$path"; row "Unauthenticated $method $origin$path" DRY-RUN "$expected"
    else
      status=$(curl -sS --max-time 10 -X "$method" -H 'Content-Type: application/json' --data '{}' -o /dev/null -w '%{http_code}' "$origin$path") || status=000
      if [[ ",$expected," == *",$status,"* ]]; then row "Unauthenticated $method $origin$path" PASS "HTTP $status"; else row "Unauthenticated $method $origin$path" FAIL "HTTP $status"; fi
    fi
  done
done
if $dry_run; then row 'Browser SMART configuration' DRY-RUN '/smart-config.json expected 404'
else
  status=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$UI_ORIGIN/smart-config.json") || status=000
  if [[ $status == 404 ]]; then row 'Browser SMART configuration' PASS 'HTTP 404'; else row 'Browser SMART configuration' FAIL "HTTP $status"; fi
fi
if $dry_run; then
  print_command "$script_dir/../../carewright-operator/scripts/ingester-launch-link.sh" "$NAMESPACE" "${INGESTER_CR_NAME:-ingester}"
  row 'Operator ingester launch and session' DRY-RUN 'Mint link; consume with cookie jar; session 200'
else
  # Link is kept in a local private file/variable and never printed in the table.
  KUBECTL="$cli" "$script_dir/../../carewright-operator/scripts/ingester-launch-link.sh" "$NAMESPACE" "$INGESTER_CR_NAME" >"$temporary_dir/link"
  chmod 600 "$temporary_dir/link"
  link=$(cat "$temporary_dir/link")
  status=$(curl -sS --max-time 10 -c "$temporary_dir/cookies" -o /dev/null -w '%{http_code}' "$link") || status=000
  session_status=$(curl -sS --max-time 10 -b "$temporary_dir/cookies" -o /dev/null -w '%{http_code}' "$ingester_origin/api/v1/session") || session_status=000
  if [[ $status == 302 && $session_status == 200 ]]; then row 'Operator ingester launch and session' PASS 'launch 302; session 200'; else row 'Operator ingester launch and session' FAIL "launch=$status session=$session_status"; fi
fi
if $dry_run; then
  print_command "$cli" -n "$NAMESPACE" get routes -o json
  row 'Public exposure inventory' DRY-RUN 'Only both UIs and declared Medplum entries'
else
  if [[ $ENVIRONMENT == openshift ]]; then "$cli" -n "$NAMESPACE" get routes,services -o json >"$temporary_dir/public.json"
  else "$cli" -n "$NAMESPACE" get ingresses,services -o json >"$temporary_dir/public.json"; fi
  allowed=$(python3 -c 'import sys,urllib.parse; print(",".join(urllib.parse.urlsplit(x).hostname for x in sys.argv[1].split(",")))' "$UI_ORIGIN,$ingester_origin,$EHR_ORIGINS")
  # Explicitly declared NodePorts/LoadBalancers may be named after operator review.
  allowed="$allowed${PUBLIC_SERVICE_ALLOWLIST:+,$PUBLIC_SERVICE_ALLOWLIST}"
  if observed=$(python3 "$script_dir/inventory.py" exposure "$temporary_dir/public.json" "$allowed"); then row 'Public exposure inventory' PASS "$observed"; else row 'Public exposure inventory' FAIL "$observed"; fi
fi
row 'Clinician browser positive path' MANUAL "At $UI_ORIGIN: EHR sign-in, launch, consent, correct patient/run, one bound approval, logout; record evidence in track-b-status.md"
if ((failures)); then exit 1; fi
if ((pending)) && ! $dry_run; then exit 2; fi
