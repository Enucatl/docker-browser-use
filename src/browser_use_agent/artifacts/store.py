"""Filesystem content-addressed artifact store with SHA-256 dedupe."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from sqlalchemy.orm import Session

from browser_use_agent.artifacts.settings import (
    ArtifactStoreSettings,
    load_artifact_store_settings,
)
from browser_use_agent.db.models import Artifact

_HASH_CHUNK = 1024 * 1024


class ArtifactNotFoundError(KeyError):
    """Raised when a storage key or digest is missing from the store."""


def storage_key_for(sha256: str) -> str:
    """Return the relative key for a validated hex SHA-256 digest."""
    digest = sha256.lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"invalid sha256 digest: {sha256!r}")
    return f"{digest[:2]}/{digest[2:4]}/{digest}"


class ArtifactStore(Protocol):
    """Common interface for content-addressed artifact backends."""

    def exists(self, storage_key: str) -> bool:
        """Return whether a blob exists for ``storage_key``."""

    def get(self, storage_key: str) -> bytes:
        """Read a blob, raising :class:`ArtifactNotFoundError` when absent."""

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
        """Store bytes and return their content-addressed result."""

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
        """Store a binary stream and return its content-addressed result."""

    def delete(self, storage_key: str) -> bool:
        """Delete a blob and return whether it existed."""


@dataclass(frozen=True, slots=True)
class PutResult:
    """Outcome of storing bytes in the artifact store.

    Attributes:
        storage_key: Relative key under the store root (``ab/cd/<sha256>``).
        sha256: Hex digest of the stored content.
        size_bytes: Byte length of the stored content.
        wrote_blob: True when the blob was newly written to disk.
        artifact_id: Metadata row id when a DB session was provided.
    """

    storage_key: str
    sha256: str
    size_bytes: int
    wrote_blob: bool
    artifact_id: uuid.UUID | None = None


class FilesystemArtifactStore:
    """Content-addressed blob store under a mounted volume.

    Objects live at ``<root>/<aa>/<bb>/<sha256>`` where ``aa``/``bb`` are the
    first four hex characters of the SHA-256 digest. Identical payloads share
    one on-disk object; callers may attach many ``artifacts`` metadata rows to
    the same ``storage_key``.

    Attributes:
        root: Absolute filesystem root for blobs.
    """

    def __init__(self, root: Path | str) -> None:
        """Create a store rooted at ``root``.

        Args:
            root: Directory that will contain content-addressed objects.
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_settings(
        cls,
        settings: ArtifactStoreSettings | None = None,
    ) -> FilesystemArtifactStore:
        """Build a store from settings or the process environment.

        Args:
            settings: Explicit settings; loads ``ARTIFACTS_ROOT`` when omitted.

        Returns:
            Configured filesystem store.
        """
        resolved = settings if settings is not None else load_artifact_store_settings()
        return cls(resolved.root)

    @staticmethod
    def storage_key_for(sha256: str) -> str:
        """Return the relative storage key for a hex SHA-256 digest.

        Args:
            sha256: Lowercase or mixed-case hex digest (64 characters).

        Returns:
            Key of the form ``ab/cd/<sha256>``.

        Raises:
            ValueError: If ``sha256`` is not a 64-character hex string.
        """
        return storage_key_for(sha256)

    def path_for(self, storage_key: str) -> Path:
        """Resolve an absolute path for a storage key.

        Args:
            storage_key: Relative key under the store root.

        Returns:
            Absolute path (may not exist yet).
        """
        return self.root / storage_key

    def exists(self, storage_key: str) -> bool:
        """Return whether a blob exists for ``storage_key``.

        Args:
            storage_key: Relative key under the store root.

        Returns:
            True when the object file is present.
        """
        return self.path_for(storage_key).is_file()

    def get(self, storage_key: str) -> bytes:
        """Read blob bytes for ``storage_key``.

        Args:
            storage_key: Relative key under the store root.

        Returns:
            Stored content bytes.

        Raises:
            ArtifactNotFoundError: If no object exists for the key.
        """
        path = self.path_for(storage_key)
        if not path.is_file():
            raise ArtifactNotFoundError(storage_key)
        return path.read_bytes()

    def delete(self, storage_key: str) -> bool:
        """Delete a blob when present.

        Args:
            storage_key: Relative key under the store root.

        Returns:
            True when a blob was removed.
        """
        path = self.path_for(storage_key)
        if not path.is_file():
            return False
        path.unlink()
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
        """Store ``data`` with SHA-256 dedupe and optional metadata row.

        Args:
            data: Raw payload bytes (already compressed if desired).
            media_type: MIME type recorded in metadata.
            kind: Artifact kind (screenshot, state, download, …).
            run_id: Optional owning run for the metadata row.
            event_id: Optional source audit event for the metadata row.
            metadata: Extra JSON fields for the metadata row.
            session: When set, insert an ``artifacts`` row referencing the blob.

        Returns:
            Storage key, digest, size, whether the blob was newly written, and
            optional metadata id.
        """
        digest = hashlib.sha256(data).hexdigest()
        storage_key = self.storage_key_for(digest)
        wrote_blob = self._write_if_absent(storage_key, data)
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
        return PutResult(
            storage_key=storage_key,
            sha256=digest,
            size_bytes=len(data),
            wrote_blob=wrote_blob,
            artifact_id=artifact_id,
        )

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
        """Hash and store a potentially large stream with SHA-256 dedupe.

        Content is hashed while copying into a temporary file under the store
        root. If an object with the same digest already exists, the temp file is
        discarded.

        Args:
            stream: Readable binary stream of the payload.
            media_type: MIME type recorded in metadata.
            kind: Artifact kind (screenshot, state, download, …).
            run_id: Optional owning run for the metadata row.
            event_id: Optional source audit event for the metadata row.
            metadata: Extra JSON fields for the metadata row.
            session: When set, insert an ``artifacts`` row referencing the blob.

        Returns:
            Storage key, digest, size, whether the blob was newly written, and
            optional metadata id.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tmp:
                while True:
                    chunk = stream.read(_HASH_CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    tmp.write(chunk)
                    size += len(chunk)
            digest = hasher.hexdigest()
            storage_key = self.storage_key_for(digest)
            dest = self.path_for(storage_key)
            if dest.is_file():
                tmp_path.unlink(missing_ok=True)
                wrote_blob = False
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path, dest)
                wrote_blob = True
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

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
        return PutResult(
            storage_key=storage_key,
            sha256=digest,
            size_bytes=size,
            wrote_blob=wrote_blob,
            artifact_id=artifact_id,
        )

    def _write_if_absent(self, storage_key: str, data: bytes) -> bool:
        """Write ``data`` to ``storage_key`` unless the object already exists.

        Args:
            storage_key: Relative key under the store root.
            data: Payload bytes.

        Returns:
            True when a new blob was written; False on dedupe hit.
        """
        dest = self.path_for(storage_key)
        if dest.is_file():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".put-", dir=dest.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            os.replace(tmp_path, dest)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return True


def record_artifact_metadata(
    *,
    session: Session | None,
    sha256: str,
    storage_key: str,
    size_bytes: int,
    media_type: str,
    kind: str,
    run_id: uuid.UUID | None,
    event_id: uuid.UUID | None,
    metadata: dict[str, object] | None,
) -> uuid.UUID | None:
    """Insert an ``artifacts`` metadata row when a session is provided.

    Args:
        session: SQLAlchemy session, or ``None`` to skip DB writes.
        sha256: Content digest.
        storage_key: Logical content-addressed key.
        size_bytes: Stored size.
        media_type: MIME type.
        kind: Artifact kind.
        run_id: Optional run foreign key.
        event_id: Optional event foreign key.
        metadata: Extra JSONB fields.

    Returns:
        New artifact id, or ``None`` when no session was given.
    """
    if session is None:
        return None
    row = Artifact(
        id=uuid.uuid4(),
        run_id=run_id,
        event_id=event_id,
        sha256=sha256,
        kind=kind,
        media_type=media_type,
        size_bytes=size_bytes,
        storage_key=storage_key,
        metadata_=dict(metadata or {}),
    )
    session.add(row)
    session.flush()
    return row.id


def create_artifact_store(
    settings: ArtifactStoreSettings | None = None,
) -> ArtifactStore:
    """Build the configured artifact store, defaulting to the filesystem.

    Args:
        settings: Explicit settings; loads the process environment when omitted.

    Returns:
        A filesystem or S3-compatible artifact store.
    """
    resolved = settings if settings is not None else load_artifact_store_settings()
    if resolved.backend == "s3":
        from browser_use_agent.artifacts.s3_store import S3ArtifactStore

        return S3ArtifactStore.from_settings(resolved)
    return FilesystemArtifactStore(resolved.root)
