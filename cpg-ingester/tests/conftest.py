"""Authenticated clients for existing service behavior tests."""
import pytest


@pytest.fixture
def service_headers(monkeypatch):
    for key in ('ACP_DECISION_ENGINE', 'CPG_BFF', 'CPG_INGESTION', 'CPG_LLM_ANALYSIS', 'CPG_ASSEMBLY', 'CPG_DELIVERY'):
        monkeypatch.setenv('SERVICE_TOKEN_' + key, 'test-only-service-credential')
    monkeypatch.delenv('WORKFLOW_CALLER_UNAUTHENTICATED', raising=False)
    return {'X-Service-Token': 'test-only-service-credential'}


@pytest.fixture(autouse=True)
def authenticated_service_clients(monkeypatch, service_headers, request):
    from fastapi.testclient import TestClient
    original = TestClient.__init__
    def init(self, *args, **kwargs):
        kwargs['headers'] = {**service_headers, **kwargs.get('headers', {})}
        original(self, *args, **kwargs)
        if (getattr(self.app, 'title', '') == 'cpg-ingester-bff'
                and request.module.__name__.split('.')[-1] not in {'test_bff_session','test_service_auth_routes','test_bff_review_binding'}):
            manager = self.app.state.sessions
            session, raw = manager.create('operator','operator')
            self.cookies.set('cpg_session',raw)
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
