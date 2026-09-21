"""Tests for the filesystem artifact store and Zstd helpers."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from browser_use_agent.artifacts import (
    ArtifactNotFoundError,
    FilesystemArtifactStore,
    create_artifact_store,
    load_artifact_store_settings,
    zstd_compress,
    zstd_decode_json,
    zstd_decompress,
    zstd_encode_json,
)
from browser_use_agent.db.models import Artifact


def test_put_get_round_trip(tmp_path: Path) -> None:
    """Stored bytes are readable via the returned storage key."""
    store = FilesystemArtifactStore(tmp_path)
    payload = b"hello-artifact"
    result = store.put(payload, media_type="application/octet-stream", kind="download")
    assert result.wrote_blob is True
    assert result.size_bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert store.exists(result.storage_key)
    assert store.get(result.storage_key) == payload
    assert result.storage_key == FilesystemArtifactStore.storage_key_for(result.sha256)
    assert (tmp_path / result.storage_key).is_file()


def test_identical_bytes_dedupe(tmp_path: Path) -> None:
    """Identical payloads write one object and reuse the same storage key."""
    store = FilesystemArtifactStore(tmp_path)
    payload = b"same-bytes-twice"
    first = store.put(payload, media_type="text/plain", kind="state")
    second = store.put(payload, media_type="text/plain", kind="state")
    assert first.storage_key == second.storage_key
    assert first.sha256 == second.sha256
    assert first.wrote_blob is True
    assert second.wrote_blob is False
    # Only one blob file under the root.
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(files) == 1


def test_put_records_metadata_row(tmp_path: Path) -> None:
    """Optional session inserts an artifacts metadata row for the blob."""
    store = FilesystemArtifactStore(tmp_path)
    session = MagicMock()
    result = store.put(
        b"with-meta",
        media_type="text/plain",
        kind="download",
        session=session,
    )
    assert result.artifact_id is not None
    session.add.assert_called_once()
    session.flush.assert_called_once()
    row = session.add.call_args.args[0]
    assert isinstance(row, Artifact)
    assert row.id == result.artifact_id
    assert row.sha256 == result.sha256
    assert row.storage_key == result.storage_key
    assert row.kind == "download"
    assert row.media_type == "text/plain"
    assert row.size_bytes == len(b"with-meta")


def test_get_missing_key_raises(tmp_path: Path) -> None:
    """Missing keys raise ArtifactNotFoundError."""
    store = FilesystemArtifactStore(tmp_path)
    key = FilesystemArtifactStore.storage_key_for("0" * 64)
    assert store.exists(key) is False
    with pytest.raises(ArtifactNotFoundError):
        store.get(key)


def test_put_stream_dedupe(tmp_path: Path) -> None:
    """Streaming put hashes large payloads and still dedupes."""
    store = FilesystemArtifactStore(tmp_path)
    payload = b"x" * (1024 * 64 + 17)
    first = store.put_stream(
        io.BytesIO(payload),
        media_type="application/octet-stream",
        kind="download",
    )
    second = store.put_stream(
        io.BytesIO(payload),
        media_type="application/octet-stream",
        kind="download",
    )
    assert first.wrote_blob is True
    assert second.wrote_blob is False
    assert first.storage_key == second.storage_key
    assert store.get(first.storage_key) == payload


def test_zstd_round_trip() -> None:
    """Zstd compress/decompress restores original bytes."""
    raw = b"structured-browser-state" * 50
    compressed = zstd_compress(raw)
    assert compressed != raw
    assert zstd_decompress(compressed) == raw


def test_zstd_json_round_trip() -> None:
    """JSON + Zstd encode/decode preserves structure."""
    obj = {"url": "https://example.test", "nodes": [1, 2, 3], "ok": True}
    blob = zstd_encode_json(obj)
    assert zstd_decode_json(blob) == obj


def test_load_artifact_store_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARTIFACTS_ROOT overrides the default compose mount path."""
    monkeypatch.setenv("ARTIFACTS_ROOT", str(tmp_path))
    settings = load_artifact_store_settings()
    assert settings.root == tmp_path
    store = FilesystemArtifactStore.from_settings(settings)
    result = store.put(b"via-settings", media_type="text/plain", kind="download")
    assert store.get(result.storage_key) == b"via-settings"


def test_artifact_store_factory_defaults_to_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend switch defaults to filesystem storage."""
    monkeypatch.delenv("ARTIFACT_STORE", raising=False)
    monkeypatch.setenv("ARTIFACTS_ROOT", str(tmp_path))
    assert isinstance(create_artifact_store(), FilesystemArtifactStore)


def test_s3_settings_read_credentials_from_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 mode reads credentials from files rather than literal env values."""
    access_file = tmp_path / "access"
    secret_file = tmp_path / "secret"
    access_file.write_text("access\n", encoding="utf-8")
    secret_file.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_STORE", "s3")
    monkeypatch.setenv("ARTIFACT_S3_ACCESS_KEY_FILE", str(access_file))
    monkeypatch.setenv("ARTIFACT_S3_SECRET_KEY_FILE", str(secret_file))
    settings = load_artifact_store_settings()
    assert settings.backend == "s3"
    assert settings.access_key == "access"
    assert settings.secret_key == "secret"
