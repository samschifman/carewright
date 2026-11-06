#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT
NAMESPACE=carewright PROBE_NAMESPACE=carewright-probe ENVIRONMENT=kind UI_ORIGIN=https://writer.example.test \
  tests/security/access-matrix.sh --dry-run >"$temporary_dir/table" 2>"$temporary_dir/commands"
[[ $(grep -c '^| ' "$temporary_dir/table") -ge 15 ]]
! grep -Eq '\| PASS \|' "$temporary_dir/table"
grep -q 'PENDING-A3' "$temporary_dir/table"
grep -q 'MANUAL' "$temporary_dir/table"
grep -q 'carewright-probe' "$temporary_dir/commands"
grep -q '10.0.0.10' "$temporary_dir/commands"
if NAMESPACE=same PROBE_NAMESPACE=same ENVIRONMENT=kind UI_ORIGIN=https://writer.example.test tests/security/access-matrix.sh --dry-run >/dev/null 2>&1; then exit 1; fi
python3 - "$temporary_dir" <<'PY'
import importlib.util,io,json,sys
sys.dont_write_bytecode = True
from contextlib import redirect_stdout
from pathlib import Path
spec=importlib.util.spec_from_file_location('inventory','tests/security/inventory.py')
inventory=importlib.util.module_from_spec(spec);spec.loader.exec_module(inventory)
doc={'items':[
 {'kind':'Service','metadata':{'name':'private'},'spec':{'ports':[{'port':8080}]}},
 {'kind':'Pod','metadata':{'name':'private-pod'},'status':{'podIP':'10.0.0.2'},'spec':{'containers':[{'ports':[{'containerPort':8080}]}]}},
 {'kind':'SandboxRequest','metadata':{'name':'writer'},'status':{'services':[{'url':'http://proxy/writer'}]}},
 {'kind':'Service','metadata':{'name':'ui','labels':{'apps.carewright.io/component':'ui'}},'spec':{'ports':[{'port':80}]}},
]}
output=io.StringIO()
with redirect_stdout(output):inventory.targets(doc,'carewright')
assert 'private.carewright.svc:8080' in output.getvalue()
assert '10.0.0.2:8080' in output.getvalue()
assert 'proxy/writer' in output.getvalue()
assert 'ui.carewright.svc' not in output.getvalue()
with redirect_stdout(io.StringIO()):
 assert inventory.exposure({'items':[{'kind':'Route','spec':{'host':'writer.test'}}]}, {'writer.test'})==0
 assert inventory.exposure({'items':[{'kind':'Route','spec':{'host':'unexpected.test'}}]}, {'writer.test'})==1
 assert inventory.exposure({'items':[{'kind':'Service','metadata':{'name':'database'},'spec':{'type':'NodePort'}}]}, {'writer.test'})==1
 assert inventory.exposure({'items':[]}, {'writer.test'})==1
try:inventory.targets({'items':[]},'carewright')
except ValueError:pass
else:raise AssertionError('Empty inventory cannot pass')
PY
printf 'Access-matrix dry-run and inventory checks passed.\n'
