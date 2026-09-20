"""Domain logic for take-control / release-control (T023)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from browser_use_agent.agent.controls import get_control_hub
from browser_use_agent.agent.takeover import is_releasable, is_takeoverable
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.db.models import Run
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.services.runs import RunControlError, get_run


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


def take_control(session: Session, run_id: uuid.UUID, *, actor: str) -> Run:
    """Hand the browser to a human and park the agent (``awaiting_human``).

    Stronger than pause: accepted from ``running`` or ``paused``. Idempotent
    when already ``awaiting_human``. Emits ``takeover_started`` with the
    Authelia operator identity.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to take over.
        actor: Authenticated username from Authelia ``Remote-User``.

    Returns:
        The updated run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
        RunControlError: When take-control is not allowed for the status.
    """
    run = get_run(session, run_id)
    current = _parse_status(run)
    if current == RunStatus.AWAITING_HUMAN:
        return run
    if current is None or not is_takeoverable(current):
        raise RunControlError(f"cannot take control of run in status {run.status!r}")

    now = datetime.now(UTC)
    previous = run.status
    run.status = RunStatus.AWAITING_HUMAN.value
    run.updated_at = now
    session.flush()

    AuditWriter(session).append(
        run.id,
        "takeover_started",
        {
            "status": run.status,
            "previous_status": previous,
            "actor": actor,
        },
        actor="human",
    )
    session.flush()
    get_control_hub().notify(run.id)
    return run


def release_control(session: Session, run_id: uuid.UUID, *, actor: str) -> Run:
    """Return control to the agent (``running``) and wake the parked worker.

    Idempotent when already ``running``. Emits ``takeover_ended`` with the
    Authelia operator identity. The worker resumes with a fresh observe.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run to release.
        actor: Authenticated username from Authelia ``Remote-User``.

    Returns:
        The updated run.

    Raises:
        RunNotFoundError: When no row exists for ``run_id``.
        RunControlError: When release is not allowed for the status.
    """
    run = get_run(session, run_id)
    current = _parse_status(run)
    if current == RunStatus.RUNNING:
        return run
    if current is None or not is_releasable(current):
        raise RunControlError(f"cannot release control of run in status {run.status!r}")

    now = datetime.now(UTC)
    previous = run.status
    run.status = RunStatus.RUNNING.value
    run.updated_at = now
    session.flush()

    AuditWriter(session).append(
        run.id,
        "takeover_ended",
        {
            "status": run.status,
            "previous_status": previous,
            "actor": actor,
        },
        actor="human",
    )
    session.flush()
    get_control_hub().notify(run.id)
    return run
