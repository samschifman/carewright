"""Durable storage for approval and automation-publication jobs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid
from typing import Any

import mlflow

from cpg_contracts import ArtifactStore, get_phi_store


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe(value: str) -> str:
    if value in {".", ".."} or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError("job identifiers contain unsupported storage characters")
    return value


class JobStore:
    """Store publication jobs in the PHI bucket or an atomic local directory."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore | None = None,
        directory: str | Path | None = None,
    ) -> None:
        self.artifact_store = artifact_store if artifact_store is not None else get_phi_store()
        self.directory = Path(
            directory or os.getenv("ACP_JOB_STORE_DIR", "data/automation-publications")
        ).expanduser()

    @staticmethod
    def _job_key(careplan_id: str, job_id: str) -> str:
        return f"automation-publications/{_safe(careplan_id)}/jobs/{_safe(job_id)}.json"

    @staticmethod
    def _withdrawal_key(careplan_id: str) -> str:
        return f"automation-publications/{_safe(careplan_id)}/withdrawn.json"

    @mlflow.trace(name="automation.job_store.put")
    def put(self, job: dict[str, Any]) -> None:
        key = self._job_key(job["careplan_id"], job["job_id"])
        if self.artifact_store:
            self.artifact_store.put(key, job)
            return
        self._write_file(key, json.dumps(job, sort_keys=True).encode("utf-8"))

    @mlflow.trace(name="automation.job_store.get")
    def get(self, careplan_id: str, job_id: str) -> dict[str, Any] | None:
        key = self._job_key(careplan_id, job_id)
        if self.artifact_store:
            return self._get_artifact_json(key)
        return self._get_file_json(key)

    @mlflow.trace(name="automation.job_store.list")
    def list(self, careplan_id: str | None = None) -> list[dict[str, Any]]:
        if careplan_id is not None:
            careplan = _safe(careplan_id)
            prefix = f"automation-publications/{careplan}/jobs/"
        else:
            prefix = "automation-publications/"
        if self.artifact_store:
            return self._list_artifact_jobs(prefix)
        paths = self.directory.glob(f"{prefix}*.json") if careplan_id else self.directory.glob(
            "automation-publications/*/jobs/*.json"
        )
        jobs = []
        for path in paths:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                jobs.append(item)
        return sorted(jobs, key=lambda item: (item.get("careplan_id", ""), item.get("created_at", "")))

    @mlflow.trace(name="automation.job_store.withdrawal")
    def get_withdrawal(self, careplan_id: str) -> dict[str, Any] | None:
        key = self._withdrawal_key(careplan_id)
        if self.artifact_store:
            return self._get_artifact_json(key)
        return self._get_file_json(key)

    @mlflow.trace(name="automation.job_store.tombstone")
    def put_withdrawal(self, careplan_id: str, marker: dict[str, Any]) -> None:
        key = self._withdrawal_key(careplan_id)
        if self.artifact_store:
            self.artifact_store.put(key, marker)
            return
        self._write_file(key, json.dumps(marker, sort_keys=True).encode("utf-8"))

    @mlflow.trace(name="automation.job_store.get_artifact")
    def _get_artifact_json(self, key: str) -> dict[str, Any] | None:
        try:
            value = self.artifact_store.get(f"{self.artifact_store.bucket}:{key}")
        except Exception as exc:
            response = getattr(exc, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = response.get("Error", {}).get("Code")
            if status == 404 or code in {"NoSuchKey", "404"}:
                return None
            raise
        return value if isinstance(value, dict) else None

    @mlflow.trace(name="automation.job_store.list_artifacts")
    def _list_artifact_jobs(self, prefix: str) -> list[dict[str, Any]]:
        client = self.artifact_store._get_client()
        paginator = client.get_paginator("list_objects_v2")
        jobs = []
        for page in paginator.paginate(Bucket=self.artifact_store.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                if "/jobs/" not in key or not key.endswith(".json"):
                    continue
                value = self._get_artifact_json(key)
                if value:
                    jobs.append(value)
        return sorted(jobs, key=lambda item: (item.get("careplan_id", ""), item.get("created_at", "")))

    @mlflow.trace(name="automation.job_store.get_file")
    def _get_file_json(self, key: str) -> dict[str, Any] | None:
        try:
            value = json.loads((self.directory / key).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @mlflow.trace(name="automation.job_store.write_file")
    def _write_file(self, key: str, payload: bytes) -> None:
        path = self.directory / key
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
