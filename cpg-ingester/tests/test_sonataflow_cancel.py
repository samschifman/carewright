"""Workflow cancel paths never send an unbound review event."""
from unittest.mock import Mock

import pytest
from cpg_ingester.services.sonataflow_client import SonataFlowClient


@pytest.mark.parametrize('data', [{'analysisResult':{}}, {'generateResult':{}}])
def test_review_cancel_requires_bound_bff_path(monkeypatch,data):
    client=SonataFlowClient('http://workflow.test')
    monkeypatch.setattr(client,'get_instance',lambda _: {'status':'ACTIVE','workflowdata':data})
    post=Mock()
    delete=Mock()
    monkeypatch.setattr('cpg_ingester.services.sonataflow_client.requests.post',post)
    monkeypatch.setattr('cpg_ingester.services.sonataflow_client.requests.delete',delete)
    with pytest.raises(ValueError, match='bound BFF review'):
        client.cancel_instance('run-1')
    post.assert_not_called()
    delete.assert_not_called()


def test_non_gate_cancel_sends_cancelled_event(monkeypatch):
    client=SonataFlowClient('http://workflow.test')
    monkeypatch.setattr(client,'get_instance',lambda _: {'status':'ACTIVE','workflowdata':{}})
    post=Mock()
    monkeypatch.setattr('cpg_ingester.services.sonataflow_client.requests.post',post)
    client.cancel_instance('run-1')
    event=post.call_args.kwargs['json']
    assert event['data']=={'cancelled':True}
    assert event['kogitoprocrefid']=='run-1'


def test_cancel_without_wait_state_aborts_instance(monkeypatch):
    client=SonataFlowClient('http://workflow.test')
    monkeypatch.setattr(client,'get_instance',lambda _: {'status':'ACTIVE','workflowdata':{'deliveryResult':{}}})
    delete=Mock()
    monkeypatch.setattr('cpg_ingester.services.sonataflow_client.requests.delete',delete)
    client.cancel_instance('run-1')
    assert delete.call_args.args[0].endswith('/run-1')
