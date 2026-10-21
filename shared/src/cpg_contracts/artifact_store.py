"""S3-compatible artifact store for large payload transfer between pods.

Two buckets enforce data classification:
- cpg-artifacts: non-PHI clinical content (recommendations)
- cpg-phi: patient-specific data (IPS bundles, planning briefs, FHIR bundles)

In distributed mode (ARTIFACT_STORE_URL set), pods store large payloads
in MinIO/S3 and pass references through the SonataFlow workflow.
In local mode (no URL set), get_artifact_store() returns None and
services pass data inline.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_BUCKET_ARTIFACTS = "cpg-artifacts"
_BUCKET_PHI = "cpg-phi"


class ArtifactStore:
    """S3-compatible store for pipeline artifacts."""

    def __init__(
        self,
        endpoint_url: str,
        bucket: str = _BUCKET_ARTIFACTS,
        access_key: str = "minioadmin",
        secret_key: str = "minioadmin",
    ):
        self.bucket = bucket
        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                config=Config(signature_version="s3v4"),
                region_name="us-east-1",
            )
            self._ensure_bucket()
        return self._client

    def _ensure_bucket(self):
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self.bucket)
                logger.info("Created artifact bucket: %s", self.bucket)
            except Exception:
                pass

    def _put_via_presigned(self, key: str, body: bytes, content_type: str) -> None:
        """PUT using a presigned URL + requests to avoid proxy incompatibility with boto3."""
        import requests as _requests

        url = self._get_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=300,
        )
        resp = _requests.put(url, data=body, timeout=30)
        resp.raise_for_status()

    def put(self, key: str, data: dict | list) -> str:
        """Store a JSON artifact. Returns a qualified ref: 'bucket:key'."""
        self._put_via_presigned(key, json.dumps(data).encode(), "application/json")
        ref = f"{self.bucket}:{key}"
        logger.debug("Stored artifact: %s", ref)
        return ref

    def get_raw(self, ref: str) -> bytes:
        """Fetch raw bytes from the artifact store (for binary files like PDFs)."""
        if ":" in ref and not ref.startswith("s3://"):
            bucket, key = ref.split(":", 1)
        else:
            bucket, key = self.bucket, ref
        obj = self._get_client().get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()

    def get_raw_in_bucket(self, bucket: str, key: str) -> bytes:
        """Fetch raw bytes using a literal key within this store's bucket."""
        if bucket != self.bucket:
            raise ValueError("Requested bucket does not match configured artifact store")
        obj = self._get_client().get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()

    def put_raw(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Store raw bytes. Returns a qualified ref: 'bucket:key'."""
        self._put_via_presigned(key, data, content_type)
        return f"{self.bucket}:{key}"

    def get(self, ref: str) -> dict | list:
        """Fetch a JSON artifact by qualified ref ('bucket:key') or plain key."""
        if ":" in ref and not ref.startswith("s3://"):
            bucket, key = ref.split(":", 1)
        else:
            bucket, key = self.bucket, ref
        obj = self._get_client().get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read().decode())
        logger.debug("Fetched artifact: %s:%s", bucket, key)
        return data


def _build_store(url: str, bucket: str) -> ArtifactStore | None:
    if not url:
        return None
    return ArtifactStore(
        endpoint_url=url,
        bucket=bucket,
        access_key=os.environ.get("ARTIFACT_STORE_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("ARTIFACT_STORE_SECRET_KEY", "minioadmin"),
    )


def get_artifact_store() -> ArtifactStore | None:
    """Non-PHI artifact store (recommendations). Returns None in local mode."""
    url = os.environ.get("ARTIFACT_STORE_URL", "")
    bucket = os.environ.get("ARTIFACT_STORE_BUCKET", _BUCKET_ARTIFACTS)
    return _build_store(url, bucket)


def get_phi_store() -> ArtifactStore | None:
    """PHI artifact store (patient bundles, care plans). Returns None in local mode."""
    url = os.environ.get("ARTIFACT_STORE_URL", "")
    bucket = os.environ.get("PHI_STORE_BUCKET", _BUCKET_PHI)
    return _build_store(url, bucket)


def resolve_ref(data: dict, field: str, store: ArtifactStore | None) -> dict | list:
    """Resolve a field that may be inline or a _ref.

    The ref format is 'bucket:key' — the store's get() parses the bucket
    from the ref, so it works across buckets.
    """
    ref_key = f"{field}_ref"
    if store and ref_key in data:
        return store.get(data[ref_key])
    return data.get(field, data.get(ref_key, {}))


def store_artifact(
    store: ArtifactStore | None, key: str, data: dict | list
) -> tuple[dict | list | None, str | None]:
    """Store data in the artifact store if available.

    Returns (None, ref) if stored, or (data, None) if inline.
    On failure, logs a warning and falls back to inline.
    """
    if store:
        try:
            ref = store.put(key, data)
            return None, ref
        except Exception as e:
            logger.warning("Artifact store unavailable, falling back to inline: %s", e)
    return data, None
