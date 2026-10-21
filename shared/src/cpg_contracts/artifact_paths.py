"""Validation helpers for run-scoped artifact keys."""

import re
import unicodedata

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESERVED_NAMESPACE = "automation-publications"


def validated_run_artifact_key(run_id: str, path: str) -> str:
    """Return a run-scoped key after validating decoded route components."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid run ID")
    if run_id == _RESERVED_NAMESPACE:
        raise ValueError("Reserved artifact namespace")
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise ValueError("Invalid artifact path")
    if any(
        character in {":", "\\", "%"} or unicodedata.category(character) == "Cc"
        for character in path
    ):
        raise ValueError("Invalid artifact path")
    segments = path.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise ValueError("Invalid artifact path")
    if segments[0] == _RESERVED_NAMESPACE:
        raise ValueError("Reserved artifact namespace")
    return f"{run_id}/{path}"
