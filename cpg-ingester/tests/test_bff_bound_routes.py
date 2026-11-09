"""Real BFF routes retain status codes and bind cancel at both review gates."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient


@pytest.fixture
def browser(monkeypatch):
    monkeypatch.setenv('SONATAFLOW_URL','http://workflow.test')
    monkeypatch.setenv('CPG_PUBLIC_ORIGIN','https://ingester.test')
    monkeypatch.setenv('CPG_SIGNING_KEY','synthetic-signing-key')
    path = Path(__file__).parents[1]/'src/cpg_ingester/services/bff.py'
    spec = importlib.util.spec_from_file_location('review_route_bff', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = Mock()
    store.get.side_effect = KeyError('not found')
    module._artifact_store = store
    workflow = Mock()
    module._sonataflow = workflow
    c = TestClient(module.app,base_url='https://ingester.test')
    # conftest supplies a valid operator session to this real BFF.
    return c,module,workflow,store


@pytest.mark.parametrize('status,expected', [(404,404),(503,503)])
def test_unknown_or_unavailable_workflow_preserves_status(browser,status,expected):
    c,module,workflow,store=browser
    workflow.get_instance.side_effect=requests.HTTPError(response=SimpleNamespace(status_code=status))
    assert c.get('/api/v1/runs/missing').status_code == expected
    # Valid binding syntax reaches the live detail loader.
    response=c.post('/api/v1/runs/missing/review/manifest',json={
        'action':'approve','reviewRound':0,'artifactRevision':'0'*64})
    assert response.status_code == expected
    workflow.send_review.assert_not_called()


def test_malformed_json_is_400(browser):
    c,_,workflow,_=browser
    assert c.post('/api/v1/runs/run-1/review/manifest',content='{').status_code == 400
    workflow.send_review.assert_not_called()


@pytest.mark.parametrize('gate', ['manifest','pre-delivery'])
def test_cancel_sends_current_bound_review(browser,monkeypatch,gate):
    c,module,workflow,store=browser
    current=module.add_review_revision({'awaitingReview':gate,'reviewIteration':3})
    monkeypatch.setattr(module,'_load_detail',lambda _:current)
    response=c.delete('/api/v1/runs/run-1')
    assert response.status_code == 200
    assert response.json() == {'status':'cancelled'}
    args=workflow.send_review.call_args.args
    assert args[:2] == ('run-1',gate)
    assert args[2]['action']=='cancel' and args[2]['reviewRound']==3
    assert args[2]['artifactRevision']==current['artifactRevision']
    workflow.cancel_instance.assert_not_called()


def test_cancel_outside_review_gate_uses_workflow_cancel(browser,monkeypatch):
    c,module,workflow,_ = browser
    monkeypatch.setattr(module,'_load_detail',lambda _: {'awaitingReview':None})
    assert c.delete('/api/v1/runs/run-1').json() == {'status':'cancelled'}
    workflow.cancel_instance.assert_called_once_with('run-1')
    workflow.send_review.assert_not_called()


@pytest.mark.parametrize('failure,expected,code', [
    (ValueError('sensitive unbound cancel'),503,'workflow-unavailable'),
    (ConnectionError('sensitive upstream'),503,'workflow-unavailable'),
    (requests.HTTPError(response=SimpleNamespace(status_code=404)),404,'run-not-found'),
])
def test_cancel_failure_status_and_redaction(browser,monkeypatch,failure,expected,code):
    c,module,workflow,_=browser
    monkeypatch.setattr(module,'_load_detail',lambda _: {'awaitingReview':None})
    workflow.cancel_instance.side_effect=failure
    response=c.delete('/api/v1/runs/sensitive-run-id')
    assert response.status_code==expected
    assert response.json()=={'detail':{'error':code}}


@pytest.mark.parametrize('status,expected,code', [(404,404,'run-not-found'),(503,503,'workflow-unavailable')])
def test_rerun_failure_status_and_redaction(browser,status,expected,code):
    c,_,workflow,_=browser
    workflow.get_instance.side_effect=requests.HTTPError(response=SimpleNamespace(status_code=status))
    response=c.post('/api/v1/runs/sensitive-run-id/rerun')
    assert response.status_code==expected
    assert response.json()=={'detail':{'error':code}}


@pytest.mark.parametrize('failure,expected,code', [
    (KeyError('sensitive object key'),404,'artifact-not-found'),
    (requests.HTTPError('sensitive upstream'),503,'artifact-store-unavailable'),
    (ConnectionError('sensitive upstream'),503,'artifact-store-unavailable'),
])
def test_artifact_failure_does_not_echo_request(browser,failure,expected,code):
    c,_,_,store=browser
    store.get_raw_in_bucket.side_effect=failure
    response=c.get('/api/v1/runs/sensitive-run-id/artifacts/sensitive-artifact.json')
    assert response.status_code==expected
    assert response.json()=={'detail':{'error':code}}


@pytest.mark.parametrize('gate', ['manifest','pre-delivery'])
def test_gate_cancel_without_store_is_unavailable(browser,monkeypatch,gate):
    c,module,workflow,_=browser
    monkeypatch.setattr(module,'_artifact_store',None)
    monkeypatch.setattr(module,'_load_detail',lambda _:module.add_review_revision({'awaitingReview':gate,'reviewIteration':1}))
    response=c.delete('/api/v1/runs/run-1')
    assert response.status_code==503
    assert response.json()=={'detail':{'error':'approval-store-unavailable'}}
    workflow.send_review.assert_not_called()
