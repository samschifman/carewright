"""Retrieve by externally retained receipt and verify the entire artifact graph."""

from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlparse

from mlflow import MlflowClient

from .models import RunEnvelope
from .security import canonical_bytes, digest, traced
from .storage import PersistenceError, safe_name, verify_bytes


@traced
def read_reference(ref, *, tracking_uri=None, client=None):
    safe_name(ref.name)
    uri = urlparse(ref.uri)
    try:
        if uri.scheme == "file" and not uri.netloc:
            return verify_bytes(Path(unquote(uri.path)).read_bytes(), ref)
        if uri.scheme == "runs":
            if not tracking_uri and client is None:
                raise PersistenceError(
                    "MLFLOW_TRACKING_URI is required to retrieve remote artifacts"
                )
            parts = uri.path.lstrip("/").split("/", 1)
            if len(parts) != 2 or parts[1] != f"prompt-eval/{ref.name}":
                raise PersistenceError("Invalid remote artifact identity")
            c = client or MlflowClient(tracking_uri=tracking_uri)
            with TemporaryDirectory(prefix="prompt-eval-retrieve-") as folder:
                path = c.download_artifacts(parts[0], parts[1], folder)
                return verify_bytes(Path(path).read_bytes(), ref)
        raise PersistenceError("Unsupported artifact URI scheme")
    except PersistenceError:
        raise
    except Exception as exc:
        raise PersistenceError("Artifact retrieval failed") from exc


@traced
def retrieve(receipt, *, tracking_uri=None, client=None, export_dir=None):
    def read(ref):
        if export_dir is not None:
            return verify_bytes((Path(export_dir) / safe_name(ref.name)).read_bytes(), ref)
        return read_reference(ref, tracking_uri=tracking_uri, client=client)

    if receipt.envelope.name != "envelope.json":
        raise PersistenceError("Receipt must identify an envelope")
    env = RunEnvelope.model_validate_json(read(receipt.envelope))
    if env.run_id != receipt.run_id or env.mlflow_run_id != receipt.mlflow_run_id:
        raise PersistenceError("Receipt and envelope identity mismatch")
    if env.config_digest != digest(env.config):
        raise PersistenceError("Configuration digest mismatch")
    base = receipt.envelope.uri.rsplit("/", 1)[0]
    names = set()
    for ref in env.artifacts:
        if ref.name in names or ref.uri != f"{base}/{ref.name}":
            raise PersistenceError("Artifact list contains duplicate or foreign identities")
        names.add(ref.name)
        read(ref)
    return env


@traced
def export_run(receipt, destination, *, tracking_uri=None, client=None):
    """Copy verified bytes without changing any original identity or digest."""
    env = retrieve(receipt, tracking_uri=tracking_uri, client=client)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for ref in (*env.artifacts, receipt.envelope):
        data = read_reference(ref, tracking_uri=tracking_uri, client=client)
        path = destination / safe_name(ref.name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o600)
    path = destination / "receipt.json"
    with path.open("xb") as stream:
        stream.write(canonical_bytes(receipt))
    path.chmod(0o600)
    return receipt
