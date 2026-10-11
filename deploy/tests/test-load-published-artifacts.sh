#!/usr/bin/env bash
# Cluster-free tests for the published-artifact loader.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/deploy/load-published-artifacts.sh"
TEST_ROOT="$(mktemp -d)"
FAKE_BIN="$TEST_ROOT/bin"
TEST_STATE_DIR="$TEST_ROOT/state"
TEST_CONFIG="$TEST_ROOT/cluster.env"
TEST_OUTPUT="$TEST_ROOT/output.log"

mkdir -p "$FAKE_BIN" "$TEST_STATE_DIR"
trap 'rm -rf "$TEST_ROOT"' EXIT

cat > "$FAKE_BIN/oc" <<'OC'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
    whoami)
        echo test-user
        exit 0
        ;;
    project)
        echo "${NAMESPACE:-loader-test}"
        exit 0
        ;;
    get)
        for arg in "$@"; do
            case "$arg" in
                app=openshell-router) echo pod/router-pod; exit 0;;
                app.kubernetes.io/name=cpg-ingester-bff) echo pod/bff-pod; exit 0;;
            esac
        done
        exit 1
        ;;
    cp)
        exit 0
        ;;
    exec)
        while [[ $# -gt 0 && "$1" != "--" ]]; do shift; done
        [[ $# -gt 0 ]] || exit 1
        shift
        exec_command="${1:-}"
        shift || true

        if [[ "$exec_command" == "python3" ]]; then
            [[ "${1:-}" == "-c" ]] && shift
            python_source="${1:-}"
            case "$python_source" in
                *metadata.json*) printf '{"cpg_id":"fixture"}\n';;
                *recommendations.json*) printf '{"recommendations":[]}\n';;
                *list_objects_v2*)
                    if [[ "$python_source" == *"/bpmn/"* ]]; then
                        printf '%s\n' \
                            '{"latest_key":"published/fixture/bpmn/home-bp-monitoring/latest.json","template_ref":"cpg-artifacts:published/fixture/bpmn/home-bp-monitoring/499a6a2c1b81.template.json","template_id":"home-bp-monitoring"}'
                    else
                        printf '%s\n' \
                            '{"key":"published/fixture/dmn/model-one.dmn","name":"model-one"}' \
                            '{"key":"published/fixture/dmn/model-two.dmn","name":"model-two"}'
                    fi
                    ;;
                *latest.json*) printf '{"template_id":"home-bp-monitoring","version":"499a6a2c1b81","template_ref":"cpg-artifacts:published/fixture/bpmn/home-bp-monitoring/499a6a2c1b81.template.json"}\n';;
                *template_ref*) printf '{"contract_version":"1.1"}\n';;
                *) printf '<definitions/>\n';;
            esac
            exit 0
        fi

        if [[ "$exec_command" == "curl" ]]; then
            if [[ -f "$TEST_STATE_DIR/curl-calls" ]]; then
                read -r call_index < "$TEST_STATE_DIR/curl-calls"
            else
                call_index=0
            fi
            printf '%s\n' "$((call_index + 1))" > "$TEST_STATE_DIR/curl-calls"

            IFS=',' read -r -a response_codes <<< "${TEST_CODES:-201,201,201,201}"
            response_code="${response_codes[$call_index]:-201}"
            status_marker=false
            automation_request=false
            for arg in "$@"; do
                [[ "$arg" == *__HTTP_STATUS__* ]] && status_marker=true
                [[ "$arg" == http://localhost:* ]] && printf '%s\n' "$arg" >> "$TEST_URL_LOG"
                [[ "$arg" == *"/api/v1/automation/templates"* ]] && automation_request=true
            done

            case "$response_code" in
                409) response_body='{"error":"Decision model id already belongs to another source CPG","existing_source_cpg":"CPG-X","source_cpg":"CPG-Y"}';;
                422)
                    if [ "$automation_request" = true ]; then
                        response_body='{"error":"BPMN registration failed","messages":[{"severity":"ERROR","text":"rejected BPMN"}]}'
                    else
                        response_body='{"error":"DMN engine validation failed","messages":[{"severity":"ERROR","text":"rejected DMN"}]}'
                    fi
                    ;;
                500) response_body='{"error":"server down"}';;
                *) response_body='{"ok":true}';;
            esac

            if [ "$status_marker" = true ]; then
                printf '%s\n__HTTP_STATUS__:%s' "$response_body" "$response_code"
            else
                printf '%s' "$response_code"
            fi
            exit 0
        fi
        echo "unexpected fake oc exec command: $exec_command" >&2
        exit 1
        ;;
    *)
        echo "unexpected fake oc command: $*" >&2
        exit 1
        ;;
esac
OC

cat > "$FAKE_BIN/helm" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

cat > "$FAKE_BIN/envsubst" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

chmod +x "$FAKE_BIN/oc" "$FAKE_BIN/helm" "$FAKE_BIN/envsubst"

cat > "$TEST_CONFIG" <<'CONFIG'
NAMESPACE="loader-test"
MAAS_GATEWAY_URL="http://unused.example"
LLM_MODEL_DEFAULT="unused-model"
GIT_REPO="https://unused.example/repo.git"
CLUSTER_DOMAIN="unused.example"
CONFIG

export PATH="$FAKE_BIN:$PATH"
export TEST_STATE_DIR
export TEST_URL_LOG="$TEST_ROOT/request-urls.log"

assert_contains() {
    local expected="$1"
    local file="$2"
    if ! grep -Fq -- "$expected" "$file"; then
        echo "FAIL: expected '$expected' in $file" >&2
        cat "$file" >&2
        exit 1
    fi
}

assert_not_contains() {
    local unexpected="$1"
    local file="$2"
    if grep -Fq -- "$unexpected" "$file"; then
        echo "FAIL: did not expect '$unexpected' in $file" >&2
        cat "$file" >&2
        exit 1
    fi
}

run_loader() {
    local codes="$1"
    shift
    rm -f "$TEST_STATE_DIR/curl-calls" "$TEST_URL_LOG"
    export TEST_CODES="$codes"
    if "$SCRIPT_PATH" --config "$TEST_CONFIG" "$@" > "$TEST_OUTPUT" 2>&1; then
        result_status=0
    else
        result_status=$?
    fi
}

run_loader '201,201,201,201' 'CPG-COUNT'
[[ "$result_status" -eq 0 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains 'Published artifacts loaded: 1 guideline, 1 recommendations bundle, 2 DMN models, 1 automation templates' "$TEST_OUTPUT"
assert_contains 'home-bp-monitoring: HTTP 201' "$TEST_OUTPUT"
echo 'PASS: successful uploads report DMN models and the latest BPMN template'

run_loader '201,201,409' 'CPG-COLLISION'
[[ "$result_status" -eq 1 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains 'Failed to deploy DMN model' "$TEST_OUTPUT"
assert_contains 'Decision model id already belongs to another source CPG' "$TEST_OUTPUT"
assert_not_contains '&replace=true' "$TEST_URL_LOG"
echo 'PASS: a DMN source collision fails without --replace and prints the conflict'

run_loader '201,201,201,201' --replace 'A/B C&D'
[[ "$result_status" -eq 0 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains 'http://localhost:8080/api/v1/decisions/models?source_cpg=A%2FB%20C%26D&replace=true' "$TEST_URL_LOG"
assert_contains 'http://localhost:8080/api/v1/automation/templates?source_cpg=A%2FB%20C%26D&replace=true' "$TEST_URL_LOG"
echo 'PASS: CPG ids are URL-encoded and --replace is sent to the decision engine'

run_loader '201,201,422' 'CPG-INVALID'
[[ "$result_status" -eq 1 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains 'rejected DMN' "$TEST_OUTPUT"
echo 'PASS: validation failures stop loading and print the engine message'

run_loader '201,201,201,201,422' 'CPG-BPMN-INVALID'
[[ "$result_status" -eq 1 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains 'Failed to register BPMN template' "$TEST_OUTPUT"
assert_contains 'rejected BPMN' "$TEST_OUTPUT"
echo 'PASS: BPMN template validation failures stop loading and print the engine message'

run_loader '201,201,500' 'CPG-ERROR'
[[ "$result_status" -eq 1 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains '{"error":"server down"}' "$TEST_OUTPUT"
echo 'PASS: unexpected DMN server errors stop loading and print the response body'

run_loader '500' 'CPG-GUIDELINE-ERROR'
[[ "$result_status" -eq 1 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains 'Guideline registration failed with HTTP 500' "$TEST_OUTPUT"
echo 'PASS: guideline registration errors stop loading'

run_loader '201,500' 'CPG-RECOMMENDATION-ERROR'
[[ "$result_status" -eq 1 ]] || { cat "$TEST_OUTPUT" >&2; exit 1; }
assert_contains 'Recommendations ingest failed with HTTP 500' "$TEST_OUTPUT"
echo 'PASS: recommendation ingestion errors stop loading'
