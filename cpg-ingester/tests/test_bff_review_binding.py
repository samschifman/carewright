import copy
from unittest.mock import Mock

import pytest
from cpg_ingester.services.review_binding import (
    add_review_revision,
    submit_bound_review,
)
from fastapi import HTTPException


class Store:
    def __init__(self): self.data = {}
    def get(self,key): return copy.deepcopy(self.data[key])
    def put(self,key,value): self.data[key] = copy.deepcopy(value)

def test_binding_replay_and_delivery_recovery(monkeypatch):
    import asyncio
    async def run():
        monkeypatch.setenv('CPG_SIGNING_KEY','synthetic-signing-key')
        store,workflow = Store(),Mock()
        detail = add_review_revision({'awaitingReview':'manifest','reviewIteration':0,'manifest':[{'id':'synthetic'}]})
        review = {'action':'approve','reviewRound':0,'artifactRevision':detail['artifactRevision'],'completed_at':'untrusted'}
        workflow.send_review.side_effect = RuntimeError('unavailable')
        async def submit(body=review):
            return await submit_bound_review('run-1','manifest',body,store,workflow,lambda _:detail,lambda *args:None)
        with pytest.raises(HTTPException) as failure: await submit()
        assert failure.value.status_code == 503
        workflow.send_review.side_effect = None
        assert (await submit())['status'] == 'accepted'
        sent = workflow.send_review.call_args.args[2]
        assert sent['completed_at'] != 'untrusted'
        assert workflow.send_review.call_args_list[0] == workflow.send_review.call_args_list[1]
        assert (await submit())['duplicate'] is True
        assert workflow.send_review.call_count == 2
        with pytest.raises(HTTPException) as failure: await submit({**review,'action':'request_changes'})
        assert failure.value.status_code == 409
        with pytest.raises(HTTPException) as failure: await submit({**review,'reviewRound':1})
        assert failure.value.status_code == 409
    asyncio.run(run())


def test_workflows_fail_closed():
    from pathlib import Path

    import yaml
    root=Path(__file__).resolve().parents[2]
    for path in [root/'cpg-ingester/deploy/orchestrator/cpg-ingester-workflow.yaml',root/'carewright-operator/internal/controller/workflows/cpg-ingester-workflow.yaml']:
        doc=yaml.safe_load(path.read_text());flow=doc.get('spec',{}).get('flow',doc)
        states={state['name']:state for state in flow['states']}
        for label,rearm,obj in [('Manifest','ReviewManifest','manifestReview'),('Artifact','ReviewArtifacts','artifactsReview')]:
            assert states['Check'+label+'Review']['defaultCondition']['transition']==rearm
            assert states['Validate'+label+'ReviewRound']['defaultCondition']['transition']==rearm
            assert '!= null' in states['Validate'+label+'ReviewRound']['dataConditions'][0]['condition']


@pytest.mark.parametrize('gate', ['manifest', 'pre-delivery'])
def test_cancel_is_bound_to_live_round_and_session(monkeypatch, gate):
    import asyncio
    monkeypatch.setenv('CPG_SIGNING_KEY', 'synthetic-signing-key')
    monkeypatch.setenv('CPG_REVIEWER', 'configured-operator')
    store, workflow = Store(), Mock()
    current = add_review_revision({'awaitingReview':gate,'reviewIteration':2})
    review = {'action':'cancel','reviewRound':2,'artifactRevision':current['artifactRevision']}
    asyncio.run(submit_bound_review('run-cancel', gate, review, store, workflow,
        lambda _:current, lambda *args:None, session_subject='synthetic-session'))
    sent = workflow.send_review.call_args.args[2]
    assert sent['reviewRound'] == 2 and sent['action'] == 'cancel'
    record = store.data[f'run-cancel/reviews/{gate}-2.json']
    assert record['sessionSubject'] == 'synthetic-session'
    assert record['reviewer'] == 'configured-operator'


@pytest.mark.parametrize('failure', ['revision', 'tamper', 'missing-key'])
def test_review_rejection_before_event(monkeypatch, failure):
    import asyncio
    monkeypatch.setenv('CPG_SIGNING_KEY','synthetic-signing-key')
    store, workflow = Store(), Mock()
    detail = add_review_revision({'awaitingReview':'manifest','reviewIteration':0})
    review = {'action':'approve','reviewRound':0,'artifactRevision':detail['artifactRevision']}
    if failure == 'revision': review['artifactRevision'] = '0'*64
    if failure == 'tamper':
        store.put('run-1/reviews/manifest-0.json', {'event':review,'signature':'forged'})
    if failure == 'missing-key': monkeypatch.delenv('CPG_SIGNING_KEY')
    with pytest.raises(HTTPException) as exc:
        asyncio.run(submit_bound_review('run-1','manifest',review,store,workflow,lambda _:detail,lambda *args:None))
    assert exc.value.status_code == (503 if failure == 'missing-key' else 409)
    workflow.send_review.assert_not_called()
