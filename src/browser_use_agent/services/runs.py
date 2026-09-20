"""Domain logic for agent run CRUD and basic lifecycle stubs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.db.models import Run
from browser_use_agent.runs.status import TERMINAL_STATUSES, RunStatus


class RunNotFoundError(LookupError):
    """Raised when a run id does not exist."""


def create_run(
    session: Session,
    goal: str,
    *,
    profile_id: str | None = None,
) -> Run:
    """Insert a queued run and emit ``task_received`` / ``run_created`` audit events.

    Args:
        session: Active SQLAlchemy session (caller commits).
        goal: Operator natural-language goal.
        profile_id: Optional browser profile key (stub until multi-profile).

    Returns:
        The persisted :class:`~browser_use_agent.db.models.Run` row.
    """
    run = Run(
        id=uuid.uuid4(),
        goal=goal,
        status=RunStatus.QUEUED.value,
        profile_id=profile_id,
    )
    session.add(run)
    session.flush()

    writer = AuditWriter(session)
    writer.append(
        run.id,
        "task_received",
        {"goal": goal, "profile_id": profile_id},
        actor="human",
    )
    writer.append(
        run.id,
        "run_created",
        {"status": run.status, "profile_id": profile_id},
        actor="system",
    )
    session.flush()
    return run


def get_run(session: Session, run_id: uuid.UUID) -> Run:
    """Load a run by id.

    Args:
        session: Active SQLAlchemy session.
        run_id: Run primary key.

    Returns:
        The matching run row.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise RunNotFoundError(f"run {run_id} does not exist")
    return run


def list_runs(session: Session, *, limit: int = 50) -> list[Run]:
    """Return recent runs ordered by creation time descending.

    Args:
        session: Active SQLAlchemy session.
        limit: Maximum number of rows to return (clamped to 1..200).

    Returns:
        Runs newest-first.
    """
    capped = max(1, min(limit, 200))
    return list(session.scalars(select(Run).order_by(Run.created_at.desc()).limit(capped)).all())


def stop_run(session: Session, run_id: uuid.UUID) -> Run:
    """Stub cancel: mark a non-terminal run as ``cancelled``.

    Real cancellation of an in-flight browser loop lands in T020. Already
    terminal runs are returned unchanged.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to stop.

    Returns:
        The updated (or unchanged terminal) run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
    """
    run = get_run(session, run_id)
    try:
        current = RunStatus(run.status)
    except ValueError:
        current = None

    if current in TERMINAL_STATUSES:
        return run

    now = datetime.now(UTC)
    run.status = RunStatus.CANCELLED.value
    run.finished_at = now
    run.updated_at = now
    session.flush()

    AuditWriter(session).append(
        run.id,
        "run_cancelled",
        {"status": run.status},
        actor="human",
    )
    session.flush()
    return run
