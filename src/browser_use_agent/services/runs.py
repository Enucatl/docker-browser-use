"""Domain logic for agent run CRUD and lifecycle controls (T010 / T020)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from browser_use_agent.agent.controls import (
    get_control_hub,
    is_pausable,
    is_resumable,
    is_retryable_failed,
)
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.db.models import Run
from browser_use_agent.runs.status import TERMINAL_STATUSES, RunStatus


class RunNotFoundError(LookupError):
    """Raised when a run id does not exist."""


class RunControlError(ValueError):
    """Raised when a lifecycle transition is not allowed for the current status."""


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


def _parse_status(run: Run) -> RunStatus | None:
    """Parse the run's status string into a :class:`RunStatus`.

    Args:
        run: Persisted run row.

    Returns:
        Enum value, or ``None`` when the stored string is unknown.
    """
    try:
        return RunStatus(run.status)
    except ValueError:
        return None


def pause_run(session: Session, run_id: uuid.UUID) -> Run:
    """Transition a running run to ``paused`` and audit the change.

    The worker parks between steps until :func:`resume_run` or cancel.
    Idempotent when already paused.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to pause.

    Returns:
        The updated run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
        RunControlError: When the run is not running or paused.
    """
    run = get_run(session, run_id)
    current = _parse_status(run)
    if current == RunStatus.PAUSED:
        return run
    if current is None or not is_pausable(current):
        raise RunControlError(f"cannot pause run in status {run.status!r}")

    now = datetime.now(UTC)
    previous = run.status
    run.status = RunStatus.PAUSED.value
    run.updated_at = now
    session.flush()

    AuditWriter(session).append(
        run.id,
        "run_paused",
        {"status": run.status, "previous_status": previous},
        actor="human",
    )
    session.flush()
    get_control_hub().notify(run.id)
    return run


def resume_run(session: Session, run_id: uuid.UUID) -> Run:
    """Transition a paused run back to ``running`` and wake the worker.

    Idempotent when already running.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to resume.

    Returns:
        The updated run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
        RunControlError: When the run is not paused or running.
    """
    run = get_run(session, run_id)
    current = _parse_status(run)
    if current == RunStatus.RUNNING:
        return run
    if current is None or not is_resumable(current):
        raise RunControlError(f"cannot resume run in status {run.status!r}")

    now = datetime.now(UTC)
    previous = run.status
    run.status = RunStatus.RUNNING.value
    run.updated_at = now
    session.flush()

    AuditWriter(session).append(
        run.id,
        "run_resumed",
        {"status": run.status, "previous_status": previous},
        actor="human",
    )
    session.flush()
    get_control_hub().notify(run.id)
    return run


def cancel_run(session: Session, run_id: uuid.UUID) -> Run:
    """Mark a non-terminal run as ``cancelled`` and wake any parked worker.

    Already terminal runs are returned unchanged. The in-flight loop exits
    cooperatively between phases after reading this status.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to cancel.

    Returns:
        The updated (or unchanged terminal) run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
    """
    run = get_run(session, run_id)
    current = _parse_status(run)

    if current in TERMINAL_STATUSES:
        return run

    now = datetime.now(UTC)
    previous = run.status
    run.status = RunStatus.CANCELLED.value
    run.finished_at = now
    run.updated_at = now
    session.flush()

    AuditWriter(session).append(
        run.id,
        "run_cancelled",
        {"status": run.status, "previous_status": previous},
        actor="human",
    )
    session.flush()
    get_control_hub().notify(run.id)
    return run


def stop_run(session: Session, run_id: uuid.UUID) -> Run:
    """Alias for :func:`cancel_run` (T010 ``/stop`` path).

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to stop.

    Returns:
        The updated (or unchanged terminal) run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
    """
    return cancel_run(session, run_id)


def retry_run(session: Session, run_id: uuid.UUID) -> Run:
    """Retry a failed run or arm an in-flight step retry.

    * **failed** → re-queue (``queued``, clear ``finished_at``) so the API can
      start the worker again; audits ``run_retry_requested``.
    * **running** / **paused** → arm one-shot step retry (re-observe + decide
      after the next failed execute).
    * Other statuses raise :class:`RunControlError`.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to retry.

    Returns:
        The updated run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
        RunControlError: When retry is not allowed for the current status.
    """
    run = get_run(session, run_id)
    current = _parse_status(run)
    if current is None:
        raise RunControlError(f"cannot retry run in status {run.status!r}")

    now = datetime.now(UTC)
    previous = run.status

    if is_retryable_failed(current):
        run.status = RunStatus.QUEUED.value
        run.finished_at = None
        run.updated_at = now
        session.flush()
        AuditWriter(session).append(
            run.id,
            "run_retry_requested",
            {
                "status": run.status,
                "previous_status": previous,
                "mode": "requeue",
            },
            actor="human",
        )
        session.flush()
        get_control_hub().discard(run.id)
        return run

    if current in {RunStatus.RUNNING, RunStatus.PAUSED}:
        AuditWriter(session).append(
            run.id,
            "run_retry_requested",
            {
                "status": run.status,
                "previous_status": previous,
                "mode": "step",
            },
            actor="human",
        )
        session.flush()
        get_control_hub().arm_step_retry(run.id)
        return run

    raise RunControlError(f"cannot retry run in status {run.status!r}")
