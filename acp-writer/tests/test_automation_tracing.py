"""Automation approval, persistence and publication work must be traced."""

from acp_writer.nodes.fhir_server_writer import (
    _automation_revisions,
    _record_and_publish,
    _set_careplan_identifier,
    _start_approval_job,
)
from acp_writer.api import health as api_health
from acp_writer.services.automation_publisher import (
    AutomationPublisher,
    _build_payload,
    _entry_index,
    _resource_ids,
    _template_refs,
)
from acp_writer.services.job_store import JobStore


def test_automation_publication_functions_are_traced():
    functions = [
        _automation_revisions,
        _record_and_publish,
        _set_careplan_identifier,
        _start_approval_job,
        _build_payload,
        _entry_index,
        _resource_ids,
        _template_refs,
        AutomationPublisher.start_approval,
        AutomationPublisher.record_write,
        AutomationPublisher.record_recovered_write,
        AutomationPublisher.publish,
        AutomationPublisher.schedule_retries,
        AutomationPublisher._retry_worker,
        AutomationPublisher.withdraw,
        AutomationPublisher.retry,
        AutomationPublisher.reconcile_on_start,
        JobStore.put,
        JobStore.get,
        JobStore.list,
        JobStore.get_withdrawal,
        JobStore.put_withdrawal,
        JobStore._get_artifact_json,
        JobStore._list_artifact_jobs,
        JobStore._get_file_json,
        JobStore._write_file,
    ]
    assert all(getattr(function, "__mlflow_traced__", False) for function in functions)


def test_recursive_reference_rewrite_is_not_traced():
    from acp_writer.services.automation_publisher import _rewrite_references

    assert not getattr(_rewrite_references, "__mlflow_traced__", False)


def test_api_health_is_not_traced():
    assert not getattr(api_health, "__mlflow_traced__", False)
