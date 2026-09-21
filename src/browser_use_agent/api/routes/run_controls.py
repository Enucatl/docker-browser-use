"""REST routes for pause, resume, cancel, and retry controls (T020)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from browser_use_agent.api.deps import get_session
from browser_use_agent.api.routes.runs import RunResponse, _to_response
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.services import runs as run_service

router = APIRouter(prefix="/api/runs", tags=["run-controls"])


@router.post("/{run_id}/pause", response_model=RunResponse)
def pause_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> RunResponse:
    """Pause a running run; the worker parks between steps until resume."""
    try:
        run = run_service.pause_run(session, run_id)
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except run_service.RunControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(run, session)


@router.post("/{run_id}/resume", response_model=RunResponse)
def resume_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> RunResponse:
    """Resume a paused run and wake the parked worker."""
    try:
        run = run_service.resume_run(session, run_id)
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except run_service.RunControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(run, session)


@router.post("/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> RunResponse:
    """Cancel a non-terminal run; the worker exits cooperatively."""
    try:
        run = run_service.cancel_run(session, run_id)
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _to_response(run, session)


@router.post("/{run_id}/retry", response_model=RunResponse)
async def retry_run(
    run_id: uuid.UUID,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> RunResponse:
    """Retry a failed run (re-queue + start worker) or arm an in-flight step retry."""
    try:
        run = run_service.retry_run(session, run_id)
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except run_service.RunControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Commit before starting the worker so it sees queued status.
    session.commit()

    if run.status == RunStatus.QUEUED.value:
        worker = getattr(request.app.state, "run_worker", None)
        if worker is not None:
            await worker.start_run(run_id)
            session.expire_all()
            run = run_service.get_run(session, run_id)

    return _to_response(run, session)
