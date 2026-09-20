"""Helpers for redacting and optionally offloading large JSON payloads."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.security.redaction import redact_for_audit, redact_text

# Payloads at or above this UTF-8 byte size may be stored as artifacts.
DEFAULT_INLINE_LIMIT_BYTES = 32_768


def redact_mapping(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a redacted dict suitable for JSONB persistence.

    Args:
        payload: Raw mapping, or ``None`` for an empty object.

    Returns:
        Redacted dictionary (never ``None``).
    """
    raw: Mapping[str, Any] = payload if payload is not None else {}
    redacted = redact_for_audit(raw)
    if not isinstance(redacted, dict):
        return {"value": redacted}
    return redacted


def redact_optional_text(value: str | None) -> str | None:
    """Redact an optional string field.

    Args:
        value: Raw text or ``None``.

    Returns:
        Redacted text, or ``None`` when input was ``None``.
    """
    if value is None:
        return None
    return redact_text(value)


def maybe_offload_json(
    payload: Mapping[str, Any],
    *,
    session: Session,
    store: FilesystemArtifactStore | None,
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    kind: str,
    inline_limit_bytes: int = DEFAULT_INLINE_LIMIT_BYTES,
) -> tuple[dict[str, Any], uuid.UUID | None]:
    """Keep ``payload`` inline or store it as a JSON artifact when large.

    Always redacts before measuring size or writing. When ``store`` is ``None``
    or the payload is under the limit, returns the redacted mapping and no
    artifact id.

    Args:
        payload: Structured data to persist (will be redacted).
        session: DB session for optional artifact metadata.
        store: Artifact store; when ``None``, never offloads.
        run_id: Owning run for the artifact metadata row.
        event_id: Source audit event for the artifact metadata row.
        kind: Artifact kind label (e.g. ``model_request``, ``model_response``).
        inline_limit_bytes: UTF-8 size threshold for offloading.

    Returns:
        Tuple of (JSONB-safe meta dict, optional artifact id). When offloaded,
        meta is ``{"artifact_id": "<uuid>", "offloaded": True, "bytes": N}``.
    """
    redacted = redact_mapping(payload)
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if store is None or len(encoded) < inline_limit_bytes:
        return redacted, None

    result = store.put(
        encoded,
        media_type="application/json",
        kind=kind,
        run_id=run_id,
        event_id=event_id,
        metadata={"offloaded_from": kind, "bytes": len(encoded)},
        session=session,
    )
    artifact_id = result.artifact_id
    stub = {
        "artifact_id": str(artifact_id) if artifact_id is not None else result.storage_key,
        "offloaded": True,
        "bytes": len(encoded),
        "sha256": result.sha256,
    }
    return stub, artifact_id
