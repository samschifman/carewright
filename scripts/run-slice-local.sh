#!/usr/bin/env bash
# Run the local automation slice from CPG extraction through FHIR approval.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

INGESTER_CLI="$ROOT_DIR/cpg-ingester/.venv/bin/cpg-ingest"
INGESTER_PYTHON="$ROOT_DIR/cpg-ingester/.venv/bin/python"
PDF="$ROOT_DIR/cpg-ingester/data/synthetic-hypertension-cpg-v2.pdf"
PATIENT="$ROOT_DIR/mock-EHR/data/patient-bundle-medication.json"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RESUME_BPMN_RUN_DIR=""
FROM_GOLDEN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --from-golden)
      FROM_GOLDEN=1
      shift
      ;;
    --resume-bpmn-run)
      if [ "$#" -lt 2 ]; then
        echo "--resume-bpmn-run requires a saved cpg-ingester run directory" >&2
        exit 2
      fi
      RESUME_BPMN_RUN_DIR="$(cd "$2" && pwd)"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--from-golden | --resume-bpmn-run RUN_DIR]"
      echo "Without an option, run the complete local automation slice."
      echo "--from-golden skips extraction and runs delivery, care-plan approval, FHIR checks, and UI setup using saved CPG artifacts plus the checked-in automation goldens."
      echo "Set SLICE_GOLDEN_SOURCE_RUN_DIR to select the completed extraction run to replay; otherwise the newest usable run is selected."
      echo "--resume-bpmn-run reruns only BPMN generation from saved run artifacts."
      echo "Set SLICE_SKIP_BUILD=1 to use the existing slice-local service images."
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done
if [ "$FROM_GOLDEN" = "1" ] && [ -n "$RESUME_BPMN_RUN_DIR" ]; then
  echo "--from-golden and --resume-bpmn-run cannot be combined" >&2
  exit 2
fi
if ! command -v podman-compose >/dev/null || ! command -v podman >/dev/null; then
  echo "podman and podman-compose are required" >&2
  exit 1
fi
PROJECT="slice-local"
COMPOSE_SLICE=(-f "$ROOT_DIR/compose.yml" -f "$ROOT_DIR/compose.slice.yml")
RUN_ROOT="$ROOT_DIR/working/slice-run"
BPMN_EVIDENCE_DIR="${SLICE_EVIDENCE_DIR:-bpmn-automation}"
SLICE_EVIDENCE_PATH="${SLICE_EVIDENCE_PATH:-$ROOT_DIR/working/$BPMN_EVIDENCE_DIR/slice-evidence.md}"
if [ -n "$RESUME_BPMN_RUN_DIR" ]; then
  RUN_ID="$(basename "$RESUME_BPMN_RUN_DIR")"
  INGESTER_RUN_DIR="$RESUME_BPMN_RUN_DIR"
elif [ "$FROM_GOLDEN" = "1" ]; then
  INGESTER_RUN_DIR="$RUN_ROOT/from-golden/$RUN_ID"
else
  INGESTER_RUN_DIR="$RUN_ROOT/ingester/$RUN_ID"
fi
SLICE_MLFLOW_EXPERIMENT_NAME="slice-$RUN_ID"
PLAN_ROOT="$RUN_ROOT/careplans"
DELIVERY_DIR="$INGESTER_RUN_DIR/deploy"
ACP_WRITER_URL="${ACP_WRITER_URL:-http://localhost:8082}"
FHIR_URL="${FHIR_URL:-http://localhost:8103/fhir/R4}"
AUTOMATION_URL="${AUTOMATION_URL:-http://localhost:8096}"
MLFLOW_URL="${MLFLOW_URL:-http://localhost:5000}"
SLICE_S3_ACCESS_KEY="${SLICE_S3_ACCESS_KEY:-slice-access}"
SLICE_S3_SECRET_KEY="${SLICE_S3_SECRET_KEY:-slice-secret}"
S3_CONFIG_FILE="$RUN_ROOT/s3-config.json"
export SLICE_S3_ACCESS_KEY SLICE_S3_SECRET_KEY
export SLICE_MLFLOW_EXPERIMENT_NAME
export BPMN_EVIDENCE_DIR
CPG_ID="SYN-HTN-2026-002"
CAREPLAN_SYSTEM="https://github.com/samschifman/cpg-to-acp/fhir/CodeSystem/careplan-id"

REQUIRED_INPUTS=("$INGESTER_PYTHON" "$PATIENT")
if [ "$FROM_GOLDEN" != "1" ]; then
  REQUIRED_INPUTS+=("$INGESTER_CLI" "$PDF")
fi
for required in "${REQUIRED_INPUTS[@]}"; do
  if [ ! -e "$required" ]; then
    echo "Required slice input is missing: $required" >&2
    exit 1
  fi
done

mkdir -p "$INGESTER_RUN_DIR" "$PLAN_ROOT"
if [ "$FROM_GOLDEN" = "1" ]; then
  if [ -n "${SLICE_GOLDEN_SOURCE_RUN_DIR:-}" ]; then
    GOLDEN_SOURCE_RUN_DIR="$(cd "$SLICE_GOLDEN_SOURCE_RUN_DIR" && pwd)"
  else
    GOLDEN_SOURCE_RUN_DIR=""
    for candidate in "$RUN_ROOT"/ingester/*; do
      if [ -s "$candidate/deploy/metadata.json" ] \
        && [ -s "$candidate/deploy/recommendations.json" ] \
        && [ -d "$candidate/deploy/dmn" ] \
        && compgen -G "$candidate/deploy/dmn/*.dmn" >/dev/null; then
        GOLDEN_SOURCE_RUN_DIR="$candidate"
      fi
    done
  fi
  if [ -z "$GOLDEN_SOURCE_RUN_DIR" ] \
    || [ ! -s "$GOLDEN_SOURCE_RUN_DIR/deploy/metadata.json" ] \
    || [ ! -s "$GOLDEN_SOURCE_RUN_DIR/deploy/recommendations.json" ] \
    || ! compgen -G "$GOLDEN_SOURCE_RUN_DIR/deploy/dmn/*.dmn" >/dev/null; then
    echo "--from-golden requires a completed extraction run with metadata, recommendations, and DMNs; set SLICE_GOLDEN_SOURCE_RUN_DIR" >&2
    exit 1
  fi
  cp -R "$GOLDEN_SOURCE_RUN_DIR/." "$INGESTER_RUN_DIR/"
  "$INGESTER_PYTHON" - "$INGESTER_RUN_DIR/run-summary.json" "$RUN_ID" "$GOLDEN_SOURCE_RUN_DIR" <<'PY'
import json
import sys
from pathlib import Path

summary_path = Path(sys.argv[1])
summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
summary["run_id"] = sys.argv[2]
summary["downstream_mode"] = "from-golden"
summary["source_run_dir"] = sys.argv[3]
summary_path.write_text(json.dumps(summary, indent=2) + "\n")
PY
  echo "Replaying downstream from saved CPG artifacts in $GOLDEN_SOURCE_RUN_DIR."
fi
SLICE_S3_ACCESS_KEY="$SLICE_S3_ACCESS_KEY" \
SLICE_S3_SECRET_KEY="$SLICE_S3_SECRET_KEY" \
  "$INGESTER_PYTHON" - "$S3_CONFIG_FILE" <<'PY'
import json
import os
import sys
from pathlib import Path

config = {
    "identities": [{
        "name": "slice-artifact-store",
        "credentials": [{
            "accessKey": os.environ["SLICE_S3_ACCESS_KEY"],
            "secretKey": os.environ["SLICE_S3_SECRET_KEY"],
        }],
        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
    }]
}
Path(sys.argv[1]).write_text(json.dumps(config) + "\n")
PY
export VITE_MOCK_SCENARIO=slice
HOST_SERVICE_PIDS=()
HOST_LOG_DIR="$INGESTER_RUN_DIR/host-services"
SLICE_SUCCESS=0

cleanup_failed_slice() {
  if [ "$SLICE_SUCCESS" != "1" ]; then
    set +u
    echo "SLICE FAIL: run $RUN_ID did not complete; inspect $INGESTER_RUN_DIR and compose logs." >&2
    for pid in "${HOST_SERVICE_PIDS[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
    env -u COMPOSE_PROJECT_NAME podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" down --remove-orphans \
      >"$RUN_ROOT/slice-compose-down-failed.log" 2>&1 || true
  fi
}
trap cleanup_failed_slice EXIT

echo "Stopping the default compose project before starting the isolated slice stack."
if ! env -u COMPOSE_PROJECT_NAME podman-compose down >"$RUN_ROOT/default-compose-down.log" 2>&1; then
  cat "$RUN_ROOT/default-compose-down.log" >&2
  exit 1
fi
if ! podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" down --remove-orphans >"$RUN_ROOT/slice-compose-down.log" 2>&1; then
  cat "$RUN_ROOT/slice-compose-down.log" >&2
  exit 1
fi

echo "Building and starting the isolated compose stack ($PROJECT)."
if [ "${SLICE_SKIP_BUILD:-0}" = "1" ]; then
  for service in litellm kogito automation acp-writer; do
    image="localhost/${PROJECT}_${service}:latest"
    if ! podman image exists "$image"; then
      echo "Required cached slice image is missing: $image" >&2
      exit 1
    fi
  done
  echo "Using existing slice-local service images."
else
  for service in litellm kogito automation acp-writer; do
    echo "Building $service"
    podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" build "$service"
  done
fi
podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" up -d --no-build \
  litellm artifact-store medplum-server medplum-loader acp-writer

wait_for() {
  local url="$1"
  local label="$2"
  local attempt
  echo "Waiting for $label"
  for ((attempt = 1; attempt <= 90; attempt++)); do
    if curl --silent --fail --output /dev/null "$url" 2>/dev/null; then
      return 0
    fi
    sleep 5
  done
  echo "$label did not become healthy at $url" >&2
  return 1
}

wait_for "$ACP_WRITER_URL/health" "acp-writer"
wait_for "$AUTOMATION_URL/health" "automation service"
wait_for "$MLFLOW_URL/health" "MLflow"
wait_for "http://localhost:9333/cluster/status" "S3-compatible artifact store"

if [ -n "$RESUME_BPMN_RUN_DIR" ]; then
  echo "Resuming only the BPMN subgraph from $INGESTER_RUN_DIR."
  if ! ARTIFACT_STORE_URL="http://localhost:9000" \
    ARTIFACT_STORE_ACCESS_KEY="$SLICE_S3_ACCESS_KEY" \
    ARTIFACT_STORE_SECRET_KEY="$SLICE_S3_SECRET_KEY" \
    LITELLM_URL="${SLICE_LITELLM_URL:-http://localhost:4000}" \
    LLM_MODEL="${SLICE_LLM_MODEL:-${LLM_MODEL:-default}}" \
    LLM_MAX_RETRIES="${SLICE_LLM_MAX_RETRIES:-0}" \
    LLM_API_KEY="" \
    MLFLOW_TRACKING_URI="$MLFLOW_URL" \
    MLFLOW_EXPERIMENT_NAME="$SLICE_MLFLOW_EXPERIMENT_NAME" \
      "$INGESTER_PYTHON" -m cpg_ingester.resume_bpmn "$INGESTER_RUN_DIR" \
      2>&1 | tee "$INGESTER_RUN_DIR/bpmn-resume.log"; then
    echo "SLICE FAIL: BPMN resume failed; see $INGESTER_RUN_DIR/bpmn-resume.log" >&2
    exit 1
  fi
elif [ "$FROM_GOLDEN" = "1" ]; then
  echo "Skipping cpg-ingester model calls; the run directory contains copied upstream artifacts."
else
  echo "Running cpg-ingester on the v2 hypertension PDF."
  if ! ARTIFACT_STORE_URL="http://localhost:9000" \
    ARTIFACT_STORE_ACCESS_KEY="$SLICE_S3_ACCESS_KEY" \
    ARTIFACT_STORE_SECRET_KEY="$SLICE_S3_SECRET_KEY" \
    LITELLM_URL="${SLICE_LITELLM_URL:-http://localhost:4000}" \
    LLM_MODEL="${SLICE_LLM_MODEL:-${LLM_MODEL:-default}}" \
    LLM_MAX_RETRIES="${SLICE_LLM_MAX_RETRIES:-0}" \
    LITELLM_API_KEY="sk-change-me" \
    MLFLOW_TRACKING_URI="$MLFLOW_URL" \
    MLFLOW_EXPERIMENT_NAME="$SLICE_MLFLOW_EXPERIMENT_NAME" \
      "$INGESTER_CLI" "$PDF" --output-dir "$INGESTER_RUN_DIR" \
      2>&1 | tee "$INGESTER_RUN_DIR/cpg-ingest.log"; then
    echo "SLICE FAIL: cpg-ingester exited non-zero; see $INGESTER_RUN_DIR/cpg-ingest.log" >&2
    exit 1
  fi
fi

mkdir -p "$HOST_LOG_DIR"
echo "Starting the cpg-ingester BFF and UI locally for the run-directory view."
env \
  CPG_INGESTER_MOCK_RUN_DIR="$INGESTER_RUN_DIR" \
  MLFLOW_TRACKING_URI="$MLFLOW_URL" \
  PYTHONPATH="$ROOT_DIR/cpg-ingester/src:$ROOT_DIR/shared/src" \
  "$INGESTER_PYTHON" -m uvicorn cpg_ingester.services.bff:app --host 0.0.0.0 --port 8095 \
  >"$HOST_LOG_DIR/cpg-ingester-bff.log" 2>&1 < /dev/null &
HOST_SERVICE_PIDS+=("$!")
env VITE_MOCK_SCENARIO=slice npm --prefix "$ROOT_DIR/cpg-ingester/ui" run dev -- --host 0.0.0.0 \
  >"$HOST_LOG_DIR/cpg-ingester-ui.log" 2>&1 < /dev/null &
HOST_SERVICE_PIDS+=("$!")
wait_for "http://localhost:8095/health" "cpg-ingester BFF"
wait_for "http://localhost:3003/" "cpg-ingester UI"

echo "Preparing CPG artifacts for acp-writer."
export SLICE_EVIDENCE_PATH
"$INGESTER_PYTHON" - "$INGESTER_RUN_DIR" "$DELIVERY_DIR" "$CPG_ID" "$FROM_GOLDEN" "$ROOT_DIR/cpg-ingester/data/golden/automation" <<'PY'
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from cpg_contracts import RecommendationBundle
from cpg_contracts.automation.compiler import compile as compile_bpmn
from cpg_contracts.automation.ir import ir_version
from cpg_contracts.automation.templates import AutomationTemplate

run_dir, delivery_dir, expected_cpg = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
from_golden = sys.argv[4] == "1"
golden_template_dir = Path(sys.argv[5])
analysis = json.loads((run_dir / "analysis-result.json").read_text())
generation = json.loads((run_dir / "generation-result.json").read_text())
metadata = analysis.get("cpg_metadata") or {}
if metadata.get("cpg_id") != expected_cpg:
    raise SystemExit(f"Expected CPG {expected_cpg}, received {metadata.get('cpg_id')!r}")
manifest = analysis.get("item_manifest") or []
processes = [item for item in manifest if item.get("type") == "process"]
if not processes:
    raise SystemExit("The extracted manifest contains no identified process")

delivery_dir.mkdir(parents=True, exist_ok=True)
(delivery_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

dmn_dir = delivery_dir / "dmn"
dmn_dir.mkdir(exist_ok=True)
dmn_count = 0
for index, item in enumerate(generation.get("dmn_results") or []):
    if item.get("escalated") or not item.get("dmn_xml"):
        continue
    path = dmn_dir / f"decision-{index + 1}.dmn"
    path.write_text(item["dmn_xml"])
    dmn_count += 1
if from_golden:
    canonical_dmns = {}
    for path in golden_template_dir.parent.glob("*.dmn"):
        try:
            model_id = ET.fromstring(path.read_text()).get("id")
        except ET.ParseError:
            continue
        if model_id:
            canonical_dmns[model_id] = path
    replaced = []
    for path in dmn_dir.glob("*.dmn"):
        try:
            model_id = ET.fromstring(path.read_text()).get("id")
        except ET.ParseError:
            continue
        if model_id in canonical_dmns:
            path.write_text(canonical_dmns[model_id].read_text())
            replaced.append(model_id)
    if not replaced:
        raise SystemExit("No canonical golden DMNs matched the replay bundle")
    print(f"Using canonical golden DMNs for: {', '.join(sorted(replaced))}")

recommendations = [
    item for item in generation.get("recommendation_results") or []
    if isinstance(item, dict) and not item.get("escalated")
]
if not recommendations:
    raise SystemExit("No publishable recommendations were generated")
rec_bundle = RecommendationBundle(
    contract_version=metadata.get("contract_version", "1.1"),
    source_cpg=expected_cpg,
    recommendations=recommendations,
)
(delivery_dir / "recommendations.json").write_text(
    rec_bundle.model_dump_json(indent=2, exclude_none=True) + "\n"
)

template_dir = delivery_dir / "templates"
template_dir.mkdir(exist_ok=True)
templates = []
if from_golden:
    for stale in template_dir.glob("*.template.json"):
        stale.unlink()
    for source in sorted(golden_template_dir.glob("*.template.json")):
        template = AutomationTemplate.model_validate_json(source.read_text())
        if template.summary.source_cpg != expected_cpg:
            raise SystemExit(f"Golden template source CPG mismatch: {source.name}")
        if template.summary.id == "home-bp-monitoring":
            candidates = [
                item for item in rec_bundle.recommendations
                if item.recommendation_type == "monitoring"
                and item.title.strip().casefold()
                == "perform and submit home blood pressure readings"
            ]
            if len(candidates) != 1:
                raise SystemExit(
                    "Expected exactly one recommendation for performing and submitting "
                    "home blood-pressure readings "
                    f"in replay artifacts; found {len(candidates)}"
                )
            recommendation_id = candidates[0].id
            template.summary.linked_recommendation_ids = [recommendation_id]
            for trigger in template.summary.triggers:
                if trigger.kind == "recommendation":
                    trigger.recommendation_id = recommendation_id
            for trigger in template.ir.process.acp.triggers:
                if trigger.kind == "recommendation":
                    trigger.recommendation_id = recommendation_id
            template.summary.version = ir_version(template.ir)
            template.bpmn_xml = compile_bpmn(template.ir)
        target = template_dir / source.name
        target.write_text(template.model_dump_json(indent=2, exclude_none=True) + "\n")
        templates.append(template)
else:
    for result in generation.get("bpmn_results") or []:
        if not isinstance(result, dict) or result.get("escalated"):
            continue
        raw = result.get("automation_template")
        if not raw:
            continue
        template = AutomationTemplate.model_validate(raw)
        if template.summary.source_cpg != expected_cpg:
            continue
        filename = re.sub(r"[^a-zA-Z0-9_-]", "-", template.summary.id)
        target = template_dir / f"{filename}.template.json"
        target.write_text(template.model_dump_json(indent=2, exclude_none=True) + "\n")
        templates.append(template)

home_monitoring = [item for item in templates if item.summary.id == "home-bp-monitoring"]
if not home_monitoring:
    raise SystemExit("No publishable home-bp-monitoring template is available")
if from_golden:
    generation["bpmn_results"] = [
        {
            "artifact_id": f"{expected_cpg}-bpmn-{template.summary.id}",
            "bpmn_xml": template.bpmn_xml,
            "process_ir": template.ir.model_dump(mode="json", exclude_none=True),
            "template_summary": template.summary.model_dump(mode="json", exclude_none=True),
            "item": {
                "id": template.summary.id,
                "type": "process",
                "name": template.summary.name,
                "section": template.summary.section,
            },
            "escalated": False,
        }
        for template in templates
    ]
    (run_dir / "generation-result.json").write_text(json.dumps(generation, indent=2) + "\n")
print(f"Manifest processes: {len(processes)}; DMNs: {dmn_count}; templates: {len(templates)}")
PY

curl --silent --show-error --fail --output "$DELIVERY_DIR/guideline-response.json" \
  -H 'Content-Type: application/json' \
  --data-binary "@$DELIVERY_DIR/metadata.json" \
  "$ACP_WRITER_URL/api/v1/guidelines"
for dmn in "$DELIVERY_DIR"/dmn/*.dmn; do
  [ -f "$dmn" ] || continue
  "$INGESTER_PYTHON" cpg-ingester/src/cpg_ingester/deploy.py \
    --source-cpg "$CPG_ID" "$dmn"
done
curl --silent --show-error --fail --output "$DELIVERY_DIR/recommendations-response.json" \
  -H 'Content-Type: application/json' \
  --data-binary "@$DELIVERY_DIR/recommendations.json" \
  "$ACP_WRITER_URL/api/v1/knowledge/recommendations/batch"
for template in "$DELIVERY_DIR"/templates/*.template.json; do
  [ -f "$template" ] || continue
  "$INGESTER_PYTHON" cpg-ingester/src/cpg_ingester/deploy.py \
    --source-cpg "$CPG_ID" --template "$template"
done

echo "Generating the patient-specific care plan."
CAREPLAN_HTTP_STATUS="$("$INGESTER_PYTHON" - "$PATIENT" "$ACP_WRITER_URL" "$RUN_ROOT" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

patient, writer_url, run_root = sys.argv[1], sys.argv[2], Path(sys.argv[3])
result = subprocess.run(
    [
        "curl", "--silent", "--show-error", "--max-time", "1800",
        "--output", "-", "--write-out", "\\n%{http_code}",
        "-H", "Content-Type: application/fhir+json",
        "--data-binary", f"@{patient}", f"{writer_url}/api/v1/careplans",
    ],
    capture_output=True,
    text=True,
    check=False,
)
body, separator, status = result.stdout.rpartition("\n")
if not separator:
    body, status = result.stdout, "000"
for name in ("OPENAI_API_KEY", "LITELLM_API_KEY", "LLM_API_KEY"):
    secret = os.environ.get(name)
    if secret:
        body = body.replace(secret, "[redacted]")
if status == "201":
    (run_root / "generated-careplan-bundle.json").write_text(body)
else:
    (run_root / "careplan-http-error.json").write_text(body)
print(status)
PY
)"
if [ "$CAREPLAN_HTTP_STATUS" != "201" ]; then
  echo "Care-plan endpoint returned HTTP $CAREPLAN_HTTP_STATUS; sanitized response saved under $RUN_ROOT." >&2
  exit 1
fi
CAREPLAN_ID="$("$INGESTER_PYTHON" - "$RUN_ROOT/generated-careplan-bundle.json" "$CAREPLAN_SYSTEM" <<'PY'
import json
import sys
bundle = json.load(open(sys.argv[1]))
system = sys.argv[2]
careplan = next(
    item["resource"] for item in bundle.get("entry", [])
    if item.get("resource", {}).get("resourceType") == "CarePlan"
)
identifier = next(
    (item.get("value") for item in careplan.get("identifier", []) if item.get("system") == system),
    None,
)
if not identifier:
    raise SystemExit("Generated bundle did not include the stable CarePlan identifier")
if not any(
    ext.get("url", "").endswith("/activity-automation")
    for activity in careplan.get("activity", [])
    for ext in activity.get("extension", [])
):
    raise SystemExit("Generated CarePlan has no activity automation extension")
print(identifier)
PY
)"

CAREPLAN_ARTIFACT_DIR="$PLAN_ROOT/$CAREPLAN_ID"
for expected in planning-brief.json fhir-bundle.json; do
  if [ ! -s "$CAREPLAN_ARTIFACT_DIR/$expected" ]; then
    echo "acp-writer did not persist $CAREPLAN_ARTIFACT_DIR/$expected" >&2
    exit 1
  fi
done

curl --silent --show-error --fail --output "$CAREPLAN_ARTIFACT_DIR/approval-response.json" \
  -X PUT -H 'Content-Type: application/json' \
  --data '{"status":"active","reviewer":{"display":"Slice test clinician"}}' \
  "$ACP_WRITER_URL/api/v1/careplans/$CAREPLAN_ID/status"

echo "Checking the FHIR server transaction result."
curl --silent --show-error --fail --retry 20 --retry-delay 2 \
  --get -H 'Accept: application/fhir+json' \
  --data-urlencode "identifier=$CAREPLAN_SYSTEM|$CAREPLAN_ID" \
  "$FHIR_URL/CarePlan" > "$CAREPLAN_ARTIFACT_DIR/careplan-search.json"
"$INGESTER_PYTHON" - "$CAREPLAN_ARTIFACT_DIR/careplan-search.json" "$CAREPLAN_ARTIFACT_DIR" <<'PY'
import json
import sys
from pathlib import Path

search_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
search = json.loads(search_path.read_text())
careplan = next(
    item.get("resource", {}) for item in search.get("entry", [])
    if item.get("resource", {}).get("resourceType") == "CarePlan"
)
if not careplan:
    raise SystemExit("FHIR search returned no CarePlan")
(out_dir / "careplan-server-resource.json").write_text(json.dumps(careplan, indent=2) + "\n")
references = {}
for activity in careplan.get("activity", []):
    for extension in activity.get("extension", []) or []:
        if not extension.get("url", "").endswith("/activity-automation"):
            continue
        for child in extension.get("extension", []) or []:
            kind = {"instance": "Task", "definition": "DocumentReference"}.get(child.get("url"))
            reference = child.get("valueReference", {}).get("reference")
            if kind and reference:
                references[kind] = reference
if set(references) != {"Task", "DocumentReference"}:
    raise SystemExit("Persisted CarePlan automation links do not include Task and DocumentReference")
(out_dir / "automation-resource-refs.json").write_text(json.dumps(references, indent=2) + "\n")
PY
while IFS= read -r resource_ref; do
  resource_type="${resource_ref%%/*}"
  resource_id="${resource_ref#*/}"
  curl --silent --show-error --fail -H 'Accept: application/fhir+json' \
    "$FHIR_URL/$resource_type/$resource_id" > "$CAREPLAN_ARTIFACT_DIR/$resource_type.json"
done < <("$INGESTER_PYTHON" -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).values()))' "$CAREPLAN_ARTIFACT_DIR/automation-resource-refs.json")

curl --silent --show-error --fail --get \
  --data-urlencode "careplan_id=$CAREPLAN_ID" \
  "$AUTOMATION_URL/api/v1/automations" \
  > "$CAREPLAN_ARTIFACT_DIR/automation-service-records.json"
"$INGESTER_PYTHON" - "$CAREPLAN_ARTIFACT_DIR/automation-service-records.json" <<'PY'
import json
import sys
records = json.load(open(sys.argv[1]))
if not records:
    raise SystemExit("Automation service has no records for the approved CarePlan")
print(f"Automation service stored {len(records)} automation record(s).")
PY

"$INGESTER_PYTHON" acp-writer/scripts/export_ui_fixture.py \
  --brief "$CAREPLAN_ARTIFACT_DIR/planning-brief.json" \
  --bundle "$RUN_ROOT/generated-careplan-bundle.json"

echo "Starting the acp-writer UI locally with the exported slice fixture."
env VITE_MOCK_SCENARIO=slice npm --prefix "$ROOT_DIR/acp-writer/ui" run dev -- --host 0.0.0.0 \
  >"$HOST_LOG_DIR/acp-writer-ui.log" 2>&1 < /dev/null &
HOST_SERVICE_PIDS+=("$!")
wait_for "http://localhost:3001/" "acp-writer UI"

"$INGESTER_PYTHON" - "$INGESTER_RUN_DIR" "$CAREPLAN_ARTIFACT_DIR" "$MLFLOW_URL" "$RUN_ROOT" "$CAREPLAN_ID" "$SLICE_MLFLOW_EXPERIMENT_NAME" <<'PY'
import json
import os
import sys
from pathlib import Path

from cpg_ingester.mlflow_usage import summarize_trace_usage
from mlflow.tracking import MlflowClient

ingester_dir = Path(sys.argv[1])
careplan_dir = Path(sys.argv[2])
tracking_uri = sys.argv[3]
root = Path(sys.argv[4])
careplan_id = sys.argv[5]
experiment_name = sys.argv[6]

analysis = json.loads((ingester_dir / "analysis-result.json").read_text())
run_summary = json.loads((ingester_dir / "run-summary.json").read_text())
templates = [
    json.loads(path.read_text())
    for path in (ingester_dir / "deploy/templates").glob("*.template.json")
]
template = next(item for item in templates if item["summary"]["id"] == "home-bp-monitoring")
careplan = json.loads((careplan_dir / "careplan-server-resource.json").read_text())
task = json.loads((careplan_dir / "Task.json").read_text())
docref = json.loads((careplan_dir / "DocumentReference.json").read_text())
excerpt = {"carePlan": careplan, "task": task, "documentReference": docref}
(careplan_dir / "fhir-automation-excerpt.json").write_text(
    json.dumps(excerpt, indent=2, ensure_ascii=False) + "\n"
)

client = MlflowClient(tracking_uri=tracking_uri)
experiment = client.get_experiment_by_name(experiment_name)
trace_ids = []
all_traces = []
if experiment is not None:
    traces = client.search_traces(
        experiment_ids=[experiment.experiment_id],
        max_results=1000,
        flush=True,
    )
    trace_ids.extend(trace.info.trace_id for trace in traces)
    all_traces.extend(traces)
trace_ids = list(dict.fromkeys(trace_ids))
token_usage = summarize_trace_usage(all_traces) if all_traces else {}
trace_note = ""
if not trace_ids:
    if run_summary.get("downstream_mode") != "from-golden":
        raise SystemExit(f"No MLflow trace IDs were available in {experiment_name}")
    trace_note = (
        f"No traces were returned for the downstream experiment `{experiment_name}`; "
        "the slice acceptance checks below do not depend on token accounting."
    )

manifest_processes = [
    item for item in analysis.get("item_manifest", []) if item.get("type") == "process"
]
record_summaries = json.loads((careplan_dir / "automation-service-records.json").read_text())
records = [
    {key: item.get(key) for key in ("automation_id", "revision", "status", "template_refs")}
    for item in record_summaries
]
evidence = f"""## Downstream slice from golden automation templates

### Result

SLICE PASS

### Run inputs and artifacts

- CPG: `{analysis.get('cpg_metadata', {}).get('cpg_id')}`
- cpg-ingester run directory: `{ingester_dir.relative_to(root)}`
- Replayed upstream run: `{run_summary.get('source_run_dir', 'live extraction')}`
- Mode: `{run_summary.get('downstream_mode', 'full generation')}`; model: `{os.environ.get('SLICE_LLM_MODEL', 'not set')}`
- Identified process items: {len(manifest_processes)}
- Home-monitoring template: `{template['summary']['id']}@{template['summary']['version']}`
- Registered golden templates: {', '.join(f"`{item['summary']['id']}`" for item in templates)}
- Planning brief: `{careplan_dir.relative_to(root)}/planning-brief.json`
- UI fixture: `acp-writer/ui/src/mocks/fixtures/slice-careplan.json`

## FHIR and automation handoff

- CarePlan identifier: `{careplan_id}`
- Persisted resources: `CarePlan`, `Task`, `DocumentReference`
- FHIR excerpt: `{careplan_dir.relative_to(root)}/fhir-automation-excerpt.json`
- Automation service records: `{json.dumps(records, ensure_ascii=False)}`

## MLflow traces

{chr(10).join(f'- `{trace_id}`' for trace_id in trace_ids) if trace_ids else trace_note}

## MLflow token totals by node

```json
{json.dumps(token_usage, indent=2, sort_keys=True)}
```

## UI routes for screenshots

- cpg-ingester: `http://localhost:3003/runs/{run_summary['run_id']}`
- acp-writer: `http://localhost:3001/careplans/slice-careplan-example`
- Screenshot paths: `working/{os.environ['BPMN_EVIDENCE_DIR']}/slice-cpg-ingester.png` and `working/{os.environ['BPMN_EVIDENCE_DIR']}/slice-acp-writer.png`
"""
evidence_path = Path(os.environ["SLICE_EVIDENCE_PATH"])
evidence_path.parent.mkdir(parents=True, exist_ok=True)
if run_summary.get("downstream_mode") == "from-golden":
    existing = evidence_path.read_text() if evidence_path.exists() else "# Local automation slice evidence\n"
    marker = "## Downstream slice from golden automation templates"
    if marker in existing:
        existing = existing[:existing.index(marker)].rstrip()
    existing = existing.replace(
        "**Live extraction remains incomplete; the downstream run is blocked before delivery.**",
        "**Live extraction remains incomplete; the golden downstream slice passed.**",
    )
    outstanding = "## Outstanding"
    if outstanding in existing:
        existing = existing[:existing.index(outstanding)].rstrip() + (
            "\n\n## Outstanding\n\n"
            "Live extraction remains outstanding. The golden downstream slice verifies delivery "
            "using saved CPG artifacts and the checked-in automation goldens."
        )
    evidence_path.write_text(existing.rstrip() + "\n\n" + evidence)
else:
    evidence_path.write_text("# Local automation slice evidence\n\n" + evidence)
print(f"CarePlan {careplan_id}; {len(records)} automation service record(s); {len(trace_ids)} MLflow trace(s).")
PY

SLICE_SUCCESS=1
echo "SLICE PASS"
