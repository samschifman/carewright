"""Single-writer, append-only artifact stores with verified reads and writes."""

import hashlib
import ipaddress
import re
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from uuid import uuid4

from mlflow import MlflowClient

from .models import ArtifactRef
from .security import Redactor, canonical_bytes, traced


class PersistenceError(RuntimeError):
    pass


def safe_name(name):
    if (
        not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
        or any(part in ("", ".", "..") for part in name.split("/"))
        or name.startswith("/")
    ):
        raise PersistenceError("Unsafe artifact name")
    return name


def verify_bytes(data, ref):
    if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise PersistenceError("Artifact size or digest mismatch")
    return data


class LocalStore:
    official = False
    mlflow_run_id = None

    def __init__(self, root: Path, run_id: str, redactor: Redactor):
        safe_name(run_id)
        if "/" in run_id:
            raise PersistenceError("Invalid run ID")
        self.run_id = run_id
        self.redactor = redactor
        self.path = Path(root).resolve() / run_id
        try:
            self.path.mkdir(parents=True, exist_ok=False, mode=0o700)
        except OSError as exc:
            raise PersistenceError("Run directory already exists or is inaccessible") from exc
        self.artifact_uri = self.path.as_uri()
        self.refs = []

    @traced
    def put(self, name, value):
        safe_name(name)
        data = canonical_bytes(self.redactor.clean(value))
        target = self.path / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not target.resolve().is_relative_to(self.path):
                raise PersistenceError("Artifact escapes run directory")
            with target.open("xb") as f:
                f.write(data)
                f.flush()
                import os

                os.fsync(f.fileno())
            target.chmod(0o600)
        except OSError as exc:
            raise PersistenceError("Artifact exists or cannot be persisted") from exc
        ref = ArtifactRef(
            name=name, uri=target.as_uri(), sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        self.read(ref)
        self.refs.append(ref)
        return ref

    @traced
    def read(self, ref):
        target = (self.path / safe_name(ref.name)).resolve()
        if not target.is_relative_to(self.path) or target.as_uri() != ref.uri:
            raise PersistenceError("Artifact identity mismatch")
        return verify_bytes(target.read_bytes(), ref)

    @traced
    def preflight(self):
        return self.put("preflight/canary.json", {"canary": str(uuid4())})

    def finish(self, success):
        pass


class MlflowStore:
    @traced
    def __init__(
        self,
        tracking_uri,
        experiment,
        run_id,
        redactor,
        *,
        official=False,
        approved_uri=None,
        client=None,
    ):
        parsed = urlparse(tracking_uri)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PersistenceError("Tracking URI must not contain credentials or query parameters")
        if official:
            host = parsed.hostname or ""
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host in ("localhost", "mlflow") or host.endswith(".localhost")
            if parsed.scheme != "https" or not host or local or tracking_uri != approved_uri:
                raise PersistenceError(
                    "Official runs require an explicitly approved HTTPS RHOAI URI"
                )
        self.tracking_uri = tracking_uri
        self.official = official
        self.run_id = run_id
        self.redactor = redactor
        self.client = client or MlflowClient(tracking_uri=tracking_uri)
        self.refs = []
        self.written = set()
        try:
            exp = self.client.get_experiment_by_name(experiment)
            experiment_id = exp.experiment_id if exp else self.client.create_experiment(experiment)
            self.experiment_id = experiment_id
            run = self.client.create_run(
                experiment_id,
                tags={
                    "prompt_eval.run_id": run_id,
                    "prompt_eval.schema": "1.0.0",
                    "prompt_eval.official": str(official).lower(),
                },
            )
            self.mlflow_run_id = run.info.run_id
            self.artifact_uri = run.info.artifact_uri
        except Exception as exc:
            raise PersistenceError("Cannot initialize MLflow run") from exc

    @traced
    def put(self, name, value):
        safe_name(name)
        remote = f"prompt-eval/{name}"
        parent = str(PurePosixPath(remote).parent)
        try:
            if remote in self.written or any(
                x.path == remote for x in self.client.list_artifacts(self.mlflow_run_id, parent)
            ):
                raise PersistenceError("Artifact overwrite rejected")
            # Reserve before upload: an ambiguous network failure is never retried as an overwrite.
            self.written.add(remote)
            data = canonical_bytes(self.redactor.clean(value))
            with TemporaryDirectory(prefix="prompt-eval-") as folder:
                path = Path(folder) / PurePosixPath(name).name
                path.write_bytes(data)
                path.chmod(0o600)
                self.client.log_artifact(self.mlflow_run_id, str(path), parent)
            ref = ArtifactRef(
                name=name,
                uri=f"runs:/{self.mlflow_run_id}/{remote}",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
            self.read(ref)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("MLflow artifact upload/download failed") from exc
        self.refs.append(ref)
        return ref

    @traced
    def read(self, ref):
        remote = f"prompt-eval/{safe_name(ref.name)}"
        if ref.uri != f"runs:/{self.mlflow_run_id}/{remote}":
            raise PersistenceError("Artifact identity mismatch")
        try:
            with TemporaryDirectory(prefix="prompt-eval-read-") as folder:
                path = self.client.download_artifacts(self.mlflow_run_id, remote, folder)
                return verify_bytes(Path(path).read_bytes(), ref)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError("MLflow artifact retrieval failed") from exc

    @traced
    def preflight(self):
        return self.put("preflight/canary.json", {"canary": str(uuid4())})

    @traced
    def finish(self, success):
        try:
            self.client.set_terminated(self.mlflow_run_id, "FINISHED" if success else "FAILED")
        except Exception as exc:
            raise PersistenceError("Cannot finalize MLflow run") from exc
