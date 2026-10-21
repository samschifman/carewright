# Automation service

This service receives approved care-plan automations, validates their BPMN and FHIR resource shapes, and stores the publication payload under the PHI data boundary. It does not execute BPMN.

## API

- `GET /health` — liveness check.
- `POST /api/v1/automations` — accept a `PublicationPayload` with `Idempotency-Key` equal to `job_id`; successful first delivery returns `201`, an identical replay returns `200`, and reusing the key with different content returns `409`.
- `GET /api/v1/automations?careplan_id=...` — list stored automation records for a care plan.
- `GET /api/v1/automations/{careplan_id}/{automation_id}` — retrieve a stored automation, latest revision by default; pass `revision` to retrieve a pinned revision.
- `POST /api/v1/automations/{careplan_id}/withdraw` — persist an idempotent withdrawal marker.

Both POST routes require `X-Automation-Token` matching `AUTOMATION_SERVICE_TOKEN`. A missing server token returns `503`; a missing or incorrect request token returns `401`. Health and read-only list/get routes remain unauthenticated, so keep this service on a trusted local network and do not expose it to an untrusted shared network. The Compose default `carewright-local-automation-dev-only` is a non-secret local-development value, not a cluster credential.

On receipt, the service decodes the BPMN attachment in the DocumentReference, reruns L1–L4, and checks required Task and DocumentReference fields before storing anything. The JSON record and BPMN XML are written to `cpg-phi:automations/{careplan_id}/{automation_id}/{revision}.json` and `.bpmn` when the PHI store is configured.

## Local development

Without `ARTIFACT_STORE_URL`, records use `AUTOMATION_STORE_DIR` (default `data/automations`). Start the service with:

```bash
cd automation
pip install -e "../shared" -e ".[dev]"
AUTOMATION_STORE_DIR=/tmp/automation-store uvicorn automation_service.app:app --reload
```

Run its offline suite from the repository root with `PYTHONPATH=shared/src:automation/src python -m pytest automation/tests -q`.
