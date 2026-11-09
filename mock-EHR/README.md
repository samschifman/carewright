# Mock Electronic Health Record

A mock EHR built on [Medplum](https://www.medplum.com/) that provides a clinical-feeling environment for demoing the acp-writer SMART on FHIR app.

## Architecture

The mock-EHR uses Medplum as the FHIR server, replacing the previous HAPI FHIR setup. Medplum provides FHIR R4, OAuth, and SMART on FHIR App Launch out of the box.

| Component | Image | Port | Purpose |
|---|---|---|---|
| PostgreSQL | `postgres:16` | 5432 | Medplum data store |
| Redis | `redis:7` | 6379 | Medplum cache/queue |
| Medplum Server | `medplum/medplum-server:5.1.27` | 8103 | FHIR R4 + OAuth + SMART |
| Mock-EHR App | (Phase 2) | 3000 | Clinical EHR UI |

## Getting Started

Start the Medplum stack from the repository root:

```bash
podman-compose up -d medplum-postgres medplum-redis medplum-server
```

Wait for the server to be healthy, then run the data loader:

```bash
podman-compose up medplum-loader
```

Or start everything at once (loader waits for server automatically):

```bash
podman-compose up
```

## Patient Data

Hand-crafted FHIR Transaction Bundles in `data/`:

| Bundle | Patient | Scenario | Expected DMN Path |
|---|---|---|---|
| `patient-bundle-medication.json` | James Reynolds, 55yo M | Hypertension (BP 142/92) + Type 2 Diabetes, on Metformin | `start_medication` |
| `patient-bundle-lifestyle.json` | Maria Chen, 45yo F | Hypertension (BP 125/80), no comorbidities | `lifestyle_only` |

## Data Loading

The `deploy/load-medplum.sh` script automates the full Medplum setup:

1. Waits for the Medplum server to be healthy
2. Changes the seeded super admin password (if `MEDPLUM_SUPERADMIN_PASSWORD` is set)
3. Creates a "CareView EHR" project
4. Loads all patient bundles from `data/`
5. Creates demo practitioner users
6. Registers the acp-writer as a SMART on FHIR app

The script is environment-agnostic — it takes `MEDPLUM_BASE_URL` as input and works in both local podman-compose and OpenShift (as a Job).

### Required environment variables

| Variable | Purpose |
|---|---|
| `MEDPLUM_ADMIN_PASSWORD` | Password for the CareView EHR project admin |
| `MEDPLUM_PRACTITIONER_PASSWORD` | Password for demo practitioner users (Dr. Mitchell, Dr. Park) |
| `MEDPLUM_SUPERADMIN_PASSWORD` | New password for the Medplum seeded super admin (optional, recommended) |
| `MEDPLUM_ADMIN_EMAIL` | Admin email (default: `admin@careview.example`) |

On OpenShift, these come from the `medplum-user-credentials` Kubernetes Secret. For local dev, set them as environment variables or in a `.env` file.

## Demo Users

After loading:

| User | Email | Role |
|---|---|---|
| Dr. Sarah Mitchell | `sarah.mitchell@careview.example` | Primary demo practitioner |
| Dr. James Park | `james.park@careview.example` | Secondary demo practitioner |

Passwords are configured via environment variables — see above.

## Verifying

After the data is loaded:

```bash
# Authenticate (get a token)
CODE=$(curl -sf -X POST http://localhost:8103/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$MEDPLUM_ADMIN_EMAIL\",\"password\":\"$MEDPLUM_ADMIN_PASSWORD\",\"codeChallengeMethod\":\"plain\",\"codeChallenge\":\"verify\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])")

TOKEN=$(curl -sf -X POST http://localhost:8103/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=$CODE&code_verifier=verify" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# List patients
curl -sf http://localhost:8103/fhir/R4/Patient \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# Patient 1 conditions (hypertension + diabetes)
curl -sf "http://localhost:8103/fhir/R4/Condition?patient=patient-1" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# Patient 1 blood pressure (142/92)
curl -sf "http://localhost:8103/fhir/R4/Observation?patient=patient-1&category=vital-signs" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# Patient IPS (International Patient Summary)
curl -sf "http://localhost:8103/fhir/R4/Patient/patient-1/\$summary" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

## SMART on FHIR

The loader registers acp-writer with `launchUri=<writer-origin>/api/v1/launch` and `redirectUri=<writer-origin>/api/v1/launch/callback`.

1. Sign in to the mock-EHR and select a patient.
2. Click **Generate Care Plan**. The writer BFF validates the issuer, creates one-time state/PKCE and redirects to Medplum.
3. The EHR UI's `/oauth` route uses Medplum's login and scope-consent components. Medplum validates the registered client redirect URI.
4. The BFF exchanges the code and reads the IPS server-side, then discards the access token and creates a patient-scoped HttpOnly session.
5. Review the exact generated artifact. Signed approval binding controls publication back to FHIR.

### SMART client credentials

The loader stores `SMART_CLIENT_ID` and `SMART_CLIENT_SECRET` in Kubernetes Secret `smart-client-credentials`. The writer BFF consumes them through individual `secretKeyRef` environment entries. No browser or UI document root receives the secret; `/smart-config.json` returns 404. The deprecated JSON Secret key is retained only for migration and must never be mounted into a UI. Local `SMART_CONFIG_DIR` writes a mode-600 `smart-client.env` for the backend, not browser configuration.

The loader's ServiceAccount is limited to the named Secret and does not print client credentials. After registration or rotation restart the writer BFF. Use `launch openid fhirUser patient/*.read user/Practitioner.read` with Medplum 5.1.27: the summary also needs access to its practitioner author. That provider's PKCE token branch does not enforce the confidential client secret; PKCE and single-use codes remain required. See [security](../docs/security.md).

## Cluster Deployment

See [`deploy/README.md`](../deploy/README.md) for the full cluster deployment guide.

### Quick reference

```bash
# Full deploy (builds + Helm + loader job + SMART credential wiring)
mock-EHR/deploy/deploy.sh --config deploy/config/cluster.env

# Redeploy without rebuilding images
mock-EHR/deploy/deploy.sh --skip-build --tag <sha> --config deploy/config/cluster.env

# Verify
mock-EHR/deploy/verify.sh --config deploy/config/cluster.env

# Teardown (preserves Secrets and ImageStreams)
mock-EHR/deploy/teardown.sh --config deploy/config/cluster.env
```

The deploy script:
1. Builds/pushes Docker Hub images to the internal registry (postgres, redis, medplum) to avoid rate limits
2. Builds custom images (mock-ehr-app, ips-viewer, medplum-loader)
3. Installs the Helm chart with all image tags and namespace overrides
4. If `medplum-user-credentials` Secret exists, enables the loader job — which seeds patient data, creates demo users, and registers the SMART app (writing credentials to the `smart-client-credentials` Secret)
5. Restarts the IPS Viewer to mount the SMART credentials
6. Deploys the mock-ehr-mcp server and registration

### Known issue: Docker Hub rate limits

`mock-EHR/ui/Containerfile` and `mock-EHR/ips-viewer/Containerfile` still pull base images from Docker Hub (`node:22-alpine`, `nginxinc/nginx-unprivileged:alpine`). On shared cluster egress IPs these can hit anonymous pull rate limits. cpg-ingester's UI has been migrated to UBI base images; mock-EHR migration is pending.

## Version Pinning

All Medplum components are pinned to version **5.1.27**. See `dev_docs/ui/mock-ehr-design.md` for the version pinning rationale and upgrade process.

The OAuth UI must be built with `VITE_SMART_REDIRECT_ORIGINS` set to the exact writer UI origin (comma-separated for multiple development writers). The Containerfile accepts the same build argument; an empty value refuses authorization. Only the fixed `/api/v1/launch/callback` path is accepted. See [security](../docs/security.md) for the Medplum login-API probe and deployment requirements.

For OpenShift source builds, export `VITE_SMART_REDIRECT_ORIGINS=https://<writer-ui-route>` before running `setup-openshift.sh` or `deploy.sh`; the BuildConfig passes it as a build argument. Helm deploys the compiled image and cannot change its allowlist.
