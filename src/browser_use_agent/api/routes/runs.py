"""Pydantic schemas and REST routes for agent runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from browser_use_agent.api.deps import get_session
from browser_use_agent.audit.costs import run_cost
from browser_use_agent.browser.profiles import get_profile
from browser_use_agent.db.models import Run
from browser_use_agent.services import runs as run_service

router = APIRouter(prefix="/api/runs", tags=["runs"])


class CreateRunRequest(BaseModel):
    """Body for ``POST /api/runs``."""

    goal: str = Field(min_length=1, description="Natural-language operator goal.")
    profile_id: str | None = Field(
        default=None,
        description="Browser profile key; defaults to the Testing profile.",
    )


class RunResponse(BaseModel):
    """Public run representation returned by the lifecycle API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal: str
    status: str
    profile_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cost: Decimal | None = Field(
        default=None,
        description="Accumulated USD cost from model calls, when priced.",
    )


async def _to_response(run: Run, session: AsyncSession | None = None) -> RunResponse:
    """Map an ORM run to the API response, including accumulated cost.

    Args:
        run: Persisted run row.
        session: Optional session used to sum cost entries.

    Returns:
        JSON-serializable run payload.
    """
    return RunResponse(
        id=run.id,
        goal=run.goal,
        status=run.status,
        profile_id=run.profile_id,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        cost=(await session.run_sync(lambda sync: run_cost(sync, run.id)))
        if session is not None
        else None,
    )


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRunRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunResponse:
    """Create a run from a natural-language goal and start the worker."""
    try:
        profile = get_profile(body.profile_id, settings=request.app.state.settings.browser)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    run = await session.run_sync(
        lambda sync: run_service.create_run(sync, body.goal.strip(), profile_id=profile.id)
    )
    # Commit before the worker so it sees the queued row.
    await session.commit()

    worker = getattr(request.app.state, "run_worker", None)
    if worker is not None:
        run_id = run.id
        await worker.start_run(run_id)
        session.expire_all()
        run = await session.get(Run, run_id)
        assert run is not None

    return await _to_response(run, session)


@router.get("", response_model=list[RunResponse])
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunResponse]:
    """List recent runs newest-first."""
    runs = await session.run_sync(lambda sync: run_service.list_runs(sync, limit=limit))
    return [await _to_response(run, session) for run in runs]


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunResponse:
    """Return one run by id."""
    try:
        run = await session.get(Run, run_id)
        if run is None:
            raise run_service.RunNotFoundError(f"run {run_id} does not exist")
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _to_response(run, session)


@router.post("/{run_id}/stop", response_model=RunResponse)
async def stop_run(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunResponse:
    """Cancel the run (alias of ``POST .../cancel``); worker exits cooperatively."""
    try:
        run = await session.run_sync(lambda sync: run_service.stop_run(sync, run_id))
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _to_response(run, session)
