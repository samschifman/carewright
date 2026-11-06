"""Shared test configuration."""

import logging


def pytest_configure(config):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


import pytest


@pytest.fixture
def service_headers(monkeypatch):
    """Explicit credentials for established endpoint behavior tests.

    Access-control tests clear client headers and vary receiver configuration.
    The same synthetic value lets monolith tests call multiple receiving roles.
    Production credentials remain distinct per receiving service.
    """
    keys = (
        'ACP_BFF', 'ACP_FHIR_SERVER', 'ACP_DECISION_ENGINE', 'ACP_LLM_REASONING',
        'ACP_PATIENT_DATA', 'ACP_FHIR_GENERATION', 'CPG_BFF', 'CPG_INGESTION',
        'CPG_LLM_ANALYSIS', 'CPG_ASSEMBLY', 'CPG_DELIVERY',
    )
    for key in keys:
        monkeypatch.setenv('SERVICE_TOKEN_' + key, 'test-only-service-credential')
    monkeypatch.delenv('WORKFLOW_CALLER_UNAUTHENTICATED', raising=False)
    return {'X-Service-Token': 'test-only-service-credential'}


@pytest.fixture(autouse=True)
def authenticated_service_clients(monkeypatch, service_headers, request):
    """Keep pre-existing behavior tests authenticated; auth tests clear headers."""
    from fastapi.testclient import TestClient
    original = TestClient.__init__
    def init(self, *args, **kwargs):
        kwargs['headers'] = {**service_headers, **kwargs.get('headers', {})}
        original(self, *args, **kwargs)
    monkeypatch.setattr(TestClient, '__init__', init)
    for existing in vars(request.module).values():
        if isinstance(existing, TestClient):
            monkeypatch.setitem(existing.headers, 'X-Service-Token', service_headers['X-Service-Token'])


@pytest.fixture(autouse=True)
def legacy_bff_browser_session(monkeypatch, request):
    """Explicit patient context for existing artifact/review behavior fixtures."""
    if request.module.__name__.split('.')[-1] not in {
        'test_bff_artifact_access', 'test_bff_review_mapping',
        'test_bff_review_submit', 'test_bff_careplan_brief_automations',
    }:
        return
    from acp_writer.services import bff
    from cpg_contracts.web_session import MemorySessionStore, SessionManager
    from fastapi.testclient import TestClient
    manager = SessionManager(MemorySessionStore(), cookie_name='acp_session')
    session, cookie = manager.create('patient', 'p1', [
        'run-1', 'run-123', '01234567-89ab-cdef-0123-456789abcdef',
    ])
    monkeypatch.setattr(bff, '_sessions', manager)
    original = TestClient.__init__
    def init(self, app, *args, **kwargs):
        original(self, app, *args, **kwargs)
        if app is bff.app:
            self.cookies.set('acp_session', cookie)
            self.headers['X-CSRF-Token'] = session.csrf_token
    monkeypatch.setattr(TestClient, '__init__', init)


@pytest.fixture(scope="session", autouse=True)
def isolated_mlflow_backend(tmp_path_factory):
    """Keep routine test traces out of persistent working-directory databases.

    Explicit telemetry tests can temporarily select their own backend. All
    subprocesses inherit this URI, and queued spans flush before teardown.
    """
    patch = pytest.MonkeyPatch()
    uri = "sqlite:///" + str(tmp_path_factory.mktemp("mlflow") / "traces.db")
    patch.setenv("MLFLOW_TRACKING_URI", uri)
    patch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
    patch.delenv("MLFLOW_EXPERIMENT_NAME", raising=False)
    try:
        import mlflow
    except ImportError:
        try:
            yield
        finally:
            patch.undo()
        return
    previous_uri = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("unit-tests")
    try:
        yield
    finally:
        mlflow.flush_trace_async_logging()
        mlflow.set_tracking_uri(previous_uri)
        patch.undo()
