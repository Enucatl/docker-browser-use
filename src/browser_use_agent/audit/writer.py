"""Append-only audit event writer with redaction and hash chaining."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from browser_use_agent.audit.hashchain import (
    GENESIS_PREV_HASH,
    compute_event_hash,
    event_hash_fields,
)
from browser_use_agent.db.models import AgentEvent, Run
from browser_use_agent.security.redaction import redact_for_audit, redact_text

Actor = Literal["agent", "human", "system"]

_VALID_ACTORS = frozenset({"agent", "human", "system"})


class AuditWriterError(ValueError):
    """Raised when an audit append cannot proceed."""


class AuditWriter:
    """Persist redacted, hash-chained events onto ``agent_events``.

    Same-run appends are serialized with ``SELECT FOR UPDATE`` on the ``runs``
    row so concurrent writers for one run cannot race on ``seq`` / ``prev_hash``.
    Distinct runs do not contend on each other's locks.

    Attributes:
        session: SQLAlchemy session used for locks and inserts.
    """

    def __init__(self, session: Session) -> None:
        """Bind the writer to a database session.

        Args:
            session: Active SQLAlchemy session (caller owns the transaction).
        """
        self.session = session

    def append(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: Actor = "system",
        step_id: uuid.UUID | None = None,
        parent_event_id: uuid.UUID | None = None,
        url: str | None = None,
        tab_id: str | None = None,
        duration_ms: int | None = None,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> AgentEvent:
        """Append one redacted, hash-chained event for ``run_id``.

        Always runs :func:`~browser_use_agent.security.redaction.redact_for_audit`
        on the payload before hashing or insert. Inserts only; never updates
        existing ``agent_events`` rows.

        Args:
            run_id: Owning run (must already exist).
            event_type: Logical event kind (e.g. ``task_received``).
            payload: Structured metadata; redacted then stored as JSONB.
            actor: Who caused the event (``agent``, ``human``, or ``system``).
            step_id: Optional step grouping within the run.
            parent_event_id: Optional causal parent event id.
            url: Optional page URL (text-redacted when present).
            tab_id: Optional browser tab identifier.
            duration_ms: Optional duration in milliseconds.
            occurred_at: Event time; defaults to UTC now.
            event_id: Optional primary key; a new UUID is generated otherwise.

        Returns:
            The inserted :class:`~browser_use_agent.db.models.AgentEvent` instance.

        Raises:
            AuditWriterError: When the run is missing or ``actor`` is invalid.
        """
        if actor not in _VALID_ACTORS:
            raise AuditWriterError(f"actor must be one of {sorted(_VALID_ACTORS)}, got {actor!r}")
        if not event_type:
            raise AuditWriterError("event_type must be a non-empty string")

        run = self.session.execute(
            select(Run).where(Run.id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run is None:
            raise AuditWriterError(f"run {run_id} does not exist")

        last = self.session.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == run_id)
            .order_by(AgentEvent.seq.desc())
            .limit(1)
        ).first()

        seq = 1 if last is None else int(last.seq) + 1
        prev_hash = GENESIS_PREV_HASH if last is None else (last.event_hash or GENESIS_PREV_HASH)
        when = occurred_at if occurred_at is not None else datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)

        raw_payload: Mapping[str, Any] = payload if payload is not None else {}
        redacted = redact_for_audit(raw_payload)
        if not isinstance(redacted, dict):
            redacted = {"value": redacted}

        safe_url = redact_text(url) if url is not None else None

        fields = event_hash_fields(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            occurred_at=when,
            actor=actor,
            metadata=redacted,
            prev_hash=prev_hash,
            step_id=step_id,
            parent_event_id=parent_event_id,
            url=safe_url,
            tab_id=tab_id,
            duration_ms=duration_ms,
        )
        digest = compute_event_hash(fields)

        row = AgentEvent(
            id=event_id or uuid.uuid4(),
            run_id=run_id,
            step_id=step_id,
            seq=seq,
            event_type=event_type,
            occurred_at=when,
            actor=actor,
            parent_event_id=parent_event_id,
            url=safe_url,
            tab_id=tab_id,
            duration_ms=duration_ms,
            metadata_=redacted,
            prev_hash=prev_hash,
            event_hash=digest,
        )
        self.session.add(row)
        self.session.flush()
        return row
