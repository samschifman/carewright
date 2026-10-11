#!/usr/bin/env bash
# deploy/load-published-artifacts.sh — Load cpg-ingester published artifacts into acp-writer
#
# TEMPORARY stand-in for the delivery/notification flow (not wired yet).
# Fetches published artifacts from MinIO and loads them into acp-writer
# via routed API calls through the openshell-router.
#
# Usage:
#   ./deploy/load-published-artifacts.sh --config <cluster.env> <cpg-id>
#   ./deploy/load-published-artifacts.sh --config <cluster.env> --replace <cpg-id>
#   ./deploy/load-published-artifacts.sh --config deploy/config/cluster.env UNK-HTN-UNDATED
#
# Prerequisites:
#   - cpg-ingester has published artifacts under cpg-artifacts/published/<cpg-id>/
#   - acp-writer sandboxes are running
#   - openshell-router is running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck disable=SC1091
source "$REPO_ROOT/deploy/lib.sh"

CONFIG_PATH="$REPO_ROOT/deploy/config/cluster.env"
CPG_ID=""
REPLACE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2;;
        --replace) REPLACE=true; shift;;
        -h|--help)
            echo "Usage: deploy/load-published-artifacts.sh --config <cluster.env> [--replace] <cpg-id>"
            echo ""
            echo "TEMPORARY: loads published CPG artifacts from MinIO into acp-writer."
            echo "Will be replaced by the delivery/notification flow."
            exit 0;;
        *) CPG_ID="$1"; shift;;
    esac
done

if [ -z "$CPG_ID" ]; then
    echo "ERROR: CPG ID required. Usage: $0 --config <cluster.env> <cpg-id>"
    exit 1
fi

load_config "$CONFIG_PATH"
preflight

ROUTER_POD=$(oc get pods -n "$NAMESPACE" -l app=openshell-router --field-selector=status.phase=Running -o name 2>/dev/null | head -1 | sed 's|pod/||')
if [ -z "$ROUTER_POD" ]; then
    echo "ERROR: openshell-router pod not found"
    exit 1
fi

# Find a pod with MinIO access (BFF has it)
BFF_POD=$(oc get pods -n "$NAMESPACE" -l app.kubernetes.io/name=cpg-ingester-bff -o name --no-headers 2>/dev/null | head -1 | sed 's|pod/||')
if [ -z "$BFF_POD" ]; then
    echo "ERROR: cpg-ingester-bff pod not found (needed for MinIO access)"
    exit 1
fi

BASE_KEY="published/${CPG_ID}"

log_step "Loading published artifacts for CPG: $CPG_ID"

# Step 1: Register guideline metadata
log "Registering guideline metadata..."
oc exec "$BFF_POD" -n "$NAMESPACE" -- python3 -c "
import os, boto3, sys
s3 = boto3.client('s3',
    endpoint_url=os.getenv('MINIO_ENDPOINT'),
    aws_access_key_id=os.getenv('ARTIFACT_STORE_ACCESS_KEY'),
    aws_secret_access_key=os.getenv('ARTIFACT_STORE_SECRET_KEY'))
try:
    body = s3.get_object(Bucket='cpg-artifacts', Key='${BASE_KEY}/metadata.json')['Body'].read()
    sys.stdout.buffer.write(body)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null > /tmp/cpg-metadata.json

oc cp /tmp/cpg-metadata.json "$ROUTER_POD:/tmp/cpg-metadata.json" -n "$NAMESPACE"
code=$(oc exec "$ROUTER_POD" -n "$NAMESPACE" -- \
    curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
    -X POST -H "Host: acp-llm-reasoning" \
    -H "Content-Type: application/json" \
    --data-binary @/tmp/cpg-metadata.json \
    http://localhost:8080/api/v1/guidelines 2>/dev/null)
log "  Guideline registration: HTTP $code"
if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    echo "ERROR: Guideline registration failed with HTTP $code" >&2
    exit 1
fi

# Step 2: Ingest recommendations
log "Ingesting recommendations..."
oc exec "$BFF_POD" -n "$NAMESPACE" -- python3 -c "
import os, boto3, sys
s3 = boto3.client('s3',
    endpoint_url=os.getenv('MINIO_ENDPOINT'),
    aws_access_key_id=os.getenv('ARTIFACT_STORE_ACCESS_KEY'),
    aws_secret_access_key=os.getenv('ARTIFACT_STORE_SECRET_KEY'))
try:
    body = s3.get_object(Bucket='cpg-artifacts', Key='${BASE_KEY}/recommendations.json')['Body'].read()
    sys.stdout.buffer.write(body)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null > /tmp/cpg-recs.json

oc cp /tmp/cpg-recs.json "$ROUTER_POD:/tmp/cpg-recs.json" -n "$NAMESPACE"
code=$(oc exec "$ROUTER_POD" -n "$NAMESPACE" -- \
    curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
    -X POST -H "Host: acp-llm-reasoning" \
    -H "Content-Type: application/json" \
    --data-binary @/tmp/cpg-recs.json \
    http://localhost:8080/api/v1/knowledge/recommendations/batch 2>/dev/null)
log "  Recommendations ingest: HTTP $code"
if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
    echo "ERROR: Recommendations ingest failed with HTTP $code" >&2
    exit 1
fi

# Step 3: Deploy DMN models
log "Deploying DMN models..."
dmn_count=0
encoded_cpg=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$CPG_ID")
dmn_url="http://localhost:8080/api/v1/decisions/models?source_cpg=${encoded_cpg}"
if [ "$REPLACE" = true ]; then
    dmn_url="${dmn_url}&replace=true"
fi
dmn_list=$(mktemp)
bpmn_latest_list=$(mktemp)
trap 'rm -f "$dmn_list" "$bpmn_latest_list"' EXIT
oc exec "$BFF_POD" -n "$NAMESPACE" -- python3 -c "
import os, boto3, sys, json
s3 = boto3.client('s3',
    endpoint_url=os.getenv('MINIO_ENDPOINT'),
    aws_access_key_id=os.getenv('ARTIFACT_STORE_ACCESS_KEY'),
    aws_secret_access_key=os.getenv('ARTIFACT_STORE_SECRET_KEY'))
objs = s3.list_objects_v2(Bucket='cpg-artifacts', Prefix='${BASE_KEY}/dmn/').get('Contents', [])
for o in objs:
    if o['Key'].endswith('.dmn'):
        print(json.dumps({'key': o['Key'], 'name': o['Key'].rsplit('/',1)[-1].replace('.dmn','')}))
" 2>/dev/null > "$dmn_list"

while IFS= read -r line; do
    [ -z "$line" ] && continue
    key=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin)['key'])")
    name=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin)['name'])")

    oc exec "$BFF_POD" -n "$NAMESPACE" -- python3 -c "
import os, boto3, sys
s3 = boto3.client('s3',
    endpoint_url=os.getenv('MINIO_ENDPOINT'),
    aws_access_key_id=os.getenv('ARTIFACT_STORE_ACCESS_KEY'),
    aws_secret_access_key=os.getenv('ARTIFACT_STORE_SECRET_KEY'))
body = s3.get_object(Bucket='cpg-artifacts', Key='$key')['Body'].read()
sys.stdout.buffer.write(body)
" 2>/dev/null > "/tmp/dmn-upload.dmn"

    oc cp "/tmp/dmn-upload.dmn" "$ROUTER_POD:/tmp/dmn-upload.dmn" -n "$NAMESPACE"
    response=$(oc exec "$ROUTER_POD" -n "$NAMESPACE" -- \
        curl -sS -w '\n__HTTP_STATUS__:%{http_code}' --max-time 15 \
        -X POST -H "Host: acp-decision-engine" \
        -H "Content-Type: application/xml" \
        --data-binary @/tmp/dmn-upload.dmn \
        "$dmn_url" 2>/dev/null)
    code="${response##*__HTTP_STATUS__:}"
    body="${response%$'\n__HTTP_STATUS__:'*}"
    log "  $name: HTTP $code"
    if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
        echo "ERROR: Failed to deploy DMN model '$name' for CPG '$CPG_ID' (HTTP $code)" >&2
        if [ "$code" = "409" ] || [ "$code" = "422" ]; then
            if ! printf '%s' "$body" | python3 -c 'import json,sys
d=json.load(sys.stdin)
if d.get("error"):
    print("    error:", d["error"])
if d.get("messages"):
    print("    messages:", json.dumps(d["messages"], separators=(",", ":")))
if not d.get("error") and not d.get("messages"):
    print("    response:", json.dumps(d, separators=(",", ":")))'; then
                printf '    response: %s\n' "$body" >&2
            fi
        else
            printf '    response: %s\n' "$body" >&2
        fi
        exit 1
    fi
    dmn_count=$((dmn_count + 1))
done < "$dmn_list"

# Step 4: Register the latest version of each BPMN automation template
log "Registering BPMN automation templates..."
bpmn_count=0
automation_url="http://localhost:8080/api/v1/automation/templates?source_cpg=$encoded_cpg"
if [ "$REPLACE" = true ]; then
    automation_url="$automation_url&replace=true"
fi
oc exec "$BFF_POD" -n "$NAMESPACE" -- python3 -c '
import json, os, sys, boto3
s3 = boto3.client("s3",
    endpoint_url=os.getenv("MINIO_ENDPOINT"),
    aws_access_key_id=os.getenv("ARTIFACT_STORE_ACCESS_KEY"),
    aws_secret_access_key=os.getenv("ARTIFACT_STORE_SECRET_KEY"))
prefix = "published/" + sys.argv[1] + "/bpmn/"
objects = s3.list_objects_v2(Bucket="cpg-artifacts", Prefix=prefix).get("Contents", [])
for obj in objects:
    latest_key = obj["Key"]
    if not latest_key.endswith("/latest.json"):
        continue
    latest = json.loads(s3.get_object(Bucket="cpg-artifacts", Key=latest_key)["Body"].read())
    ref = latest.get("template_ref")
    if not isinstance(ref, str) or not ref:
        raise ValueError("latest pointer is missing template_ref: " + latest_key)
    bucket, key = ref.split(":", 1) if ":" in ref else ("cpg-artifacts", ref)
    if bucket != "cpg-artifacts" or not key.startswith(prefix) or not key.endswith(".template.json"):
        raise ValueError("latest pointer contains an invalid template_ref: " + ref)
    print(json.dumps({"latest_key": latest_key, "template_ref": ref,
                      "template_id": latest.get("template_id", "")}))
' "$CPG_ID" 2>/dev/null > "$bpmn_latest_list"

while IFS= read -r line; do
    [ -z "$line" ] && continue
    template_ref=$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["template_ref"])')
    template_id=$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("template_id", "unknown"))')
    oc exec "$BFF_POD" -n "$NAMESPACE" -- python3 -c '
import os, sys, boto3
ref = sys.argv[1]
bucket, key = ref.split(":", 1) if ":" in ref else ("cpg-artifacts", ref)
if bucket != "cpg-artifacts":
    raise ValueError("ref is outside the non-PHI artifact bucket")
s3 = boto3.client("s3",
    endpoint_url=os.getenv("MINIO_ENDPOINT"),
    aws_access_key_id=os.getenv("ARTIFACT_STORE_ACCESS_KEY"),
    aws_secret_access_key=os.getenv("ARTIFACT_STORE_SECRET_KEY"))
sys.stdout.buffer.write(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
' "$template_ref" 2>/dev/null > /tmp/bpmn-template.json

    oc cp /tmp/bpmn-template.json "$ROUTER_POD:/tmp/bpmn-template.json" -n "$NAMESPACE"
    response=$(oc exec "$ROUTER_POD" -n "$NAMESPACE" -- \
        curl -sS -w '\n__HTTP_STATUS__:%{http_code}' --max-time 30 \
        -X POST -H "Host: acp-decision-engine" \
        -H "Content-Type: application/json" \
        --data-binary @/tmp/bpmn-template.json \
        "$automation_url" 2>/dev/null)
    code=$(printf '%s' "$response" | sed -n 's/.*__HTTP_STATUS__:\([0-9][0-9][0-9]\)$/\1/p')
    body=$(printf '%s' "$response" | sed 's/__HTTP_STATUS__:[0-9][0-9][0-9]$//')
    log "  $template_id: HTTP $code"
    if [[ ! "$code" =~ ^2[0-9][0-9]$ ]]; then
        echo "ERROR: Failed to register BPMN template '$template_id' for CPG '$CPG_ID' (HTTP $code)" >&2
        if [ -n "$body" ]; then
            printf '    response: %s\n' "$body" >&2
        fi
        exit 1
    fi
    bpmn_count=$((bpmn_count + 1))
done < "$bpmn_latest_list"

log_step "Published artifacts loaded: 1 guideline, 1 recommendations bundle, $dmn_count DMN models, $bpmn_count automation templates"
