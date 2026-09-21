"""Read-only cost dashboard API routes (T029)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from browser_use_agent.api.deps import get_session
from browser_use_agent.audit.costs import recent_cost_summary, run_cost
from browser_use_agent.db.models import Run

router = APIRouter(prefix="/api", tags=["costs"])


class CostTotal(BaseModel):
    """One aggregate cost value."""

    amount: Decimal
    currency: str = "USD"


class CostSummary(BaseModel):
    """Recent cost totals grouped by day and model."""

    since: datetime
    total: Decimal
    currency: str = "USD"
    by_run: dict[str, Decimal]
    by_day: dict[str, Decimal]
    by_model: dict[str, Decimal]


@router.get("/runs/{run_id}/cost", response_model=CostTotal)
def get_run_cost(
    run_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> CostTotal:
    """Return the USD cost accumulated by one run."""
    if session.get(Run, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return CostTotal(amount=run_cost(session, run_id) or Decimal("0"))


@router.get("/costs/summary", response_model=CostSummary)
def get_cost_summary(
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> CostSummary:
    """Return recent USD cost totals grouped by UTC day and model."""
    summary = recent_cost_summary(session, days=days)
    return CostSummary(
        since=summary["since"],
        total=summary["total"],
        by_run=summary["by_run"],
        by_day=summary["by_day"],
        by_model=summary["by_model"],
    )
