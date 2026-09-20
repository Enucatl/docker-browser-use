"""Pydantic schemas and REST routes for agent runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from browser_use_agent.api.deps import get_session
from browser_use_agent.db.models import Run
from browser_use_agent.services import runs as run_service

router = APIRouter(prefix="/api/runs", tags=["runs"])


class CreateRunRequest(BaseModel):
    """Body for ``POST /api/runs``."""

    goal: str = Field(min_length=1, description="Natural-language operator goal.")
    profile_id: str | None = Field(
        default=None,
        description="Optional browser profile key (stub until multi-profile).",
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
        description="Placeholder until cost tracking (T029).",
    )


def _to_response(run: Run) -> RunResponse:
    """Map an ORM run to the API response, with a null cost placeholder.

    Args:
        run: Persisted run row.

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
        cost=None,
    )


@router.post("", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
def create_run(
    body: CreateRunRequest,
    session: Annotated[Session, Depends(get_session)],
) -> RunResponse:
    """Create a queued run from a natural-language goal."""
    run = run_service.create_run(session, body.goal.strip(), profile_id=body.profile_id)
    return _to_response(run)


@router.get("", response_model=list[RunResponse])
def list_runs(
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunResponse]:
    """List recent runs newest-first."""
    return [_to_response(run) for run in run_service.list_runs(session, limit=limit)]


@router.get("/{run_id}", response_model=RunResponse)
def get_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> RunResponse:
    """Return one run by id."""
    try:
        run = run_service.get_run(session, run_id)
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _to_response(run)


@router.post("/{run_id}/stop", response_model=RunResponse)
def stop_run(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> RunResponse:
    """Stub stop: mark the run cancelled when not already terminal."""
    try:
        run = run_service.stop_run(session, run_id)
    except run_service.RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _to_response(run)
