#!/usr/bin/env bash
# Run the local automation slice from CPG extraction through FHIR approval.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"
SLICE_TEMPLATE_DIR="${SLICE_TEMPLATE_DIR:-$ROOT_DIR/cpg-ingester/data/golden/automation}"
SLICE_LITELLM_URL_INPUT="${SLICE_LITELLM_URL:-}"
SLICE_LITELLM_URL_INPUT="${SLICE_LITELLM_URL_INPUT%/}"
case "$SLICE_LITELLM_URL_INPUT" in
  "")
    SLICE_DIRECT_LLM=0
    SLICE_LITELLM_URL="http://litellm:4000"
    SLICE_HOST_LITELLM_URL="http://localhost:4000"
    ;;
  http://litellm:4000)
    SLICE_DIRECT_LLM=0
    SLICE_LITELLM_URL="$SLICE_LITELLM_URL_INPUT"
    SLICE_HOST_LITELLM_URL="http://localhost:4000"
    ;;
  http://localhost:4000|http://127.0.0.1:4000)
    SLICE_DIRECT_LLM=0
    SLICE_LITELLM_URL="http://litellm:4000"
    SLICE_HOST_LITELLM_URL="$SLICE_LITELLM_URL_INPUT"
    ;;
  *)
    SLICE_DIRECT_LLM=1
    SLICE_LITELLM_URL="$SLICE_LITELLM_URL_INPUT"
    SLICE_HOST_LITELLM_URL="$SLICE_LITELLM_URL_INPUT"
    ;;
esac

# Load only the Medplum setup values needed by the one-shot loader. In
# particular, do not source the whole secrets file into the loader container.
SLICE_SECRETS_ENV_FILE="${SLICE_SECRETS_ENV_FILE:-$ROOT_DIR/deploy/config/secrets.env}"
if [ -f "$SLICE_SECRETS_ENV_FILE" ]; then
  while IFS= read -r entry || [ -n "$entry" ]; do
    case "$entry" in
      MEDPLUM_ADMIN_EMAIL=*) export MEDPLUM_ADMIN_EMAIL="${entry#*=}" ;;
      MEDPLUM_ADMIN_PASSWORD=*) export MEDPLUM_ADMIN_PASSWORD="${entry#*=}" ;;
      MEDPLUM_PRACTITIONER_PASSWORD=*) export MEDPLUM_PRACTITIONER_PASSWORD="${entry#*=}" ;;
      MEDPLUM_SUPERADMIN_PASSWORD=*) export MEDPLUM_SUPERADMIN_PASSWORD="${entry#*=}" ;;
    esac
  done < "$SLICE_SECRETS_ENV_FILE"
fi
SLICE_LITELLM_ENV_FILE="${SLICE_LITELLM_ENV_FILE:-$ROOT_DIR/platform/litellm/deploy/.env}"
if [ "$SLICE_DIRECT_LLM" = "1" ]; then
  SLICE_LLM_API_KEY="${OPENAI_API_KEY:-}"
elif [ -f "$SLICE_LITELLM_ENV_FILE" ]; then
  while IFS= read -r entry || [ -n "$entry" ]; do
    case "$entry" in
      LITELLM_MASTER_KEY=*) export LITELLM_MASTER_KEY="${entry#*=}" ;;
    esac
  done < "$SLICE_LITELLM_ENV_FILE"
  SLICE_LLM_API_KEY="${LITELLM_MASTER_KEY:-}"
else
  SLICE_LLM_API_KEY="${LITELLM_MASTER_KEY:-}"
fi
export SLICE_LLM_API_KEY SLICE_LITELLM_URL SLICE_HOST_LITELLM_URL

INGESTER_CLI="$ROOT_DIR/cpg-ingester/.venv/bin/cpg-ingest"
INGESTER_PYTHON="$ROOT_DIR/cpg-ingester/.venv/bin/python"
ACP_WRITER_PYTHON="$ROOT_DIR/acp-writer/.venv/bin/python"
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
      echo "--from-golden skips extraction and runs delivery, care-plan approval, FHIR checks, and UI setup using saved CPG artifacts plus *.template.json files from SLICE_TEMPLATE_DIR."
      echo "SLICE_TEMPLATE_DIR defaults to cpg-ingester/data/golden/automation."
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
if [ -z "$SLICE_LLM_API_KEY" ]; then
  if [ "$SLICE_DIRECT_LLM" = "1" ]; then
    echo "Direct LLM mode requires OPENAI_API_KEY." >&2
  else
    echo "Local LLM gateway mode requires its configured gateway key." >&2
  fi
  exit 1
fi
if [ "$FROM_GOLDEN" = "1" ] && [ -n "$RESUME_BPMN_RUN_DIR" ]; then
  echo "--from-golden and --resume-bpmn-run cannot be combined" >&2
  exit 2
fi
if [ "$FROM_GOLDEN" = "1" ] \
  && { [ ! -d "$SLICE_TEMPLATE_DIR" ] || ! compgen -G "$SLICE_TEMPLATE_DIR/*.template.json" >/dev/null; }; then
  echo "--from-golden requires at least one *.template.json in SLICE_TEMPLATE_DIR: $SLICE_TEMPLATE_DIR" >&2
  exit 1
fi
if ! command -v podman-compose >/dev/null || ! command -v podman >/dev/null; then
  echo "podman and podman-compose are required" >&2
  exit 1
fi
if ! podman info >/dev/null 2>&1; then
  echo "Podman preflight failed: Podman is not responding." >&2
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
# FHIR reads performed inside acp-writer must use the Compose service name;
# localhost there points back to the acp-writer container.
FHIR_CONTAINER_URL="${FHIR_CONTAINER_URL:-http://medplum-server:8103/fhir/R4}"
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

REQUIRED_INPUTS=("$INGESTER_PYTHON" "$ACP_WRITER_PYTHON" "$PATIENT")
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
export PYTHONUNBUFFERED=1
echo "Slice runner log: $INGESTER_RUN_DIR/slice-runner.log"
exec >>"$INGESTER_RUN_DIR/slice-runner.log" 2>&1
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

redact_log_stream() {
  "$INGESTER_PYTHON" -c '
import os
import re
import sys
from pathlib import Path

credential_name = re.compile(r"(?:API[_-]?KEY|MASTER[_-]?KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE)
secrets = [
    value for name, value in os.environ.items()
    if credential_name.search(name) and value
]
root = Path(sys.argv[1])
for path in (
    root / "deploy/config/secrets.env",
    root / "platform/litellm/deploy/.env",
    root / ".medplum/medplum-fhir.env",
):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        continue
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if credential_name.search(name) and value:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"\x27":
                value = value[1:-1]
            if value:
                secrets.append(value)

secrets = sorted(set(secrets), key=len, reverse=True)
for line in sys.stdin:
    for secret in secrets:
        line = line.replace(secret, "[redacted]")
    sys.stdout.write(line)
' "$ROOT_DIR"
}

compose_service_names() {
  "$INGESTER_PYTHON" - "$ROOT_DIR/compose.yml" "$ROOT_DIR/compose.slice.yml" <<'PY'
import sys

import yaml

services = set()
for filename in sys.argv[1:]:
    data = yaml.safe_load(open(filename)) or {}
    services.update((data.get("services") or {}).keys())
print("\n".join(sorted(services)))
PY
}

save_compose_logs() {
  local service log_path
  mkdir -p "$RUN_ROOT/compose-logs"
  compose_service_names | while IFS= read -r service; do
    [ -n "$service" ] || continue
    log_path="$RUN_ROOT/compose-logs/$service.log"
    if ! podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" logs "$service" 2>&1 \
      | redact_log_stream > "$log_path"; then
      echo "Compose log capture reported an error for service $service." >> "$log_path"
    fi
  done
}

cleanup_failed_slice() {
  if [ "$SLICE_SUCCESS" != "1" ]; then
    set +u
    echo "SLICE FAIL: run $RUN_ID did not complete; inspect $INGESTER_RUN_DIR and compose logs." >&2
    save_compose_logs
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

if [ "${SLICE_SKIP_BUILD:-0}" != "1" ]; then
  echo "Removing images from the previous slice build."
  for service in litellm kogito automation acp-writer medplum-loader; do
    image="localhost/${PROJECT}_${service}:latest"
    if podman image exists "$image"; then
      if ! podman image rm -f "$image" >/dev/null 2>&1; then
        echo "Could not remove a previous slice build image: $image" >&2
        exit 1
      fi
    fi
  done
fi

echo "Building and starting the isolated compose stack ($PROJECT)."
if [ "${SLICE_SKIP_BUILD:-0}" = "1" ]; then
  for service in litellm kogito automation acp-writer medplum-loader; do
    image="localhost/${PROJECT}_${service}:latest"
    if ! podman image exists "$image"; then
      echo "Required cached slice image is missing: $image" >&2
      exit 1
    fi
  done
  echo "Using existing slice-local service images."
else
  for service in litellm kogito automation acp-writer medplum-loader; do
    echo "Building $service"
    podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" build "$service"
  done
fi

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

podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" up -d --no-build \
  litellm artifact-store medplum-server

wait_for "${FHIR_URL%/fhir/R4}/healthcheck" "Medplum FHIR server"

# The loader creates a project and SMART client, so rerunning it against an
# existing database is not idempotent. Bootstrap only when no writer
# credentials exist, and finish before acp-writer reads the env file.
if [ ! -s "$ROOT_DIR/.medplum/medplum-fhir.env" ]; then
  if [ -z "${MEDPLUM_ADMIN_PASSWORD:-}" ] || [ -z "${MEDPLUM_PRACTITIONER_PASSWORD:-}" ]; then
    echo "Medplum loader requires MEDPLUM_ADMIN_PASSWORD and MEDPLUM_PRACTITIONER_PASSWORD from the local secrets environment." >&2
    exit 1
  fi
  if ! podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" run --rm medplum-loader; then
    echo "Medplum initialization failed; inspect the sanitized loader diagnostics above." >&2
    exit 1
  fi
  if [ ! -s "$ROOT_DIR/.medplum/medplum-fhir.env" ]; then
    echo "Medplum initialization did not create the FHIR client environment file." >&2
    exit 1
  fi
else
  echo "Reusing the existing Medplum client; the authenticated FHIR preflight will verify it."
fi
podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" up -d --no-build acp-writer

wait_for "$ACP_WRITER_URL/health" "acp-writer"
wait_for "$AUTOMATION_URL/health" "automation service"
wait_for "$MLFLOW_URL/health" "MLflow"
wait_for "http://localhost:9333/cluster/status" "S3-compatible artifact store"

echo "Checking authenticated FHIR access from acp-writer."
if ! podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" exec -T acp-writer python - <<'PY'
import json
import logging
import os

logging.disable(logging.CRITICAL)
try:
    from acp_writer.nodes.fhir_server_writer import FHIR_SERVER_URL, _get_auth_headers
    import requests

    headers = _get_auth_headers()
    response = requests.get(
        f"{FHIR_SERVER_URL.rstrip('/')}/metadata",
        headers={"Accept": "application/fhir+json", **headers},
        timeout=10,
    )
    result = {
        "client_id_configured": bool(os.environ.get("FHIR_CLIENT_ID")),
        "client_secret_configured": bool(os.environ.get("FHIR_CLIENT_SECRET")),
        "authorization_header": "Authorization" in headers,
        "metadata_status": response.status_code,
    }
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if response.status_code == 200 else 1)
except Exception as exc:
    print(json.dumps({"preflight_error_type": type(exc).__name__}, sort_keys=True))
    raise SystemExit(1)
PY
then
  echo "Authenticated FHIR preflight failed; stopping before care-plan generation." >&2
  exit 1
fi

echo "Checking LiteLLM gateway authentication and model alias."
if ! podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" exec -T acp-writer python - <<'PY'
import json
import os

import requests

try:
    response = requests.get(
        f"{os.environ['LITELLM_URL'].rstrip('/')}/v1/models",
        headers={"Authorization": f"Bearer {os.environ['LITELLM_API_KEY']}"},
        timeout=10,
    )
    model = os.environ.get("LLM_MODEL", "default")
    available = response.status_code == 200 and any(
        item.get("id") == model for item in response.json().get("data", [])
    )
    print(json.dumps({"models_status": response.status_code, "selected_model_available": available}))
    raise SystemExit(0 if available else 1)
except Exception as exc:
    print(json.dumps({"preflight_error_type": type(exc).__name__}))
    raise SystemExit(1)
PY
then
  echo "LiteLLM gateway preflight failed; stopping before care-plan generation." >&2
  exit 1
fi

if [ -n "$RESUME_BPMN_RUN_DIR" ]; then
  echo "Resuming only the BPMN subgraph from $INGESTER_RUN_DIR."
  if ! ARTIFACT_STORE_URL="http://localhost:9000" \
    ARTIFACT_STORE_ACCESS_KEY="$SLICE_S3_ACCESS_KEY" \
    ARTIFACT_STORE_SECRET_KEY="$SLICE_S3_SECRET_KEY" \
    LITELLM_URL="$SLICE_HOST_LITELLM_URL" \
    LLM_MODEL="${SLICE_LLM_MODEL:-${LLM_MODEL:-default}}" \
    LLM_MAX_RETRIES="${SLICE_LLM_MAX_RETRIES:-0}" \
    LLM_API_KEY="" \
    LITELLM_API_KEY="$SLICE_LLM_API_KEY" \
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
    LITELLM_URL="$SLICE_HOST_LITELLM_URL" \
    LLM_MODEL="${SLICE_LLM_MODEL:-${LLM_MODEL:-default}}" \
    LLM_MAX_RETRIES="${SLICE_LLM_MAX_RETRIES:-0}" \
    LITELLM_API_KEY="$SLICE_LLM_API_KEY" \
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
"$INGESTER_PYTHON" - "$INGESTER_RUN_DIR" "$DELIVERY_DIR" "$CPG_ID" "$FROM_GOLDEN" "$SLICE_TEMPLATE_DIR" <<'PY'
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
canonical_golden_dir = Path("cpg-ingester/data/golden/automation").resolve()
use_canonical_golden_dmns = (
    from_golden and golden_template_dir.resolve() == canonical_golden_dir
)
if use_canonical_golden_dmns:
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

if not templates:
    raise SystemExit("No publishable automation template is available")
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
    # The copied upstream run may contain stale BPMN escalations from the
    # extraction attempt. The checked-in goldens replace those BPMN results;
    # retain independent decision-item escalations only.
    generation["escalated_items"] = [
        item for item in generation.get("escalated_items", [])
        if item.get("type") != "automation"
    ]
    (run_dir / "escalated-items.json").write_text(
        json.dumps(generation["escalated_items"], indent=2) + "\n"
    )
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
CAREPLAN_HTTP_RESULT="$("$INGESTER_PYTHON" - "$PATIENT" "$ACP_WRITER_URL" "$RUN_ROOT" <<'PY'
import os
import json
import re
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
body = re.sub(
    r"(?i)\b(?:OPENAI_API_KEY|LITELLM_API_KEY|LLM_API_KEY)\b\s*[:=]\s*[\"']?[^\s,;\"']+",
    "[REDACTED_CREDENTIAL]",
    body,
)
body = re.sub(r"\bsk-[A-Za-z0-9_-]{20,}\b", "[REDACTED_CREDENTIAL]", body)
if status == "201":
    (run_root / "generated-careplan-bundle.json").write_text(body)
else:
    (run_root / "careplan-http-error.json").write_text(body)
    try:
        error = json.loads(body).get("detail", body)
    except (json.JSONDecodeError, AttributeError):
        error = body
    error = str(error)
    error = re.sub(
        r"(?i)\b(?:OPENAI_API_KEY|LITELLM_API_KEY|LLM_API_KEY)\b\s*[:=]\s*[\"']?[^\s,;\"']+",
        "[REDACTED_CREDENTIAL]",
        error,
    )
    error = re.sub(r"\bsk-[A-Za-z0-9_-]{20,}\b", "[REDACTED_CREDENTIAL]", error)
    for name in ("OPENAI_API_KEY", "LITELLM_API_KEY", "LLM_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            error = error.replace(secret, "[REDACTED_CREDENTIAL]")
    error = " ".join(error.split())[:1000]
    print(status)
    print(error or "Care plan generation failed without an error detail.")
if status == "201":
    print(status)
PY
)"
CAREPLAN_HTTP_STATUS="$(printf '%s\n' "$CAREPLAN_HTTP_RESULT" | sed -n '1p')"
CAREPLAN_HTTP_ERROR="$(printf '%s\n' "$CAREPLAN_HTTP_RESULT" | sed -n '2p')"
if [ "$CAREPLAN_HTTP_STATUS" != "201" ]; then
  echo "SLICE FAIL: care-plan generation returned HTTP $CAREPLAN_HTTP_STATUS: $CAREPLAN_HTTP_ERROR. Sanitized response saved under $RUN_ROOT." >&2
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

GUARD_RESULT="$(PYTHONPATH="$ROOT_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}" "$INGESTER_PYTHON" - \
  "$CAREPLAN_ARTIFACT_DIR/planning-brief.json" \
  "$RUN_ROOT/generated-careplan-bundle.json" \
  "$CAREPLAN_ARTIFACT_DIR/automation-guard.json" <<'PY'
import json
import sys
from pathlib import Path
from slice_automation_guard import write_automation_guard_or_raise

brief_path, bundle_path, result_path = map(Path, sys.argv[1:])
brief = json.loads(brief_path.read_text())
bundle = json.loads(bundle_path.read_text())
result = write_automation_guard_or_raise(brief, bundle, result_path)
print("1" if result["valid_instances"] else "0")
PY
)"
HAS_VALID_AUTOMATION="$(printf '%s\n' "$GUARD_RESULT" | tail -n 1)"
ATTACHED_AUTOMATION_COUNT="$("$INGESTER_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["attached_count"])' "$CAREPLAN_ARTIFACT_DIR/automation-guard.json")"
echo "Attached $ATTACHED_AUTOMATION_COUNT live automation(s); valid instances present: $HAS_VALID_AUTOMATION."

curl --silent --show-error --fail --output "$CAREPLAN_ARTIFACT_DIR/approval-response.json" \
  -X PUT -H 'Content-Type: application/json' \
  --data '{"status":"active","reviewer":{"display":"Slice test clinician"}}' \
  "$ACP_WRITER_URL/api/v1/careplans/$CAREPLAN_ID/status"

"$INGESTER_PYTHON" - "$CAREPLAN_ARTIFACT_DIR/approval-response.json" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1]))
if result.get("delivery_status") != "delivered":
    raise SystemExit(
        "FHIR approval write was not delivered "
        f"(status={result.get('delivery_status')}, http={result.get('fhir_server_status')})"
    )
PY

echo "Checking the FHIR server transaction result."
podman-compose "${COMPOSE_SLICE[@]}" -p "$PROJECT" exec -T acp-writer python - \
  "$CAREPLAN_SYSTEM" "$CAREPLAN_ID" "/slice-run/careplans/$CAREPLAN_ID" "$FHIR_CONTAINER_URL" "$HAS_VALID_AUTOMATION" <<'PY'
import json
import sys
from pathlib import Path
import requests

from acp_writer.nodes.fhir_server_writer import _get_auth_headers

system, careplan_id, output_dir, fhir_url, has_valid_automation = sys.argv[1:]
out_dir = Path(output_dir)
out_dir.mkdir(parents=True, exist_ok=True)
headers = {"Accept": "application/fhir+json", **_get_auth_headers()}
response = requests.get(
    f"{fhir_url.rstrip('/')}/CarePlan",
    params={"identifier": f"{system}|{careplan_id}"},
    headers=headers,
    timeout=30,
)
if response.status_code != 200:
    raise SystemExit(f"Authenticated FHIR CarePlan search returned HTTP {response.status_code}")
search = response.json()
search_path = out_dir / "careplan-search.json"
search_path.write_text(json.dumps(search, indent=2) + "\n")
careplan = next(
    item.get("resource", {}) for item in search.get("entry", [])
    if item.get("resource", {}).get("resourceType") == "CarePlan"
)
if not careplan:
    raise SystemExit("FHIR search returned no CarePlan")
(out_dir / "careplan-server-resource.json").write_text(json.dumps(careplan, indent=2) + "\n")
if has_valid_automation != "1":
    print(json.dumps({
        "careplan_search_status": response.status_code,
        "careplan_found": True,
        "automation_resources_expected": False,
    }))
    raise SystemExit(0)
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
print(json.dumps({"careplan_search_status": response.status_code, "careplan_found": True}))
for resource_type, reference in references.items():
    resource_response = requests.get(f"{fhir_url.rstrip('/')}/{reference}", headers=headers, timeout=30)
    if resource_response.status_code != 200:
        raise SystemExit(
            f"Authenticated FHIR {resource_type} read returned HTTP {resource_response.status_code}"
        )
    resource = resource_response.json()
    if resource.get("resourceType") != resource_type:
        raise SystemExit(f"FHIR {resource_type} read returned the wrong resource type")
    (out_dir / f"{resource_type}.json").write_text(json.dumps(resource, indent=2) + "\n")
    print(json.dumps({"resource_type": resource_type, "status": resource_response.status_code}))
PY

if [ "$HAS_VALID_AUTOMATION" = "1" ]; then
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
else
  printf '[]\n' > "$CAREPLAN_ARTIFACT_DIR/automation-service-records.json"
  echo "No valid automation instance exists; skipping automation-service record checks."
fi

PYTHONPATH="$ROOT_DIR/acp-writer/src:$ROOT_DIR/shared/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$ACP_WRITER_PYTHON" acp-writer/scripts/export_ui_fixture.py \
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
from mlflow.exceptions import RestException
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
brief = json.loads((careplan_dir / "planning-brief.json").read_text())
registered_by_id = {item["summary"]["id"]: item for item in templates}
activity_automation_rows = []
for activity in brief.get("activities", []):
    description = activity.get("description", "")
    normalized = description.casefold() if isinstance(description, str) else ""
    home_bp_activity = (
        ("home blood pressure" in normalized or "home bp" in normalized)
        and ("reading" in normalized or "monitor" in normalized)
    )
    for automation in activity.get("automations", []) or []:
        refs = automation.get("template_refs", []) or []
        missing_refs = [
            ref for ref in refs
            if ref.get("template_id") not in registered_by_id
        ]
        if missing_refs:
            raise SystemExit(
                "Planning brief automation references a template that was not registered: "
                + json.dumps(missing_refs, ensure_ascii=False)
            )
        activity_automation_rows.append({
            "activity_id": activity.get("id"),
            "activity_type": activity.get("type"),
            "activity_description": description,
            "home_bp_monitoring": home_bp_activity,
            "automation_id": automation.get("id"),
            "template_refs": [
                f"{ref.get('template_id')}@{ref.get('version', 'unknown')}"
                for ref in refs
            ],
            "status": (automation.get("validation") or {}).get("status"),
            "errors": (automation.get("validation") or {}).get("errors", []),
        })
home_monitoring_automations = [
    item for item in activity_automation_rows if item["home_bp_monitoring"]
]
careplan = json.loads((careplan_dir / "careplan-server-resource.json").read_text())
excerpt = {"carePlan": careplan}
resource_names = ["CarePlan"]
for filename, key, label in (
    ("Task.json", "task", "Task"),
    ("DocumentReference.json", "documentReference", "DocumentReference"),
):
    resource_path = careplan_dir / filename
    if resource_path.is_file():
        excerpt[key] = json.loads(resource_path.read_text())
        resource_names.append(label)
(careplan_dir / "fhir-automation-excerpt.json").write_text(
    json.dumps(excerpt, indent=2, ensure_ascii=False) + "\n"
)

trace_ids = []
all_traces = []
trace_note = ""
token_usage = {}
try:
    client = MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name(experiment_name)
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
    if not trace_ids:
        if run_summary.get("downstream_mode") != "from-golden":
            raise SystemExit(f"No MLflow trace IDs were available in {experiment_name}")
        trace_note = (
            f"No traces were returned for the downstream experiment `{experiment_name}`; "
            "the slice acceptance checks below do not depend on token accounting."
        )
except RestException as exc:
    trace_note = f"token usage unavailable: {exc}"

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
- Home-BP monitoring activity automations: `{json.dumps(home_monitoring_automations, ensure_ascii=False)}`
- Activities, attached templates, and validation statuses:

```json
{json.dumps(activity_automation_rows, indent=2, ensure_ascii=False)}
```
- Registered templates: {', '.join(f"`{item['summary']['id']}@{item['summary']['version']}`" for item in templates)}
- Planning brief: `{careplan_dir.relative_to(root)}/planning-brief.json`
- UI fixture: `acp-writer/ui/src/mocks/fixtures/slice-careplan.json`

## FHIR and automation handoff

- CarePlan identifier: `{careplan_id}`
- Persisted resources: {', '.join(f'`{name}`' for name in resource_names)}
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
