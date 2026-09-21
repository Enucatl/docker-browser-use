"""Artifact store settings from the environment and Docker secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from browser_use_agent.db.settings import read_secret_file

DEFAULT_ARTIFACTS_ROOT = Path("/var/lib/browser-use/artifacts")


@dataclass(frozen=True, slots=True)
class ArtifactStoreSettings:
    """Configuration for filesystem or S3-compatible artifact blobs.

    Attributes:
        backend: ``fs`` (the default) or ``s3``.
        root: Directory that holds ``ab/cd/<sha256>`` objects.
        endpoint_url: S3-compatible endpoint, normally the internal MinIO URL.
        bucket: S3 bucket for artifact objects.
        prefix: Optional prefix before content-addressed object keys.
        region: S3 signing region.
        access_key: Access key read from a Docker secret file.
        secret_key: Secret key read from a Docker secret file.
        sse: Optional S3 server-side encryption mode.
    """

    root: Path = DEFAULT_ARTIFACTS_ROOT
    backend: Literal["fs", "s3"] = "fs"
    endpoint_url: str = "http://minio:9000"
    bucket: str = "browser-use-artifacts"
    prefix: str = ""
    region: str = "us-east-1"
    access_key: str | None = None
    secret_key: str | None = None
    sse: str | None = None


def load_artifact_store_settings() -> ArtifactStoreSettings:
    """Load artifact store settings from environment variables.

    Returns:
        Immutable artifact store settings. S3 credentials are read only from
        ``ARTIFACT_S3_ACCESS_KEY_FILE`` and ``ARTIFACT_S3_SECRET_KEY_FILE``.
    """
    raw = os.environ.get("ARTIFACTS_ROOT")
    root = Path(raw) if raw else DEFAULT_ARTIFACTS_ROOT
    backend = os.environ.get("ARTIFACT_STORE", "fs").strip().lower()
    if backend not in {"fs", "s3"}:
        raise ValueError("ARTIFACT_STORE must be 'fs' or 's3'")
    return ArtifactStoreSettings(
        backend=backend,
        root=root,
        endpoint_url=os.environ.get("ARTIFACT_S3_ENDPOINT_URL", "http://minio:9000").rstrip("/"),
        bucket=os.environ.get("ARTIFACT_S3_BUCKET", "browser-use-artifacts"),
        prefix=os.environ.get("ARTIFACT_S3_PREFIX", "").strip("/"),
        region=os.environ.get("ARTIFACT_S3_REGION", "us-east-1"),
        access_key=read_secret_file("ARTIFACT_S3_ACCESS_KEY") if backend == "s3" else None,
        secret_key=read_secret_file("ARTIFACT_S3_SECRET_KEY") if backend == "s3" else None,
        sse=os.environ.get("ARTIFACT_S3_SSE", "").strip() or None,
    )
