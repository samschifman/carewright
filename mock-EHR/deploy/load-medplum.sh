#!/usr/bin/env bash
# Load patient data and configure a Medplum server for the mock-EHR demo.
#
# This script is environment-agnostic: it takes MEDPLUM_BASE_URL as input
# and works identically in local podman-compose and OpenShift (as a Job).
#
# What it does:
#   1. Waits for the Medplum server to be healthy
#   2. Authenticates with the seeded super admin credentials
#   3. Creates a demo project ("CareView EHR")
#   4. Loads FHIR patient bundles into the project
#   5. Creates practitioner users for the demo
#   6. Registers the acp-writer as a SMART on FHIR ClientApplication

set -euo pipefail

MEDPLUM_BASE_URL="${MEDPLUM_BASE_URL:-http://localhost:8103}"
DATA_DIR="${DATA_DIR:-/data}"
ACP_WRITER_LAUNCH_URI="${ACP_WRITER_LAUNCH_URI:-http://localhost:3001/launch}"
ACP_WRITER_REDIRECT_URI="${ACP_WRITER_REDIRECT_URI:-http://localhost:3001/}"

CODE_CHALLENGE="mock_ehr_setup_challenge"

# --- Helpers ---

log() { echo "[load-medplum] $*"; }

fail() { log "ERROR: $*" >&2; exit 1; }

medplum_post_file() {
  local path="$1"
  local file="$2"
  local token="$3"
  curl -s -X POST "$MEDPLUM_BASE_URL$path" \
    -H "Content-Type: application/fhir+json" \
    -H "Authorization: Bearer $token" \
    --max-time 600 \
    -d @"$file"
}

refresh_token() {
  local login_resp auth_code token_resp
  login_resp=$(curl -sf -X POST "$MEDPLUM_BASE_URL/auth/login" \
    -H "Content-Type: application/json" \
    -d "{
      \"email\": \"$PROJECT_EMAIL\",
      \"password\": \"$PROJECT_PASSWORD\",
      \"codeChallengeMethod\": \"plain\",
      \"codeChallenge\": \"$CODE_CHALLENGE\"
    }")
  auth_code=$(echo "$login_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])" 2>/dev/null) || return 1
  token_resp=$(curl -sf -X POST "$MEDPLUM_BASE_URL/oauth2/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=authorization_code&code=$auth_code&code_verifier=$CODE_CHALLENGE")
  PROJECT_TOKEN=$(echo "$token_resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null) || return 1
}

# --- Step 1: Wait for Medplum server ---

log "Waiting for Medplum server at $MEDPLUM_BASE_URL ..."
retries=0
max_retries=60
until curl -sf "$MEDPLUM_BASE_URL/healthcheck" > /dev/null 2>&1; do
  retries=$((retries + 1))
  if [ "$retries" -ge "$max_retries" ]; then
    fail "Medplum server not ready after $max_retries attempts"
  fi
  sleep 2
done
log "Medplum server is healthy"

# --- Step 1.5: Change seeded super admin password ---
# Medplum creates admin@example.com / medplum_admin on first boot.
# Change it immediately to close the window of known-default access.
MEDPLUM_SUPERADMIN_PASSWORD="${MEDPLUM_SUPERADMIN_PASSWORD:-}"
if [ -n "$MEDPLUM_SUPERADMIN_PASSWORD" ]; then
  log "Changing seeded super admin password ..."
  sa_login=$(curl -sf -X POST "$MEDPLUM_BASE_URL/auth/login" \
    -H "Content-Type: application/json" \
      -d '{"email":"admin@example.com","password":"medplum_admin","codeChallengeMethod":"plain","codeChallenge":"sa"}' 2>/dev/null) || sa_login=""
  sa_code=$(echo "$sa_login" | python3 -c "import sys,json; print(json.load(sys.stdin).get('code',''))" 2>/dev/null) || sa_code=""
  if [ -n "$sa_code" ]; then
    sa_token=$(curl -sf -X POST "$MEDPLUM_BASE_URL/oauth2/token" \
      -H "Content-Type: application/x-www-form-urlencoded" \
      -d "grant_type=authorization_code&code=$sa_code&code_verifier=sa" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null) || sa_token=""
    if [ -n "$sa_token" ]; then
      curl -sf -X POST "$MEDPLUM_BASE_URL/auth/changepassword" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $sa_token" \
        -d "{\"oldPassword\":\"medplum_admin\",\"newPassword\":\"$MEDPLUM_SUPERADMIN_PASSWORD\"}" > /dev/null 2>&1 \
        && log "  Super admin password changed" \
        || log "  WARNING: Failed to change super admin password"
    fi
  else
    log "  Super admin password already changed (default login failed)"
  fi
fi

# --- Step 2-3: Create project and authenticate ---
# Medplum uses a two-step registration flow: /auth/newuser -> /auth/newproject
# This creates a new user, a new project, and returns an auth code in one flow.

PROJECT_EMAIL="${MEDPLUM_ADMIN_EMAIL:-admin@careview.example}"
PROJECT_PASSWORD="${MEDPLUM_ADMIN_PASSWORD:?Set MEDPLUM_ADMIN_PASSWORD environment variable}"
PRACTITIONER_PASSWORD="${MEDPLUM_PRACTITIONER_PASSWORD:?Set MEDPLUM_PRACTITIONER_PASSWORD environment variable}"

log "Creating demo project (CareView EHR) ..."

newuser_response=$(curl -sf -X POST "$MEDPLUM_BASE_URL/auth/newuser" \
  -H "Content-Type: application/json" \
  -d "{
    \"firstName\": \"Admin\",
    \"lastName\": \"CareView\",
    \"email\": \"$PROJECT_EMAIL\",
    \"password\": \"$PROJECT_PASSWORD\",
    \"recaptchaToken\": \"\",
    \"codeChallengeMethod\": \"plain\",
    \"codeChallenge\": \"$CODE_CHALLENGE\"
  }") \
  || fail "Failed to create user"

LOGIN_ID=$(echo "$newuser_response" | python3 -c "import sys,json; print(json.load(sys.stdin)['login'])" 2>/dev/null) \
  || fail "Failed to extract login ID: $newuser_response"

newproject_response=$(curl -sf -X POST "$MEDPLUM_BASE_URL/auth/newproject" \
  -H "Content-Type: application/json" \
  -d "{
    \"login\": \"$LOGIN_ID\",
    \"projectName\": \"CareView EHR\"
  }") \
  || fail "Failed to create project"

auth_code=$(echo "$newproject_response" | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])" 2>/dev/null) \
  || fail "Failed to extract auth code: $newproject_response"

token_response=$(curl -sf -X POST "$MEDPLUM_BASE_URL/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=$auth_code&code_verifier=$CODE_CHALLENGE")

PROJECT_TOKEN=$(echo "$token_response" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null) \
  || fail "Failed to get project token: $token_response"

PROJECT_ID=$(echo "$token_response" | python3 -c "import sys,json; ref=json.load(sys.stdin).get('project',{}).get('reference',''); print(ref.split('/')[-1] if '/' in ref else ref)" 2>/dev/null) \
  || fail "Failed to extract project ID from token response"

log "Created project: CareView EHR ($PROJECT_ID)"
log "Project admin: $PROJECT_EMAIL"

# --- Step 4: Load patient bundles ---

log "Loading patient bundles from $DATA_DIR ..."
bundle_count=0
for bundle_file in "$DATA_DIR"/*.json; do
  [ -f "$bundle_file" ] || continue
  filename=$(basename "$bundle_file")
  log "  Loading $filename ..."
  response=$(medplum_post_file "/fhir/R4" "$bundle_file" "$PROJECT_TOKEN")
  http_type=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('resourceType',''))" 2>/dev/null || echo "unknown")
  if [ "$http_type" = "Bundle" ]; then
    log "  Loaded $filename successfully"
    bundle_count=$((bundle_count + 1))
  else
    log "  WARNING: Unexpected response for $filename: $response"
  fi
done
log "Loaded $bundle_count patient bundle(s)"

# Load Synthea-generated patients if present
if [ -d "$DATA_DIR/synthea" ]; then
  log "Refreshing token for Synthea loading ..."
  refresh_token || log "WARNING: Token refresh failed, using existing token"

  # Load hospital and practitioner bundles first (patient bundles reference them)
  for prefix in hospitalInformation practitionerInformation; do
    for bundle_file in "$DATA_DIR/synthea"/${prefix}*.json; do
      [ -f "$bundle_file" ] || continue
      filename=$(basename "$bundle_file")
      log "  Loading Synthea infrastructure: $filename ..."
      response=$(medplum_post_file "/fhir/R4" "$bundle_file" "$PROJECT_TOKEN")
      http_type=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('resourceType',''))" 2>/dev/null || echo "unknown")
      if [ "$http_type" = "Bundle" ]; then
        log "  Loaded $filename successfully"
      else
        log "  WARNING: Failed to load $filename"
      fi
    done
  done

  synthea_count=0
  for bundle_file in "$DATA_DIR/synthea"/*.json; do
    [ -f "$bundle_file" ] || continue
    filename=$(basename "$bundle_file")
    # Skip infrastructure bundles (already loaded above)
    case "$filename" in hospitalInformation*|practitionerInformation*) continue;; esac
    log "  Loading Synthea: $filename ..."
    response=$(medplum_post_file "/fhir/R4" "$bundle_file" "$PROJECT_TOKEN")
    http_type=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('resourceType',''))" 2>/dev/null || echo "unknown")
    if [ "$http_type" = "Bundle" ]; then
      log "  Loaded $filename successfully"
      synthea_count=$((synthea_count + 1))
    else
      log "  WARNING: Unexpected response for $filename"
    fi
  done
  log "Loaded $synthea_count Synthea patient(s)"
  bundle_count=$((bundle_count + synthea_count))
fi

# --- Step 5: Create practitioner users ---

log "Creating practitioner users ..."

# Dr. Sarah Mitchell — primary demo user
curl -sf -X POST "$MEDPLUM_BASE_URL/admin/projects/$PROJECT_ID/invite" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PROJECT_TOKEN" \
  -d "{
    \"resourceType\": \"Practitioner\",
    \"firstName\": \"Sarah\",
    \"lastName\": \"Mitchell\",
    \"email\": \"sarah.mitchell@careview.example\",
    \"password\": \"$PRACTITIONER_PASSWORD\",
    \"sendEmail\": false,
    \"membership\": { \"admin\": true }
  }" > /dev/null \
  || log "WARNING: Failed to create Dr. Mitchell (may already exist)"

log "  Created Dr. Sarah Mitchell"

# Dr. James Park — secondary demo user
curl -sf -X POST "$MEDPLUM_BASE_URL/admin/projects/$PROJECT_ID/invite" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PROJECT_TOKEN" \
  -d "{
    \"resourceType\": \"Practitioner\",
    \"firstName\": \"James\",
    \"lastName\": \"Park\",
    \"email\": \"james.park@careview.example\",
    \"password\": \"$PRACTITIONER_PASSWORD\",
    \"sendEmail\": false,
    \"membership\": { \"admin\": false }
  }" > /dev/null \
  || log "WARNING: Failed to create Dr. Park (may already exist)"

log "  Created Dr. James Park"

# --- Step 6: Register acp-writer SMART app ---

log "Registering acp-writer SMART app ..."

client_response=$(curl -sf -X POST "$MEDPLUM_BASE_URL/admin/projects/$PROJECT_ID/client" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PROJECT_TOKEN" \
  -d "{
    \"name\": \"ACP Writer\",
    \"description\": \"AI-powered care plan generator (SMART on FHIR app)\",
    \"redirectUri\": \"$ACP_WRITER_REDIRECT_URI\"
  }") \
  || fail "Failed to create ClientApplication"

CLIENT_ID=$(echo "$client_response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
CLIENT_SECRET=$(echo "$client_response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('secret',''))" 2>/dev/null)

# Set the launchUri on the ClientApplication (not supported in the create endpoint).
# Use GET + add field + PUT since JSON PATCH is unreliable on Medplum.
if [ -n "$CLIENT_ID" ]; then
  client_full=$(curl -sf "$MEDPLUM_BASE_URL/fhir/R4/ClientApplication/$CLIENT_ID" \
    -H "Authorization: Bearer $PROJECT_TOKEN")
  client_updated=$(echo "$client_full" | python3 -c "
import sys,json
r=json.load(sys.stdin)
r['launchUri']='$ACP_WRITER_LAUNCH_URI'
print(json.dumps(r))" 2>/dev/null)
  if [ -n "$client_updated" ]; then
    curl -sf -X PUT "$MEDPLUM_BASE_URL/fhir/R4/ClientApplication/$CLIENT_ID" \
      -H "Content-Type: application/fhir+json" \
      -H "Authorization: Bearer $PROJECT_TOKEN" \
      -d "$client_updated" > /dev/null 2>&1 \
      || log "WARNING: Failed to set launchUri"
  fi

  log "  Client ID:     $CLIENT_ID"
  log "  Client Secret: (stored in Secret smart-client-credentials)"
  log "  Launch URI:    $ACP_WRITER_LAUNCH_URI"
  log "  Redirect URI:  $ACP_WRITER_REDIRECT_URI"

  # Write credentials to a K8s Secret (not logs, not files, not ConfigMaps).
  # The loader image has no kubectl, so talk to the API server directly with
  # curl + the ServiceAccount token (RBAC: Role cpg-mock-ehr-loader-secret-writer).
  SA_DIR="/var/run/secrets/kubernetes.io/serviceaccount"
  if [ -f "$SA_DIR/token" ]; then
    K8S_API="https://kubernetes.default.svc"
    K8S_TOKEN=$(cat "$SA_DIR/token")
    K8S_NS=$(cat "$SA_DIR/namespace")
    SECRET_PAYLOAD=$(CLIENT_ID="$CLIENT_ID" CLIENT_SECRET="$CLIENT_SECRET" python3 - <<'PYEOF'
import json, os
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {
        "name": "smart-client-credentials",
        "labels": {
            "app.kubernetes.io/part-of": "cpg-to-acp",
            "app.kubernetes.io/managed-by": "medplum-loader",
        },
    },
    "type": "Opaque",
    "stringData": {
        "smart-config.json": json.dumps({
            "clientId": os.environ["CLIENT_ID"],
            "clientSecret": os.environ["CLIENT_SECRET"],
        })
    },
}))
PYEOF
)
    secret_code=$(echo "$SECRET_PAYLOAD" | curl -s -o /dev/null -w "%{http_code}" \
      --cacert "$SA_DIR/ca.crt" --max-time 15 \
      -H "Authorization: Bearer $K8S_TOKEN" \
      -H "Content-Type: application/json" \
      -X POST "$K8S_API/api/v1/namespaces/$K8S_NS/secrets" \
      -d @- || echo "000")
    if [ "$secret_code" = "409" ]; then
      # Secret exists — replace its data via merge patch
      secret_code=$(echo "$SECRET_PAYLOAD" | curl -s -o /dev/null -w "%{http_code}" \
        --cacert "$SA_DIR/ca.crt" --max-time 15 \
        -H "Authorization: Bearer $K8S_TOKEN" \
        -H "Content-Type: application/merge-patch+json" \
        -X PATCH "$K8S_API/api/v1/namespaces/$K8S_NS/secrets/smart-client-credentials" \
        -d @- || echo "000")
    fi
    case "$secret_code" in
      200|201) log "  Stored SMART credentials in Secret smart-client-credentials" ;;
      *) log "  WARNING: Failed to store SMART credentials in Secret (HTTP $secret_code) — SMART launch will fail" ;;
    esac
    unset SECRET_PAYLOAD K8S_TOKEN
  else
    log "  Not running in-cluster (no ServiceAccount token) — skipping Secret creation"
  fi
fi

log "Registered acp-writer SMART app"

# Legacy file-based config (kept for local dev / compose.yml)
SMART_CONFIG_DIR="${SMART_CONFIG_DIR:-}"
if [ -n "$SMART_CONFIG_DIR" ] && [ -n "$CLIENT_ID" ]; then
  echo "{\"clientId\":\"$CLIENT_ID\",\"clientSecret\":\"$CLIENT_SECRET\"}" > "$SMART_CONFIG_DIR/smart-config.json"
  log "  Wrote SMART config to $SMART_CONFIG_DIR/smart-config.json"
fi

FHIR_ENV_DIR="${FHIR_ENV_DIR:-}"
if [ -n "$FHIR_ENV_DIR" ] && [ -n "$CLIENT_ID" ]; then
  cat > "$FHIR_ENV_DIR/medplum-fhir.env" << ENVEOF
FHIR_CLIENT_ID=$CLIENT_ID
FHIR_CLIENT_SECRET=$CLIENT_SECRET
ENVEOF
  log "  Wrote FHIR credentials to $FHIR_ENV_DIR/medplum-fhir.env"
fi

# --- Done ---

log ""
log "=== Medplum setup complete ==="
log "  Project:      CareView EHR ($PROJECT_ID)"
log "  FHIR endpoint: $MEDPLUM_BASE_URL/fhir/R4"
log "  Patients:     $bundle_count bundle(s) loaded"
log "  Users:        $PROJECT_EMAIL (project admin)"
log "                sarah.mitchell@careview.example (Dr. Mitchell)"
log "                james.park@careview.example (Dr. Park)"
log "  SMART App:    ACP Writer (client_id=$CLIENT_ID)"
log ""
