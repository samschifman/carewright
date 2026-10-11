"""F13: meaningful conflict-path functions must carry @mlflow.trace.

AGENTS.md requires every function doing meaningful work (data transformations,
external calls) to be traced. These conflict read-back / provenance / normalize
functions were previously untraced. ``mlflow.trace`` marks the wrapped callable
with ``__mlflow_traced__`` — assert it is present.
"""

from acp_writer.planning_brief import coerce_conflicts
from acp_writer.services.ai_transparency import (
    build_conflict_provenance,
    plan_conflict_from_provenance,
)
from acp_writer.services.artifact_resolver import (
    _automation_view_from_entry,
    enrich_run_detail,
    plan_activity_from_entry,
    plan_conflict_from_entry,
    plan_goal_from_entry,
)
from acp_writer.services.bff import (
    _automation_view_from_fhir_extension,
    _extract_view_from_bundle,
)


def test_conflict_functions_are_traced():
    for fn in (
        coerce_conflicts,
        build_conflict_provenance,
        plan_conflict_from_provenance,
        plan_conflict_from_entry,
        plan_goal_from_entry,
        plan_activity_from_entry,
        _automation_view_from_entry,
        enrich_run_detail,
        _automation_view_from_fhir_extension,
        _extract_view_from_bundle,
    ):
        assert getattr(fn, "__mlflow_traced__", False), f"{fn.__name__} is not @mlflow.trace-decorated"
