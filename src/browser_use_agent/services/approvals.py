"""Domain logic for human approval gates (T021)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from browser_use_agent.agent.controls import get_control_hub
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.db.models import HumanApproval, Run
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.services.runs import RunControlError, get_run

APPROVAL_STATUS_REQUESTED = "requested"
APPROVAL_STATUS_GRANTED = "granted"
APPROVAL_STATUS_DENIED = "denied"


def create_pending_approval(
    session: Session,
    run_id: uuid.UUID,
    *,
    event_id: uuid.UUID,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> HumanApproval:
    """Insert a ``requested`` human_approvals row for an open gate.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Owning run.
        event_id: ``approval_requested`` audit event id.
        reason: Operator-visible policy reason.
        metadata: Extra searchable fields (action kind, URL, …).

    Returns:
        The persisted :class:`~browser_use_agent.db.models.HumanApproval` row.
    """
    row = HumanApproval(
        id=uuid.uuid4(),
        run_id=run_id,
        event_id=event_id,
        status=APPROVAL_STATUS_REQUESTED,
        reason=reason,
        actor=None,
        decided_at=None,
        metadata_=dict(metadata or {}),
    )
    session.add(row)
    session.flush()

    signals = get_control_hub().signals_for(run_id)
    signals.pending_approval_id = row.id
    return row


def get_pending_approval(session: Session, run_id: uuid.UUID) -> HumanApproval | None:
    """Return the newest open (``requested``) approval for a run, if any.

    Args:
        session: Active SQLAlchemy session.
        run_id: Run primary key.

    Returns:
        Pending approval row, or ``None``.
    """
    stmt = (
        select(HumanApproval)
        .where(
            HumanApproval.run_id == run_id,
            HumanApproval.status == APPROVAL_STATUS_REQUESTED,
        )
        .order_by(HumanApproval.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _decide(
    session: Session,
    run_id: uuid.UUID,
    *,
    granted: bool,
    actor: str,
    reason: str | None,
) -> tuple[Run, HumanApproval]:
    """Shared approve/reject transition.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run awaiting approval.
        granted: ``True`` to approve, ``False`` to reject.
        actor: Authelia username (or local stub).
        reason: Optional operator note.

    Returns:
        Updated run and approval rows.

    Raises:
        RunNotFoundError: When the run does not exist.
        RunControlError: When the run is not awaiting approval / no pending row.
    """
    run = get_run(session, run_id)
    if run.status != RunStatus.AWAITING_APPROVAL.value:
        raise RunControlError(
            f"cannot {'approve' if granted else 'reject'} run in status {run.status!r}"
        )

    pending = get_pending_approval(session, run_id)
    if pending is None:
        raise RunControlError(f"run {run_id} has no pending approval")

    now = datetime.now(UTC)
    previous = run.status
    decision_status = APPROVAL_STATUS_GRANTED if granted else APPROVAL_STATUS_DENIED
    pending.status = decision_status
    pending.actor = actor
    pending.decided_at = now
    if reason is not None:
        pending.reason = reason if pending.reason is None else f"{pending.reason} | {reason}"

    if granted:
        run.status = RunStatus.RUNNING.value
        run.updated_at = now
        event_type = "approval_granted"
        hub_decision = "granted"
    else:
        run.status = RunStatus.FAILED.value
        run.finished_at = now
        run.updated_at = now
        event_type = "approval_denied"
        hub_decision = "denied"

    session.flush()

    payload: dict[str, Any] = {
        "status": run.status,
        "previous_status": previous,
        "approval_id": str(pending.id),
        "decision": decision_status,
        "actor": actor,
    }
    if reason:
        payload["operator_reason"] = reason

    AuditWriter(session).append(
        run.id,
        event_type,
        payload,
        actor="human",
    )
    session.flush()

    # Commit before waking the worker. Otherwise it can start its next DB
    # write while this transaction still holds the run/approval row locks.
    session.commit()
    get_control_hub().set_approval_decision(run.id, hub_decision)
    return run, pending


def approve_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    actor: str,
    reason: str | None = None,
) -> Run:
    """Grant a pending approval and resume the parked worker.

    On grant the run returns to ``running`` and the in-flight loop continues
    executing the blocked action. Approver identity is stored on
    ``human_approvals.actor``.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run awaiting approval.
        actor: Authenticated username from Authelia ``Remote-User``.
        reason: Optional operator note.

    Returns:
        The updated run.

    Raises:
        RunNotFoundError: When the run does not exist.
        RunControlError: When approve is not allowed.
    """
    run, _pending = _decide(session, run_id, granted=True, actor=actor, reason=reason)
    return run


def reject_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    actor: str,
    reason: str | None = None,
) -> Run:
    """Deny a pending approval and fail the run (Phase-1 reject behavior).

    Reject does **not** ask Jev to replan; the run becomes ``failed``. Richer
    replan policies belong in T028.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Run awaiting approval.
        actor: Authenticated username from Authelia ``Remote-User``.
        reason: Optional operator note.

    Returns:
        The updated run.

    Raises:
        RunNotFoundError: When the run does not exist.
        RunControlError: When reject is not allowed.
    """
    run, _pending = _decide(session, run_id, granted=False, actor=actor, reason=reason)
    return run


def mark_approval_timed_out(
    session: Session,
    run_id: uuid.UUID,
    *,
    approval_id: uuid.UUID | None = None,
) -> HumanApproval | None:
    """Mark a pending approval denied due to fail-closed timeout.

    Args:
        session: Active SQLAlchemy session (caller commits).
        run_id: Owning run.
        approval_id: Specific row when known; otherwise the newest pending.

    Returns:
        Updated approval row, or ``None`` when nothing was pending.
    """
    pending: HumanApproval | None
    if approval_id is not None:
        pending = session.get(HumanApproval, approval_id)
        if pending is None or pending.run_id != run_id:
            return None
        if pending.status != APPROVAL_STATUS_REQUESTED:
            return pending
    else:
        pending = get_pending_approval(session, run_id)
        if pending is None:
            return None

    now = datetime.now(UTC)
    pending.status = APPROVAL_STATUS_DENIED
    pending.actor = "system"
    pending.decided_at = now
    note = "timeout (fail closed)"
    pending.reason = note if pending.reason is None else f"{pending.reason} | {note}"
    session.flush()
    return pending
