#!/usr/bin/env bash
# deploy/verify-all.sh — Full deployment verification + secret-leak scan
#
# Usage:
#   ./deploy/verify-all.sh [--config <path>]
#   ./deploy/verify-all.sh --e2e   # adds functional QA round trip + terminology probe
#
# Runs:
#   1. Per-component verify.sh (pods, images, supervision, routed health)
#   2. Shared checks (MinIO, MaaS, SonataFlow)
#   3. S7 secret-leak scan (mandatory — exits non-zero on any leak)
#   4. Orphan check
#   5. [--e2e] QA round trip + terminology egress probe

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck disable=SC1091
source "$REPO_ROOT/deploy/lib.sh"

CONFIG_PATH="$REPO_ROOT/deploy/config/cluster.env"
E2E=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2;;
        --e2e) E2E=true; shift;;
        -h|--help)
            echo "Usage: deploy/verify-all.sh [--config <path>] [--e2e]"
            echo ""
            echo "  (default)   Component checks + shared infra + secret-leak scan"
            echo "  --e2e       Also run QA round trip and terminology egress probe"
            exit 0;;
        *) shift;;
    esac
done

load_config "$CONFIG_PATH"
preflight

ERRORS=0

# --- 1. Per-component verification ---

for component in mock-EHR acp-writer cpg-ingester; do
    script="$REPO_ROOT/$component/deploy/verify.sh"
    if [ -f "$script" ]; then
        "$script" --config "$CONFIG_PATH" || {
            log "FAIL: $component verification failed"
            ERRORS=$((ERRORS + 1))
        }
    else
        log "SKIP: $script not found"
    fi
done

# --- 2. Shared infrastructure checks ---

log_step "Shared infrastructure checks"

# MinIO
minio_code=$(oc exec deployment/minio -n "$NAMESPACE" -- \
    curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    http://localhost:9000/minio/health/live 2>/dev/null || echo "000")
if [ "$minio_code" = "200" ]; then
    echo "  ✓ MinIO: HTTP 200"
else
    echo "  ✗ MinIO: HTTP $minio_code"
    ERRORS=$((ERRORS + 1))
fi

# MaaS (one test call through the gateway). MAAS_ROUTE_SEGMENT may be empty (EnMaaS):
# joining with a bare '/' then yields '//v1/...', which the gateway rejects with 503.
# SECURITY: the API key is piped over stdin — it must NEVER appear in the
# oc exec argument list (argv is visible in the pod's process list and is
# recorded in the K8s API server audit log of the exec request).
maas_code=$(read_secret llm-credentials LLM_API_KEY | \
    oc exec -i deployment/openshell-router -n "$NAMESPACE" -- sh -c '
        curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
            -X POST "$1/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer $(cat)" \
            -d "{\"model\":\"$2\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}]}"
    ' _ "${MAAS_GATEWAY_URL}${MAAS_ROUTE_SEGMENT:+/$MAAS_ROUTE_SEGMENT}" "$LLM_MODEL_DEFAULT" \
    2>/dev/null || echo "000")
if [ "$maas_code" = "200" ]; then
    echo "  ✓ MaaS LLM call: HTTP 200"
else
    echo "  ✗ MaaS LLM call: HTTP $maas_code"
    ERRORS=$((ERRORS + 1))
fi

# SonataFlow pods
for sf_pod in sonataflow-platform-data-index-service sonataflow-platform-jobs-service; do
    ready=$(oc get deployment "$sf_pod" -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
    if [ "${ready:-0}" -ge 1 ]; then
        echo "  ✓ $sf_pod: ready"
    else
        echo "  ✗ $sf_pod: not ready"
        ERRORS=$((ERRORS + 1))
    fi
done

# --- 3. S7 Secret-leak scan (mandatory) ---

log_step "S7 Secret-leak scan"

{ set +x; } 2>/dev/null

# Read secret prefixes to scan for (never print the full values)
LLM_KEY_PREFIX=$(read_secret llm-credentials LLM_API_KEY | head -c 10)
MINIO_PASS=$(read_secret minio-credentials ARTIFACT_STORE_SECRET_KEY | head -c 10)

# Classify pods by OpenShell sandbox label and extract env values.
# Sandbox pods (agents.x-k8s.io/sandbox-name-hash label) receive secrets
# via --env — documented S6 exposure, reported as WARNING not FAIL.
SCAN_RESULT=$(oc get pods -n "$NAMESPACE" --field-selector=status.phase=Running -o json 2>/dev/null | \
    python3 -c "
import sys, json
data = json.load(sys.stdin)
sb_count = 0
non_sb_values = []
for pod in data.get('items', []):
    labels = pod.get('metadata', {}).get('labels', {})
    is_sandbox = 'agents.x-k8s.io/sandbox-name-hash' in labels
    if is_sandbox:
        sb_count += 1
        continue
    for c in pod.get('spec', {}).get('containers', []):
        for env in c.get('env', []):
            if 'value' in env and 'valueFrom' not in env:
                non_sb_values.append(env['value'])
print(f'SB_COUNT={sb_count}')
for v in non_sb_values:
    print(v)
" 2>/dev/null || echo "SB_COUNT=0")

SB_COUNT=$(echo "$SCAN_RESULT" | head -1 | sed 's/SB_COUNT=//')
NON_SB_ENV_VALUES=$(echo "$SCAN_RESULT" | tail -n +2)
REAL_LEAK=false

for prefix_label in "LLM_API_KEY:$LLM_KEY_PREFIX" "MINIO_SECRET:$MINIO_PASS"; do
    label="${prefix_label%%:*}"
    prefix="${prefix_label##*:}"
    [ -z "$prefix" ] && continue
    hits=$(echo "$NON_SB_ENV_VALUES" | grep -F "$prefix" | head -5 || true)
    if [ -n "$hits" ]; then
        echo "  ✗ LEAK: $label prefix found in non-sandbox pod env values"
        REAL_LEAK=true
    fi
done

if [ "$REAL_LEAK" = true ]; then
    echo "  SECRET LEAK DETECTED in non-sandbox pod specs. Secrets should use secretKeyRef."
    ERRORS=$((ERRORS + 1))
else
    echo "  ✓ No secret leaks in non-sandbox pod specs"
fi

if [ "$SB_COUNT" -gt 0 ]; then
    echo "  ⚠ $SB_COUNT sandbox pod(s) have secrets via OpenShell --env (documented S6 exposure)"
fi

# --- 4. Orphan check ---

log_step "Orphan check"

EXPECTED_PREFIXES="sb-|cpg-mock-ehr|cpg-decision-svc|acp-ui|acp-writer-mcp|cpg-ingester-ui|cpg-ingester-bff|minio|openshell|sonataflow|cpg-gateway|acpwriter|cpgingester|mock-ehr-mcp"
orphans=$(oc get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -v -E "$EXPECTED_PREFIXES" | grep -v "Completed\|Error" | head -10 || true)
if [ -n "$orphans" ]; then
    echo "  ⚠ Unexpected pods found:"
    echo "$orphans"
else
    echo "  ✓ No orphaned pods"
fi

# --- 5. E2E functional verification (optional) ---

if [ "$E2E" = true ]; then
    log_step "E2E functional verification"

    # QA round trip: deploy test DMN + execute via routed path
    echo "  QA round trip:"
    DMN_FILE="$REPO_ROOT/cpg-ingester/data/golden/treatment-recommendation.dmn"
    PATIENT_FILE="$REPO_ROOT/mock-EHR/data/patient-bundle-medication.json"

    if [ -f "$DMN_FILE" ] && [ -f "$PATIENT_FILE" ]; then
        ROUTER_POD=$(oc get pods -n "$NAMESPACE" -l app=openshell-router --field-selector=status.phase=Running -o name 2>/dev/null | head -1 | sed 's|pod/||')
        if [ -z "$ROUTER_POD" ]; then
            echo "    ✗ openshell-router pod not found — skipping e2e"
            ERRORS=$((ERRORS + 1))
        else
            # Deploy DMN via router Host header
            oc cp "$DMN_FILE" "${ROUTER_POD}:/tmp/test-dmn.dmn" -n "$NAMESPACE" 2>/dev/null
            deploy_code=$(oc exec "$ROUTER_POD" -n "$NAMESPACE" -- \
                curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
                -X POST -H "Host: acp-decision-engine" \
                -H "Content-Type: application/xml" \
                --data-binary @/tmp/test-dmn.dmn \
                http://localhost:8080/api/v1/decisions/models 2>/dev/null || echo "000")

            if [ "$deploy_code" = "201" ] || [ "$deploy_code" = "200" ]; then
                echo "    ✓ DMN model deployed: HTTP $deploy_code"
            else
                echo "    ✗ DMN model deploy failed: HTTP $deploy_code"
                ERRORS=$((ERRORS + 1))
            fi

            # Build execute-dmn payload locally, copy to router
            python3 -c "
import json
with open('$PATIENT_FILE') as f:
    bundle = json.load(f)
payload = {
    'ips_bundle': bundle,
    'applicable_dmn_models': [{'id':'treatment-recommendation','name':'Treatment Recommendation',
        'inputs':[{'name':'Systolic BP','type':'number'},{'name':'Has Diabetes','type':'boolean'},
                  {'name':'Has Kidney Disease','type':'boolean'}],
        'outputs':[{'name':'Action','type':'string'}]}],
    'dmn_dependency_graph': [['treatment-recommendation']],
}
with open('/tmp/test-execute-payload.json', 'w') as f:
    json.dump(payload, f)
" 2>/dev/null
            oc cp /tmp/test-execute-payload.json "${ROUTER_POD}:/tmp/test-execute-payload.json" -n "$NAMESPACE" 2>/dev/null

            # Execute via router (LLM calls — generous timeout)
            result=$(oc exec "$ROUTER_POD" -n "$NAMESPACE" -- \
                curl -s --max-time 300 \
                -X POST -H "Host: acp-llm-reasoning" \
                -H "Content-Type: application/json" \
                --data-binary @/tmp/test-execute-payload.json \
                http://localhost:8080/api/v1/execute-dmn 2>/dev/null || echo "{}")

            if echo "$result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
r = d.get('dmn_results', [{}])[0]
assert 'input_resolution' in r, 'no input_resolution'
assert r.get('error') is None or r.get('error') == '', 'has error: ' + str(r.get('error'))
has_match_basis = any(v.get('match_basis') for v in r.get('input_resolution', {}).values())
assert has_match_basis, 'no match_basis in audit'
print('PASS')
" 2>/dev/null; then
                echo "    ✓ QA round trip: decision result with match_basis audit"
            else
                echo "    ✗ QA round trip failed"
                ERRORS=$((ERRORS + 1))
            fi
        fi
    else
        echo "    ⚠ Test fixtures not found — skipping QA round trip"
    fi

    # Terminology egress probe (from sandbox — tests OpenShell egress policy)
    echo "  Terminology egress:"
    if oc get pod sb-llm-reasoning -n "$NAMESPACE" &>/dev/null; then
        term_code=$(oc exec sb-llm-reasoning -n "$NAMESPACE" -- \
            curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
            "https://tx.fhir.org/r4/ValueSet/\$expand?url=http://snomed.info/sct?fhir_vs&filter=test&count=1" \
            2>/dev/null || echo "000")
        if [ "$term_code" = "200" ]; then
            echo "    ✓ Terminology egress (tx.fhir.org via sandbox): HTTP 200"
        else
            echo "    ⚠ Terminology egress blocked or unavailable: HTTP $term_code"
            echo "      Pipeline degrades gracefully but terminology resolution is limited."
        fi
    else
        echo "    ⚠ sb-llm-reasoning not present (--skip-openshell?) — skipping terminology probe"
    fi
fi

# --- Summary ---

echo ""
if [ $ERRORS -eq 0 ]; then
    log "ALL VERIFICATION CHECKS PASSED"
else
    log "$ERRORS CHECK(S) FAILED"
    exit 1
fi
