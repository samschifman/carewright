import asyncio
from types import SimpleNamespace

from acp_writer import api
from acp_writer.services import automation_publisher


def test_monolith_startup_reconciles_approval_jobs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        automation_publisher,
        "get_publisher",
        lambda: SimpleNamespace(reconcile_on_start=lambda: calls.append("reconciled")),
    )

    asyncio.run(api.startup())

    assert calls == ["reconciled"]
