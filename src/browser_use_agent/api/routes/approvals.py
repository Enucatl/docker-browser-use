"""REST routes for human approval gates (T021)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from browser_use_agent.api.deps import CurrentUser, get_session
from browser_use_agent.api.routes.runs import RunResponse, _to_response
from browser_use_agent.services import approvals as approval_service
from browser_use_agent.services import runs as run_service

router = APIRouter(prefix="/api/runs", tags=["approvals"])


class ApprovalDecisionRequest(BaseModel):
    """Optional body for approve / reject."""

    reason: str | None = Field(
        default=None,
        description="Optional operator note recorded on the approval row.",
    )


@router.post("/{run_id}/approve", response_model=RunResponse)
def approve_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: CurrentUser,
    body: ApprovalDecisionRequest | None = None,
) -> RunResponse:
    """Approve a high-impact action; identity comes from Authelia Remote-User."""
    reason = body.reason if body is not None else None
    try:
        run = approval_service.approve_run(
            session,
            run_id,
            actor=user.username,
            reason=reason,
        )
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except run_service.RunControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(run, session)


@router.post("/{run_id}/reject", response_model=RunResponse)
def reject_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
    user: CurrentUser,
    body: ApprovalDecisionRequest | None = None,
) -> RunResponse:
    """Reject a high-impact action and fail the run (no replan in Phase 1)."""
    reason = body.reason if body is not None else None
    try:
        run = approval_service.reject_run(
            session,
            run_id,
            actor=user.username,
            reason=reason,
        )
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except run_service.RunControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(run, session)
