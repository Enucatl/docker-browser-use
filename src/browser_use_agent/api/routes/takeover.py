"""REST routes for take-control / release-control (T023)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from browser_use_agent.api.deps import CurrentUser, get_session
from browser_use_agent.api.routes.runs import RunResponse, _to_response
from browser_use_agent.services import runs as run_service
from browser_use_agent.services import takeover as takeover_service

router = APIRouter(prefix="/api/runs", tags=["takeover"])


@router.post("/{run_id}/take-control", response_model=RunResponse)
async def take_control(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RunResponse:
    """Hand the browser to the operator; identity from Authelia Remote-User."""
    try:
        run = await session.run_sync(
            lambda sync: takeover_service.take_control(sync, run_id, actor=user.username)
        )
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except run_service.RunControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _to_response(run, session)


@router.post("/{run_id}/release-control", response_model=RunResponse)
async def release_control(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: CurrentUser,
) -> RunResponse:
    """Return control to the agent; identity from Authelia Remote-User."""
    try:
        run = await session.run_sync(
            lambda sync: takeover_service.release_control(sync, run_id, actor=user.username)
        )
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except run_service.RunControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _to_response(run, session)
