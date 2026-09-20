"""SQLAlchemy models for the event-sourcing audit schema.

``agent_events`` is the canonical immutable timeline. Normalized helper tables
exist for analysis and do not replace the event stream. In normal operation,
``agent_events`` rows are never updated or deleted (enforced by DB triggers).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for audit schema models."""


class Run(Base):
    """One agent execution identified by a UUID.

    Attributes:
        id: Stable run identifier.
        goal: Operator natural-language goal for the run.
        status: Lifecycle status string (see T010 enum).
        profile_id: Optional browser profile key.
        created_at: When the run row was inserted.
        updated_at: Last status/metadata change.
        started_at: When execution began, if ever.
        finished_at: When the run reached a terminal status.
        metadata_: Searchable JSONB bag for non-blob fields.
        events: Related append-only audit events.
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_status", "status"),
        Index("ix_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    profile_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    events: Mapped[list[AgentEvent]] = relationship(back_populates="run")


class AgentEvent(Base):
    """Immutable append-only audit event on the canonical timeline.

    Rows must never be updated or deleted in normal operation. Hash-chain
    columns (``prev_hash``, ``event_hash``) are filled by the audit writer (T009).

    Attributes:
        id: Event primary key.
        run_id: Owning run.
        step_id: Optional step grouping within a run.
        seq: Monotonic sequence number per run.
        event_type: Logical event kind (e.g. ``task_received``).
        occurred_at: Event timestamp (timestamptz).
        actor: Who caused the event (``agent``, ``human``, ``system``).
        parent_event_id: Optional causal parent event.
        url: Page URL when relevant.
        tab_id: Browser tab identifier when relevant.
        duration_ms: Optional duration in milliseconds.
        metadata_: Searchable JSONB payload (no large blobs).
        prev_hash: Previous event hash in the chain (T009).
        event_hash: This event's content hash (T009).
    """

    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_events_run_seq"),
        Index("ix_agent_events_run_id", "run_id"),
        Index("ix_agent_events_occurred_at", "occurred_at"),
        Index("ix_agent_events_event_type", "event_type"),
        Index("ix_agent_events_event_hash", "event_hash"),
        Index("ix_agent_events_prev_hash", "prev_hash"),
        Index("ix_agent_events_run_id_occurred_at", "run_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'system'"))
    parent_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    tab_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    prev_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="events")


class AgentDecision(Base):
    """Normalized decision row linked to an audit event.

    Attributes:
        id: Decision primary key.
        run_id: Owning run.
        event_id: Source ``agent_events`` row.
        step_id: Optional step grouping.
        action: Chosen action name or kind.
        rationale: Short human-readable reason when available.
        payload: Structured decision details (JSONB).
        created_at: Insert timestamp.
    """

    __tablename__ = "agent_decisions"
    __table_args__ = (Index("ix_agent_decisions_run_id", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ModelCall(Base):
    """Normalized model request/response trace (redacted payloads).

    Attributes:
        id: Model-call primary key.
        run_id: Owning run.
        event_id: Source audit event.
        step_id: Optional step grouping (mirrors ``agent_events.step_id``).
        event_seq: Denormalized ``agent_events.seq`` for range queries.
        provider: Provider name when known.
        model_name: Model identifier.
        call_kind: e.g. ``jev``, ``text_llm``.
        status: ``ok`` / ``failed`` when known.
        retries: Retry count before this attempt succeeded or failed.
        request_id: Provider / HTTP request id when known.
        prompt_tokens: Optional input token count.
        completion_tokens: Optional output token count.
        latency_ms: End-to-end latency.
        cost_usd: Optional estimated or billed cost in USD.
        request_meta: Redacted request metadata (or artifact refs).
        response_meta: Redacted response metadata (or artifact refs).
        request_artifact_id: Optional artifact holding a large request payload.
        response_artifact_id: Optional artifact holding a large response payload.
        created_at: Insert timestamp.
    """

    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_run_id", "run_id"),
        Index("ix_model_calls_created_at", "created_at"),
        Index("ix_model_calls_event_id", "event_id"),
        Index("ix_model_calls_event_seq", "run_id", "event_seq"),
        Index("ix_model_calls_step_id", "step_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    request_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    response_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    request_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    response_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class BrowserAction(Base):
    """Normalized browser action (click, type stub, navigate, etc.).

    Attributes:
        id: Action primary key.
        run_id: Owning run.
        event_id: Source audit event.
        step_id: Optional step grouping (mirrors ``agent_events.step_id``).
        event_seq: Denormalized ``agent_events.seq`` for range queries.
        action_type: Action verb/kind.
        status: e.g. ``requested``, ``completed``, ``failed``.
        target: Selector or semantic target description.
        url: Page URL when relevant.
        title: Document title when known.
        tab_id: Browser tab identifier when relevant.
        element_index: Snapshot-local interactable index when relevant.
        page_changed: Whether the page URL/title changed after the action.
        result: Short outcome summary (redacted).
        duration_ms: Action duration.
        before_artifact_id: Optional pre-action state artifact.
        after_artifact_id: Optional post-action state artifact.
        metadata_: Extra searchable fields (role, name, bounds, ids, …).
        created_at: Insert timestamp.
    """

    __tablename__ = "browser_actions"
    __table_args__ = (
        Index("ix_browser_actions_run_id", "run_id"),
        Index("ix_browser_actions_created_at", "created_at"),
        Index("ix_browser_actions_event_id", "event_id"),
        Index("ix_browser_actions_event_seq", "run_id", "event_seq"),
        Index("ix_browser_actions_step_id", "step_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    tab_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    element_index: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_changed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    before_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    after_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class PageVisit(Base):
    """Normalized page visit / navigation record.

    Attributes:
        id: Visit primary key.
        run_id: Owning run.
        event_id: Source audit event.
        url: Visited URL.
        title: Document title when known.
        tab_id: Browser tab identifier when relevant.
        visited_at: When the visit was observed.
        metadata_: Extra searchable fields.
    """

    __tablename__ = "page_visits"
    __table_args__ = (
        Index("ix_page_visits_run_id", "run_id"),
        Index("ix_page_visits_visited_at", "visited_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    tab_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    visited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )


class HumanApproval(Base):
    """Normalized human approval gate record.

    Attributes:
        id: Approval primary key.
        run_id: Owning run.
        event_id: Source audit event.
        status: ``requested``, ``granted``, or ``denied``.
        reason: Operator-visible reason or policy note.
        actor: Human identity when known.
        decided_at: When a decision was recorded (null while pending).
        metadata_: Extra searchable fields.
        created_at: Insert timestamp.
    """

    __tablename__ = "human_approvals"
    __table_args__ = (Index("ix_human_approvals_run_id", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CostEntry(Base):
    """Normalized cost accounting row for a run.

    Attributes:
        id: Cost entry primary key.
        run_id: Owning run.
        event_id: Optional source audit event.
        kind: Cost category (e.g. ``model``, ``browser``).
        amount: Monetary or unit amount.
        currency: ISO currency or unit label.
        units: Optional quantity of billable units.
        metadata_: Extra searchable fields.
        created_at: Insert timestamp.
    """

    __tablename__ = "cost_entries"
    __table_args__ = (
        Index("ix_cost_entries_run_id", "run_id"),
        Index("ix_cost_entries_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'USD'"))
    units: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ErrorRecord(Base):
    """Normalized error record linked to a run (and optional event).

    Attributes:
        id: Error primary key.
        run_id: Owning run.
        event_id: Optional source audit event.
        error_type: Exception or error class name.
        message: Short error message (redacted).
        stack: Optional stack trace text (redacted).
        metadata_: Extra searchable fields.
        created_at: Insert timestamp.
    """

    __tablename__ = "errors"
    __table_args__ = (
        Index("ix_errors_run_id", "run_id"),
        Index("ix_errors_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    error_type: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Artifact(Base):
    """Metadata for a content-addressed artifact blob (payloads in T007).

    Attributes:
        id: Artifact metadata primary key.
        run_id: Optional owning run (shared blobs may omit).
        event_id: Optional source audit event.
        sha256: Content hash used for deduplication.
        kind: Artifact kind (screenshot, state, download, …).
        media_type: MIME type.
        size_bytes: Uncompressed or stored size in bytes.
        storage_key: Filesystem/object key under the artifact store.
        metadata_: Extra searchable fields.
        created_at: Insert timestamp.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_run_id", "run_id"),
        Index("ix_artifacts_sha256", "sha256"),
        Index("ix_artifacts_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
