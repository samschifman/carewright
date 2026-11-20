"""The clinician who approves a plan can open it; nobody else's session can.

These tests use output from the real bundle builder and the real approved
write path. The builder's CarePlan subject is a bundle-local ``urn:uuid:``
reference, so access scoping must come from the signed approval record
(EHR patient and issuer), never from the bundle.
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import acp_writer.nodes.fhir_server_writer as writer_module
import acp_writer.services.automation_publisher as automation_publisher_module
from acp_writer.nodes.fhir_server_writer import (
    fhir_server_writer,
    get_care_plan,
    list_care_plans,
    rehydrate_care_plans_from_jobs,
)
from acp_writer.planning_brief import PlanGoal, PlanningBrief
from acp_writer.services import bff
from acp_writer.services.approval_binding import write_approved_bundle
from acp_writer.services.automation_publisher import AutomationPublisher
from acp_writer.services.job_store import JobStore
from acp_writer.services.reviewer import default_reviewer
from acp_writer.validators.fhir_bundle_builder import build_fhir_bundle
from cpg_contracts.approvals import artifact_revision, sign_record
from cpg_contracts.web_session import MemorySessionStore, SessionManager

PATIENT = 'ehr-patient-7'
ISSUER = 'https://ehr.example/fhir/R4/'
OTHER_ISSUER = 'https://another-ehr.example/fhir/R4/'
CAREPLAN_ID = 'workflow-7'
SIGNING_KEY = 'test-signing-key'


def _real_bundle() -> dict:
    brief = PlanningBrief(
        patient_reference='Patient/' + PATIENT,
        applicable_cpgs=['SYN-CPG-1'],
        goals=[PlanGoal(description='Synthetic goal', source_recommendation_id='rec-1', source_cpg='SYN-CPG-1')],
        activities=[],
    )
    return build_fhir_bundle(brief, patient_demographics={'id': PATIENT, 'name': 'Synthetic Patient'}, careplan_id=CAREPLAN_ID)


class _PhiStore:
    def __init__(self, bundle, record):
        self.objects = {'run-7/fhir_bundle.json': bundle, 'run-7/approvals/0.json': record}

    def get_raw_in_bucket(self, bucket, key):
        assert bucket == 'cpg-phi'
        return json.dumps(self.objects[key]).encode()


@pytest.fixture
def published(monkeypatch, tmp_path):
    """Publish one real bundle through the approved write path; return a helper."""
    writer_module._care_plans.clear()
    monkeypatch.setenv('APPROVAL_SIGNING_KEY', SIGNING_KEY)
    monkeypatch.setattr(
        automation_publisher_module, '_publisher',
        AutomationPublisher(JobStore(directory=tmp_path), automation_url='http://automation', automation_token='test-token'),
    )

    def fake_post(url, *, json=None, headers=None, timeout=None, **_kwargs):
        entries = [
            {'response': {'location': f"{e['resource']['resourceType']}/server-{i}/_history/1"}}
            for i, e in enumerate((json or {}).get('entry', []))
        ]
        return SimpleNamespace(status_code=201, content=b'{}', text='', json=lambda: {'entry': entries})

    monkeypatch.setattr(writer_module.requests, 'post', fake_post)

    def publish(**record_fields):
        bundle = _real_bundle()
        record = sign_record({
            'runId': 'run-7', 'workflowInstanceId': CAREPLAN_ID, 'reviewRound': 0,
            'fhirBundleRef': 'cpg-phi:run-7/fhir_bundle.json', 'artifactRevision': artifact_revision(bundle),
            'decision': 'approve', 'reviewer': default_reviewer().model_dump(),
            'sessionSubject': PATIENT, 'issuer': ISSUER, **record_fields,
        }, SIGNING_KEY)
        result = write_approved_bundle(
            {'approval_ref': 'cpg-phi:run-7/approvals/0.json', 'careplan_id': CAREPLAN_ID,
             'fhir_bundle_ref': 'cpg-phi:run-7/fhir_bundle.json'},
            _PhiStore(bundle, record), fhir_server_writer,
        )
        assert result['delivery_status'] == 'delivered'
        return bundle

    yield publish
    writer_module._care_plans.clear()


@pytest.fixture
def browser(monkeypatch):
    """BFF sessions whose fhir-server calls are answered by the real care-plan index."""
    def fhir_server(request: httpx.Request) -> httpx.Response:
        assert request.headers.get('X-Service-Token')
        if request.url.path == '/api/v1/careplans':
            return httpx.Response(200, json=list_care_plans(patient=request.url.params.get('patient')))
        record = get_care_plan(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(200, json=record) if record else httpx.Response(404, json={'detail': 'Not found'})

    manager = SessionManager(MemorySessionStore(), cookie_name='acp_session', public_origin='https://writer.example')
    monkeypatch.setattr(bff, '_sessions', manager)
    monkeypatch.setattr(bff, '_sonataflow', None)
    monkeypatch.setattr(bff, '_http', httpx.AsyncClient(transport=httpx.MockTransport(fhir_server)))

    def open_session(patient=PATIENT, issuer=ISSUER):
        _, cookie = manager.create('patient', patient, [], meta={'iss': issuer} if issuer is not None else {})
        client = TestClient(bff.app, base_url='https://writer.example')
        client.cookies.set('acp_session', cookie)
        return client

    return open_session


def _listed(client) -> list[str]:
    response = client.get('/api/v1/careplans')
    assert response.status_code == 200
    return [plan['id'] for plan in response.json()]


def _assert_only_the_approving_session_can_read(browser):
    owner = browser()
    detail = owner.get(f'/api/v1/careplans/{CAREPLAN_ID}')
    assert detail.status_code == 200, detail.text
    assert _listed(owner) == [CAREPLAN_ID]
    for patient, issuer in [('another-patient', ISSUER), (PATIENT, OTHER_ISSUER), (PATIENT, ''), (PATIENT, None)]:
        other = browser(patient, issuer)
        assert other.get(f'/api/v1/careplans/{CAREPLAN_ID}').status_code == 403
        assert _listed(other) == []


def test_real_builder_subject_is_bundle_local(published):
    """The premise: nothing in the bundle names the EHR patient as a subject."""
    bundle = published()
    plans = [e['resource'] for e in bundle['entry'] if e['resource']['resourceType'] == 'CarePlan']
    assert len(plans) == 1
    assert plans[0]['subject']['reference'].startswith('urn:uuid:')


def test_approved_write_records_patient_and_issuer_from_the_signed_record(published):
    published()
    record = get_care_plan(CAREPLAN_ID)
    assert record['patient_reference'] == 'Patient/' + PATIENT
    assert record['issuer'] == ISSUER
    assert list_care_plans(patient=PATIENT) == [{
        'id': CAREPLAN_ID, 'patient_name': record['patient_name'], 'patient_reference': 'Patient/' + PATIENT,
        'issuer': ISSUER, 'status': 'active', 'generated_at': record['generated_at'],
    }]
    assert list_care_plans(patient='another-patient') == []


def test_approving_session_can_open_and_list_the_published_plan(published, browser):
    published()
    _assert_only_the_approving_session_can_read(browser)


def test_access_survives_loss_of_the_in_memory_index(published, browser):
    published()
    writer_module._care_plans.clear()
    assert browser().get(f'/api/v1/careplans/{CAREPLAN_ID}').status_code != 200
    assert rehydrate_care_plans_from_jobs() == 1
    record = get_care_plan(CAREPLAN_ID)
    assert (record['patient_reference'], record['issuer']) == ('Patient/' + PATIENT, ISSUER)
    _assert_only_the_approving_session_can_read(browser)


def test_plan_approved_without_an_issuer_is_visible_only_without_one(published, browser):
    published(issuer='')
    for issuer in ('', None):
        owner = browser(PATIENT, issuer)
        assert owner.get(f'/api/v1/careplans/{CAREPLAN_ID}').status_code == 200
        assert _listed(owner) == [CAREPLAN_ID]
    smart = browser(PATIENT, ISSUER)
    assert smart.get(f'/api/v1/careplans/{CAREPLAN_ID}').status_code == 403
    assert _listed(smart) == []
    assert browser('another-patient', '').get(f'/api/v1/careplans/{CAREPLAN_ID}').status_code == 403


def test_job_written_before_ownership_was_recorded_has_no_issuer(published, browser):
    """Older jobs fall back to the server-assigned patient and match no SMART session."""
    published()
    store = automation_publisher_module.get_publisher().store
    for job in store.list():
        job.pop('patient_reference', None)
        job.pop('issuer', None)
        store.put(job)
    writer_module._care_plans.clear()
    assert rehydrate_care_plans_from_jobs() == 1
    record = get_care_plan(CAREPLAN_ID)
    assert record['issuer'] == ''
    assert record['patient_reference'].startswith('Patient/server-')
    assert browser().get(f'/api/v1/careplans/{CAREPLAN_ID}').status_code == 403
    assert _listed(browser()) == []


@pytest.mark.parametrize('record', [
    None, [], 'Patient/' + PATIENT,
    {}, {'issuer': ISSUER},
    {'patient_reference': 'urn:uuid:' + PATIENT, 'issuer': ISSUER},
    {'patient_reference': 'Practitioner/' + PATIENT, 'issuer': ISSUER},
    {'patient_reference': 'Patient/' + PATIENT},
    {'patient_reference': 'Patient/' + PATIENT, 'issuer': OTHER_ISSUER},
    # The bundle is never consulted, whatever its subject says.
    {'issuer': ISSUER, 'bundle': {'entry': [{'resource': {'resourceType': 'CarePlan', 'subject': {'reference': 'Patient/' + PATIENT}}}]}},
])
def test_records_that_do_not_name_this_patient_and_issuer_are_out_of_scope(record):
    session = SimpleNamespace(subject=PATIENT, meta={'iss': ISSUER})
    assert bff._careplan_in_session(record, session) is False


def test_matching_record_is_in_scope():
    session = SimpleNamespace(subject=PATIENT, meta={'iss': ISSUER})
    assert bff._careplan_in_session({'patient_reference': 'Patient/' + PATIENT, 'issuer': ISSUER}, session) is True
