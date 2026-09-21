"""S3-compatible content-addressed artifact store."""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from typing import Any, BinaryIO

import boto3
from sqlalchemy.orm import Session

from browser_use_agent.artifacts.settings import ArtifactStoreSettings, load_artifact_store_settings
from browser_use_agent.artifacts.store import (
    ArtifactNotFoundError,
    PutResult,
    record_artifact_metadata,
    storage_key_for,
)

_HASH_CHUNK = 1024 * 1024


def _missing_error(error: Exception) -> bool:
    """Return whether a botocore error represents a missing S3 object."""
    response = getattr(error, "response", {})
    code = str(response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


class S3ArtifactStore:
    """Content-addressed blobs in an S3-compatible bucket.

    The public storage key remains ``ab/cd/<sha256>``. ``prefix`` is applied
    only to the object key so PostgreSQL metadata stays backend-independent.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        prefix: str = "",
        region: str = "us-east-1",
        access_key: str | None,
        secret_key: str | None,
        sse: str | None = None,
        client: Any | None = None,
    ) -> None:
        """Create an S3 store and ensure its bucket exists.

        Args:
            endpoint_url: S3-compatible API endpoint.
            bucket: Bucket containing artifact objects.
            prefix: Optional object-key prefix.
            region: S3 signing region.
            access_key: Access key from a secret file.
            secret_key: Secret key from a secret file.
            sse: Optional server-side encryption mode such as ``AES256``.
            client: Existing S3 client, primarily for tests.

        Raises:
            ValueError: If either S3 credential is missing.
        """
        if not access_key or not secret_key:
            raise ValueError("S3 artifact store requires access and secret key files")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        self.sse = sse
        self._ensure_bucket()

    @classmethod
    def from_settings(cls, settings: ArtifactStoreSettings | None = None) -> S3ArtifactStore:
        """Build an S3 store from explicit settings or the environment."""
        resolved = settings if settings is not None else load_artifact_store_settings()
        return cls(
            endpoint_url=resolved.endpoint_url,
            bucket=resolved.bucket,
            prefix=resolved.prefix,
            region=resolved.region,
            access_key=resolved.access_key,
            secret_key=resolved.secret_key,
            sse=resolved.sse,
        )

    @staticmethod
    def storage_key_for(sha256: str) -> str:
        """Return the standard logical key for a SHA-256 digest."""
        return storage_key_for(sha256)

    def _object_key(self, storage_key: str) -> str:
        """Apply the configured prefix to a logical storage key."""
        return f"{self.prefix}/{storage_key}" if self.prefix else storage_key

    def _ensure_bucket(self) -> None:
        """Create the bucket when an S3-compatible service has not got it."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as error:
            if not _missing_error(error):
                raise
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception as create_error:
                if not _missing_error(create_error) and "AlreadyOwned" not in str(create_error):
                    raise

    def exists(self, storage_key: str) -> bool:
        """Return whether an object exists for ``storage_key``."""
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._object_key(storage_key))
        except Exception as error:
            if _missing_error(error):
                return False
            raise
        return True

    def get(self, storage_key: str) -> bytes:
        """Read an object, raising ``ArtifactNotFoundError`` when absent."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(storage_key))
        except Exception as error:
            if _missing_error(error):
                raise ArtifactNotFoundError(storage_key) from error
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def delete(self, storage_key: str) -> bool:
        """Delete an object when present."""
        if not self.exists(storage_key):
            return False
        self.client.delete_object(Bucket=self.bucket, Key=self._object_key(storage_key))
        return True

    def put(
        self,
        data: bytes,
        *,
        media_type: str,
        kind: str,
        run_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        metadata: dict[str, object] | None = None,
        session: Session | None = None,
    ) -> PutResult:
        """Store bytes with SHA-256 deduplication and optional metadata."""
        digest = hashlib.sha256(data).hexdigest()
        storage_key = self.storage_key_for(digest)
        wrote_blob = False
        if not self.exists(storage_key):
            self._put_object(storage_key, data, media_type=media_type)
            wrote_blob = True
        artifact_id = record_artifact_metadata(
            session=session,
            sha256=digest,
            storage_key=storage_key,
            size_bytes=len(data),
            media_type=media_type,
            kind=kind,
            run_id=run_id,
            event_id=event_id,
            metadata=metadata,
        )
        return PutResult(storage_key, digest, len(data), wrote_blob, artifact_id)

    def put_stream(
        self,
        stream: BinaryIO,
        *,
        media_type: str,
        kind: str,
        run_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        metadata: dict[str, object] | None = None,
        session: Session | None = None,
    ) -> PutResult:
        """Hash and upload a stream without holding the full payload in memory."""
        hasher = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile() as temp:
            while chunk := stream.read(_HASH_CHUNK):
                hasher.update(chunk)
                temp.write(chunk)
                size += len(chunk)
            digest = hasher.hexdigest()
            storage_key = self.storage_key_for(digest)
            wrote_blob = False
            if not self.exists(storage_key):
                temp.seek(0)
                self._put_object(storage_key, temp, media_type=media_type)
                wrote_blob = True
        artifact_id = record_artifact_metadata(
            session=session,
            sha256=digest,
            storage_key=storage_key,
            size_bytes=size,
            media_type=media_type,
            kind=kind,
            run_id=run_id,
            event_id=event_id,
            metadata=metadata,
        )
        return PutResult(storage_key, digest, size, wrote_blob, artifact_id)

    def _put_object(self, storage_key: str, body: Any, *, media_type: str) -> None:
        """Upload one object with content type and optional SSE."""
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._object_key(storage_key),
            "Body": body,
            "ContentType": media_type,
        }
        if self.sse:
            kwargs["ServerSideEncryption"] = self.sse
        self.client.put_object(**kwargs)
