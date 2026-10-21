#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
SCRIPT_PATH="$SCRIPT_DIR/run-slice-local.sh"
TEST_ROOT="$(mktemp -d)"
FAKE_BIN="$TEST_ROOT/bin"
mkdir -p "$FAKE_BIN"
trap 'rm -rf "$TEST_ROOT"' EXIT

cat > "$FAKE_BIN/podman-compose" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
[[ "$#" -eq 5 && "$1" == "-f" && "$3" == "-f" && "$5" == "config" ]]
[[ "$2" == "$ROOT_DIR/compose.yml" ]]
[[ "$4" == "$ROOT_DIR/compose.slice.yml" ]]
STUB
chmod +x "$FAKE_BIN/podman-compose"

compose_assignment="$(sed -n 's/^COMPOSE_SLICE=/COMPOSE_SLICE=/p' "$SCRIPT_PATH")"
[[ -n "$compose_assignment" ]]
export ROOT_DIR
eval "$compose_assignment"
PATH="$FAKE_BIN:$PATH" podman-compose "${COMPOSE_SLICE[@]}" config

if grep -Fq -- '8096:8080' "$ROOT_DIR/compose.yml"; then
  echo 'FAIL: base Compose publishes the automation port' >&2
  exit 1
fi
port_count="$(grep -Fc -- '8096:8080' "$ROOT_DIR/compose.slice.yml" || true)"
[[ "$port_count" == "1" ]]
if ! grep -Fq -- '      - "127.0.0.1:8096:8080"' "$ROOT_DIR/compose.slice.yml"; then
  echo 'FAIL: slice Compose does not bind the automation port to loopback' >&2
  exit 1
fi

token_expression='AUTOMATION_SERVICE_TOKEN=${AUTOMATION_SERVICE_TOKEN:-carewright-local-automation-dev-only}'
base_token_count="$(grep -Fc -- "$token_expression" "$ROOT_DIR/compose.yml" || true)"
[[ "$base_token_count" == "2" ]]
grep -Fq -- "AUTOMATION_SERVICE_TOKEN: ${token_expression#AUTOMATION_SERVICE_TOKEN=}" \
  "$ROOT_DIR/compose.orchestrated.yml"

echo 'PASS: the default slice overlay is selected, token wiring matches, and host access is loopback-only'
