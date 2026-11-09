"""Persist and deliver a signed decision for one exact operator review round."""
import asyncio
import os
from datetime import datetime, timezone

from cpg_contracts.approvals import artifact_revision, sign_record, verify_record
from cpg_contracts.artifact_paths import validated_run_artifact_key
from cpg_contracts.tracing import trace_safe
from cpg_contracts.web_session import AsyncKeyedLocks
from fastapi import HTTPException

_locks = AsyncKeyedLocks()


def _missing(exc):
    return isinstance(exc, KeyError) or getattr(exc,'response',{}).get('Error',{}).get('Code') in {'NoSuchKey','NotFound','404'}


@trace_safe
def add_review_revision(detail):
    gate = detail.get('awaitingReview')
    fields = ('manifest', 'sectionMap', 'metadata') if gate == 'manifest' else ('decisions','recommendations','automations','recommendationEscalations','escalatedItems')
    if gate in {'manifest','pre-delivery'}:
        detail['artifactRevision'] = artifact_revision({field:detail.get(field) for field in fields})
    return detail


@trace_safe
async def submit_bound_review(run_id, gate, review, store, workflow, load_detail, validate, *, session_subject="operator"):

    if gate not in {'manifest','pre-delivery'}:
        raise HTTPException(400, detail={'error':'unknown-gate'})
    try:
        record_key = validated_run_artifact_key(run_id, f'reviews/{gate}-{review.get("reviewRound")}.json')
    except ValueError:
        raise HTTPException(400, detail={'error':'approval-binding'}) from None
    round_number = review.get('reviewRound')
    if type(round_number) is not int or round_number < 0 or round_number > 1000000 or review.get('action') not in {'approve','request_changes','cancel'}:
        raise HTTPException(400, detail={'error':'approval-binding'})
    key = os.getenv('CPG_SIGNING_KEY','')
    if not key or store is None:
        raise HTTPException(503, detail={'error':'approval-store-unavailable'})
    key_id = os.getenv('CPG_SIGNING_KEY_ID','v1')
    receipt_key = record_key.replace('/reviews/','/review-deliveries/')
    event = {k:v for k,v in review.items() if k in {'action','feedback','overallComment','parameterEdits','acknowledgements','reviewRound','artifactRevision'}}
    async with _locks.hold(run_id):
        try:
            try:
                record = await asyncio.to_thread(store.get, record_key)
            except Exception as exc:
                if not _missing(exc): raise
                record = None
            if record:
                if not verify_record(record,key,key_id) or record.get('event') != event:
                    raise HTTPException(409, detail={'error':'approval-stale'})
                try:
                    receipt = await asyncio.to_thread(store.get, receipt_key)
                except Exception as exc:
                    if not _missing(exc): raise
                    receipt = None
                if receipt and verify_record(receipt,key,key_id) and receipt.get('recordRevision') == artifact_revision(record):
                    return {'status':'accepted','duplicate':True}
            else:
                current = add_review_revision(await asyncio.to_thread(load_detail, run_id))
                if current.get('awaitingReview') != gate or current.get('reviewIteration') != round_number or current.get('artifactRevision') != review.get('artifactRevision'):
                    raise HTTPException(409, detail={'error':'approval-stale'})
                error = validate(current,gate,review)
                if error:
                    raise HTTPException(400, detail={'error':error})
                record = sign_record({'runId':run_id,'gate':gate,'reviewRound':round_number,
                    'artifactRevision':current['artifactRevision'],'event':event,
                    'sessionSubject':session_subject, 'reviewer':os.getenv('CPG_REVIEWER', 'operator'),
                    'completed_at':datetime.now(timezone.utc).isoformat()}, key,key_id)
                await asyncio.to_thread(store.put, record_key, record)
            payload = {**record['event'],'completed_at':record['completed_at']}
            await asyncio.to_thread(workflow.send_review, run_id, gate, payload, event_id=artifact_revision(record))
            await asyncio.to_thread(store.put, receipt_key, sign_record({'recordRevision':artifact_revision(record)},key,key_id))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, detail={'error':'review-delivery-unavailable'}) from None
    return {'status':'accepted','reviewRound':round_number,'artifactRevision':record['artifactRevision']}
