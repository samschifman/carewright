"""Durability checks for approval job storage."""

from acp_writer.services.job_store import JobStore


def test_directory_store_survives_a_new_store_instance(tmp_path):
    first = JobStore(directory=tmp_path)
    job = {"job_id": "job-1", "careplan_id": "careplan-1", "state": "approving"}
    first.put(job)
    first.put_withdrawal("careplan-1", {"status": "withdrawn"})

    second = JobStore(directory=tmp_path)
    assert second.get("careplan-1", "job-1") == job
    assert second.list("careplan-1") == [job]
    assert second.get_withdrawal("careplan-1") == {"status": "withdrawn"}


def test_store_rejects_path_traversal_ids(tmp_path):
    store = JobStore(directory=tmp_path)
    try:
        store.get("../escape", "job")
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("unsafe care-plan id was accepted")
