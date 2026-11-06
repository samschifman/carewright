# Access-matrix probes

Run from an operator workstation with Kubernetes/oc access and `curl`, Python 3 and Bash. Supply existing, distinct application and probe namespaces; the script creates temporary unlabelled curl pods and removes them with `kubectl run --rm`. It does not create or delete namespaces. Probe pod scheduling/image failures fail the row; they cannot prove isolation.

Set `NAMESPACE`, `PROBE_NAMESPACE`, `ENVIRONMENT=openshift|kind`, `UI_ORIGIN` (writer), `INGESTER_UI_ORIGIN`, `INGESTER_CR_NAME`, and comma-separated `EHR_ORIGINS` (public Medplum API/app origins). Set `PROBE_IMAGE` to a published `quay.io/carewright` image, preferably the candidate SHA, containing `sh` and `curl`. Mirror/build that image before running. Set `KUBECTL=oc` if needed.

```bash
NAMESPACE=carewright PROBE_NAMESPACE=carewright-probe ENVIRONMENT=kind \
UI_ORIGIN=https://writer.example.test tests/security/access-matrix.sh --dry-run
```

Normal execution prints a Markdown table and sends per-target diagnostics to stderr. Exit 1 means a failed probe; exit 2 means a missing required input or the pending A3 gateway authentication probe. `DRY-RUN` and `MANUAL` are never claims of successful enforcement. Review the target inventory against `docs/security.md` before accepting results. Service DNS, declared private pod ports and published sandbox URLs are discovered; the script fails if one category is empty. `REQUIRED_TARGET_PATTERNS` is a comma-separated set of expressions for required infrastructure categories; adjust names for the environment without omitting a dependency. External ODF/model endpoints must be added with `EXTRA_TARGETS_FILE` (tab-separated target label and HTTP(S) URL) before claiming full coverage. Include their DNS and pod IP forms wherever applicable.

`GATEWAY_CREATE_URL` must name Track A's actual sandbox-creation API. The expected response is HTTP 401/403; a timeout or 404 does not prove gateway authentication. Leave unset only while A3 is pending; the script returns 2. `NAMESPACE_WIDE_ACCEPTED_TARGETS` can name exact comma-separated inventory labels such as `minio/dns` for a previously documented namespace-wide exception. Every such exception must be recorded in the exposure matrix. Cross-namespace exceptions are never admitted. `PUBLIC_SERVICE_ALLOWLIST` may explicitly name operator-reviewed public services as `service:<name>`; otherwise all NodePort/LoadBalancer services fail exposure enumeration.

The ingester positive test invokes the launch-link script, keeps the bearer URL and cookies in a private temporary directory, and prints only status codes. Never run with shell tracing or copy the link into release evidence. The manual clinician row requires a real EHR login, consent, correct patient scope, bound approval, and logout. Record its result, the emitted table and the candidate image/runtime versions in `working/planning-nov-2026/track-b-status.md` and the release record. Cluster telemetry sentinel verification is separate.

Run `bash scripts/tests/test_access_matrix.sh` for dry-run and inventory checks.


Run the credential gate before workflow probes:

```sh
python3 tests/security/credential-digest-probe.py --namespace <ns> --pod <sb-pod> \
  --secret carewright-service-tokens --key SERVICE_TOKEN_ACP_FHIR_SERVER \
  --env SERVICE_TOKEN_ACP_FHIR_SERVER
```

Repeat for each sandbox receiver/caller and for `APPROVAL_SIGNING_KEY` in the FHIR publication sandbox. Add `--container` for a multi-container pod. Only SHA-256 digests are emitted; errors retain neither the Secret nor command stderr. Mismatch, missing variables and unsupported exec block deployment. See [the decided fallback and runtime prerequisite](../../docs/security.md#deployment-configuration-and-credential-gate).

Local script regression includes `scripts/tests/test_access_matrix_runtime.py` (non-dry-run with a stdin-consuming mock CLI), `test_compose_security.py` (caller/receiver and key-scope agreement), and `test_credential_digest_probe.py` (match, mismatch and safe failures). These are script tests, not cluster boundary evidence.
