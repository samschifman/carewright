#!/usr/bin/env bash
# The service token stays in the BFF pod. The output is a short-lived bearer URL.
set -euo pipefail
if [[ $# != 2 ]]; then echo "Usage: $0 <namespace> <cpgingester-cr-name>" >&2; exit 2; fi
cli=${KUBECTL:-kubectl}
namespace=$1
name=$2
origin=$($cli -n "$namespace" get cpgingester "$name" -o jsonpath='{.status.endpoints.ui}')
[[ "$origin" == https://* || "$origin" == http://localhost:* ]] || { echo 'Missing or unsupported UI origin' >&2; exit 1; }
path=$($cli -n "$namespace" exec "deployment/$name-bff" -- python3 -c '
import json, os, urllib.request
request = urllib.request.Request("http://127.0.0.1:8080/api/v1/admin/launch-links", data=b"{}", headers={"X-Service-Token":os.environ["SERVICE_TOKEN_CPG_BFF"],"Content-Type":"application/json"})
with urllib.request.urlopen(request,timeout=10) as response:
    print(json.load(response)["url"])
')
[[ "$path" == /api/v1/launch/* && "$path" != *$'\n'* ]] || { echo 'Invalid launch-link response' >&2; exit 1; }
printf '%s%s\n' "${origin%/}" "$path"
