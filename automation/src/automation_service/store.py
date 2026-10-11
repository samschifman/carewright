"""Durable storage for published care-plan automation records."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from cpg_contracts import ArtifactStore, get_phi_store


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def safe_component(value: str) -> str:
    """Reject identifiers that could escape their storage prefix."""
    if value in {".", ".."} or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError("identifier contains unsupported storage characters")
    return value


class AutomationStore:
    """Use the PHI bucket when configured, otherwise persist under a directory."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore | None = None,
        directory: str | Path | None = None,
    ):
        self.artifact_store = artifact_store if artifact_store is not None else get_phi_store()
        self.directory = Path(
            directory or os.getenv("AUTOMATION_STORE_DIR", "data/automations")
        ).expanduser()

    @staticmethod
    def record_key(careplan_id: str, automation_id: str, revision: str) -> str:
        careplan = safe_component(careplan_id)
        automation = safe_component(automation_id)
        rev = safe_component(revision)
        return f"automations/{careplan}/{automation}/{rev}"

    def put_record(
        self,
        careplan_id: str,
        automation_id: str,
        revision: str,
        record: dict[str, Any],
        bpmn_xml: str,
    ) -> None:
        """Persist the PHI-bearing record and its canonical BPMN XML."""
        key = self.record_key(careplan_id, automation_id, revision)
        if self.artifact_store:
            self.artifact_store.put(f"{key}.json", record)
            self.artifact_store.put_raw(
                f"{key}.bpmn", bpmn_xml.encode("utf-8"), "application/xml"
            )
            return
        self._write_file(f"{key}.json", json.dumps(record, sort_keys=True).encode())
        self._write_file(f"{key}.bpmn", bpmn_xml.encode("utf-8"))

    def get_record(
        self,
        careplan_id: str,
        automation_id: str,
        revision: str | None = None,
    ) -> dict[str, Any] | None:
        careplan = safe_component(careplan_id)
        automation = safe_component(automation_id)
        if revision is not None:
            key = f"automations/{careplan}/{automation}/{safe_component(revision)}.json"
            return self._get_json(key)
        records = self.list_records(careplan)
        matches = [item for item in records if item.get("automation_id") == automation]
        return max(
            matches,
            key=lambda item: (item.get("approved_at", ""), item.get("stored_at", "")),
            default=None,
        )

    def list_records(self, careplan_id: str) -> list[dict[str, Any]]:
        careplan = safe_component(careplan_id)
        prefix = f"automations/{careplan}/"
        if self.artifact_store:
            client = self.artifact_store._get_client()
            response = client.list_objects_v2(
                Bucket=self.artifact_store.bucket,
                Prefix=prefix,
            )
            keys = [
                item["Key"] for item in response.get("Contents", [])
                if item["Key"].endswith(".json")
                and item["Key"].count("/") == 3
            ]
            records = [self._get_json(key) for key in keys]
        else:
            path = self.directory / prefix
            records = []
            if path.exists():
                for record_path in path.glob("*/*.json"):
                    if record_path.name == "withdrawn.json":
                        continue
                    try:
                        records.append(json.loads(record_path.read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError):
                        continue
        return sorted(
            [item for item in records if isinstance(item, dict)],
            key=lambda item: (item.get("automation_id", ""), item.get("revision", "")),
        )

    def get_withdrawal(self, careplan_id: str) -> dict[str, Any] | None:
        careplan = safe_component(careplan_id)
        return self._get_json(f"automations/{careplan}/withdrawn.json")

    def put_withdrawal(self, careplan_id: str, marker: dict[str, Any]) -> None:
        careplan = safe_component(careplan_id)
        key = f"automations/{careplan}/withdrawn.json"
        if self.artifact_store:
            self.artifact_store.put(key, marker)
        else:
            self._write_file(key, json.dumps(marker, sort_keys=True).encode())

    def get_idempotency(self, job_id: str) -> dict[str, Any] | None:
        import hashlib

        key = hashlib.sha256(job_id.encode()).hexdigest()
        return self._get_json(f"automations/idempotency/{key}.json")

    def put_idempotency(self, job_id: str, result: dict[str, Any]) -> None:
        import hashlib

        key = hashlib.sha256(job_id.encode()).hexdigest()
        path = f"automations/idempotency/{key}.json"
        if self.artifact_store:
            self.artifact_store.put(path, result)
        else:
            self._write_file(path, json.dumps(result, sort_keys=True).encode())

    def _get_json(self, key: str) -> dict[str, Any] | None:
        if self.artifact_store:
            try:
                result = self.artifact_store.get(f"{self.artifact_store.bucket}:{key}")
            except Exception as exc:
                status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if status == 404 or code in {"NoSuchKey", "404"}:
                    return None
                raise
            return result if isinstance(result, dict) else None
        path = self.directory / key
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _write_file(self, key: str, payload: bytes) -> None:
        path = self.directory / key
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
