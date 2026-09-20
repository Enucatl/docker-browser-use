"""Per-run SHA-256 hash chaining for append-only audit events.

Canonicalization rules (stable across writers and verifiers)
-----------------------------------------------------------
The preimage for ``event_hash`` is a UTF-8 JSON document of the event fields
that participate in the chain. Rules:

* **Key sort:** object keys are sorted lexicographically at every nesting
  level (``json.dumps(..., sort_keys=True)``).
* **Separators:** compact form with ``","`` and ``":"`` (no extra whitespace).
* **UTC timestamps:** ``datetime`` values are normalized to timezone-aware UTC
  and encoded as ISO-8601 with a ``Z`` suffix (milliseconds trimmed of trailing
  zeros beyond seconds only when the fractional part is zero).
* **UUIDs:** encoded as canonical lowercase hyphenated strings.
* **Nulls:** JSON ``null``; omitted optional fields that are ``None`` are still
  present as ``null`` so the key set stays fixed.
* **Payload:** the redacted ``metadata`` mapping is included as-is after
  :func:`~browser_use_agent.security.redaction.redact_for_audit`.
* **Omit volatile / derived fields:** ``id`` and ``event_hash`` are never part
  of the preimage. ``prev_hash`` *is* included so each link binds to its
  predecessor.

Chain linkage
-------------
The first event in a run uses :data:`GENESIS_PREV_HASH` (64 zero hex digits)
as ``prev_hash``. Each subsequent event stores the previous row's
``event_hash``. Verification recomputes every hash and checks ``seq`` is a
contiguous ``1..N`` sequence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from browser_use_agent.db.models import AgentEvent

GENESIS_PREV_HASH = "0" * 64

# Fixed key set hashed for every event (values may be null).
_HASH_FIELD_KEYS = (
    "actor",
    "duration_ms",
    "event_type",
    "metadata",
    "occurred_at",
    "parent_event_id",
    "prev_hash",
    "run_id",
    "seq",
    "step_id",
    "tab_id",
    "url",
)


def _to_utc_z(value: datetime) -> str:
    """Format a datetime as UTC ISO-8601 with a ``Z`` suffix.

    Args:
        value: Aware or naive datetime (naive treated as UTC).

    Returns:
        Canonical UTC timestamp string.
    """
    if value.tzinfo is None:
        aware = value.replace(tzinfo=UTC)
    else:
        aware = value.astimezone(UTC)
    text = aware.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if text.endswith("+0000"):
        text = text[:-5] + "Z"
    return text


def canonicalize_value(value: Any) -> Any:
    """Convert a Python value into a JSON-serializable canonical form.

    Args:
        value: Arbitrary nested structure from an event field.

    Returns:
        A value suitable for :func:`json.dumps` with ``sort_keys=True``.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return _to_utc_z(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(k): canonicalize_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canonicalize_value(item) for item in value]
    if isinstance(value, bytes | bytearray):
        return value.hex()
    return str(value)


def canonicalize_event_fields(fields: Mapping[str, Any]) -> str:
    """Serialize hash-participating fields to canonical JSON text.

    Args:
        fields: Mapping that must include the fixed hash field keys.

    Returns:
        Compact, key-sorted JSON string.

    Raises:
        KeyError: When a required hash field key is missing.
    """
    payload = {key: canonicalize_value(fields[key]) for key in _HASH_FIELD_KEYS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_event_hash(fields: Mapping[str, Any]) -> str:
    """Compute the SHA-256 hex digest for one event's chain preimage.

    Args:
        fields: Hash fields including ``prev_hash`` and redacted ``metadata``.

    Returns:
        Lowercase hex SHA-256 digest.
    """
    preimage = canonicalize_event_fields(fields)
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def event_hash_fields(
    *,
    run_id: uuid.UUID,
    seq: int,
    event_type: str,
    occurred_at: datetime,
    actor: str,
    metadata: Mapping[str, Any],
    prev_hash: str,
    step_id: uuid.UUID | None = None,
    parent_event_id: uuid.UUID | None = None,
    url: str | None = None,
    tab_id: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Build the fixed-key mapping used as the hash preimage.

    Args:
        run_id: Owning run.
        seq: Monotonic sequence number within the run.
        event_type: Logical event kind.
        occurred_at: Event timestamp.
        actor: ``agent``, ``human``, or ``system``.
        metadata: Redacted JSON-compatible payload.
        prev_hash: Previous event hash or :data:`GENESIS_PREV_HASH`.
        step_id: Optional step grouping.
        parent_event_id: Optional causal parent.
        url: Optional page URL.
        tab_id: Optional tab identifier.
        duration_ms: Optional duration in milliseconds.

    Returns:
        Mapping with exactly the keys hashed by :func:`compute_event_hash`.
    """
    return {
        "actor": actor,
        "duration_ms": duration_ms,
        "event_type": event_type,
        "metadata": dict(metadata),
        "occurred_at": occurred_at,
        "parent_event_id": parent_event_id,
        "prev_hash": prev_hash,
        "run_id": run_id,
        "seq": seq,
        "step_id": step_id,
        "tab_id": tab_id,
        "url": url,
    }


def _row_as_hash_fields(event: AgentEvent) -> dict[str, Any]:
    """Project an ``AgentEvent`` ORM row into hash-field mapping form.

    Args:
        event: Persisted or pending event row.

    Returns:
        Hash field mapping (uses stored ``prev_hash`` or genesis).
    """
    return event_hash_fields(
        run_id=event.run_id,
        seq=int(event.seq),
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        actor=event.actor,
        metadata=event.metadata_ or {},
        prev_hash=event.prev_hash or GENESIS_PREV_HASH,
        step_id=event.step_id,
        parent_event_id=event.parent_event_id,
        url=event.url,
        tab_id=event.tab_id,
        duration_ms=event.duration_ms,
    )


def verify_events_chain(events: Sequence[AgentEvent]) -> bool:
    """Return True when ``events`` form a valid contiguous hash chain.

    Args:
        events: Events for one run ordered by ascending ``seq``.

    Returns:
        True when sequence numbers, ``prev_hash`` links, and digests match.
    """
    if not events:
        return True

    expected_prev = GENESIS_PREV_HASH
    for index, event in enumerate(events, start=1):
        if int(event.seq) != index:
            return False
        stored_prev = event.prev_hash or GENESIS_PREV_HASH
        if stored_prev != expected_prev:
            return False
        if not event.event_hash:
            return False
        recomputed = compute_event_hash(_row_as_hash_fields(event))
        if recomputed != event.event_hash:
            return False
        expected_prev = event.event_hash
    return True


def verify_run_chain(session: Session, run_id: uuid.UUID) -> bool:
    """Verify the hash chain for all events belonging to ``run_id``.

    Args:
        session: SQLAlchemy session bound to the audit database.
        run_id: Run whose ``agent_events`` rows should be checked.

    Returns:
        True when the persisted chain is intact; False on any break.
    """
    events = session.scalars(
        select(AgentEvent).where(AgentEvent.run_id == run_id).order_by(AgentEvent.seq.asc())
    ).all()
    return verify_events_chain(events)
