#!/usr/bin/env bash
# Run the live BPMN generation-quality baseline with the cpg-ingester venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CPG_INGESTER_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PY="$(printenv BENCH_PYTHON || true)"
if [[ -n "$PY" ]]; then
  :
else
  PY="$CPG_INGESTER_DIR/.venv/bin/python"
fi

if [[ ! -x "$PY" ]]; then
  echo "ERROR: Python interpreter not found at $PY" >&2
  echo "Set BENCH_PYTHON to your virtual-environment interpreter." >&2
  exit 1
fi

exec "$PY" "$SCRIPT_DIR/run_benchmark.py" "$@"
