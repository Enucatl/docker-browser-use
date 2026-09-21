"""Tests for the mocked S3 artifact backend."""

from __future__ import annotations

import hashlib
import io
from typing import ClassVar

import pytest

from browser_use_agent.artifacts.s3_store import S3ArtifactStore
from browser_use_agent.artifacts.store import ArtifactNotFoundError


class MissingObject(Exception):
    """Minimal botocore-like missing-object error for the fake client."""

    response: ClassVar[dict[str, dict[str, str]]] = {"Error": {"Code": "404"}}


class FakeS3:
    """Small in-memory S3 client for store behavior tests."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str | None]] = {}
        self.buckets: set[str] = set()

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket not in self.buckets:
            raise MissingObject

    def create_bucket(self, *, Bucket: str) -> None:
        self.buckets.add(Bucket)

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise MissingObject

    def put_object(self, *, Bucket: str, Key: str, Body, ContentType: str, **kwargs) -> None:
        self.objects[(Bucket, Key)] = (
            Body.read() if hasattr(Body, "read") else Body,
            kwargs.get("ServerSideEncryption"),
        )

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        try:
            payload, _ = self.objects[(Bucket, Key)]
        except KeyError as error:
            raise MissingObject from error
        return {"Body": io.BytesIO(payload)}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)


def test_s3_store_round_trip_dedupes_and_uses_sha_key() -> None:
    """S3 objects use the filesystem-compatible logical content key."""
    client = FakeS3()
    store = S3ArtifactStore(
        endpoint_url="http://minio:9000",
        bucket="artifacts",
        prefix="prod",
        access_key="access",
        secret_key="secret",
        sse="AES256",
        client=client,
    )
    payload = b"s3-artifact"
    first = store.put(payload, media_type="text/plain", kind="download")
    second = store.put(payload, media_type="text/plain", kind="download")

    assert first.sha256 == hashlib.sha256(payload).hexdigest()
    assert first.storage_key == S3ArtifactStore.storage_key_for(first.sha256)
    assert first.wrote_blob is True
    assert second.wrote_blob is False
    assert store.get(first.storage_key) == payload
    assert client.objects[("artifacts", f"prod/{first.storage_key}")][1] == "AES256"


def test_s3_store_stream_and_missing() -> None:
    """Streams round-trip and missing objects use the shared error."""
    store = S3ArtifactStore(
        endpoint_url="http://minio:9000",
        bucket="artifacts",
        access_key="access",
        secret_key="secret",
        client=FakeS3(),
    )
    result = store.put_stream(
        io.BytesIO(b"streamed"), media_type="application/octet-stream", kind="download"
    )
    assert store.get(result.storage_key) == b"streamed"
    assert store.delete(result.storage_key) is True
    with pytest.raises(ArtifactNotFoundError):
        store.get(result.storage_key)
