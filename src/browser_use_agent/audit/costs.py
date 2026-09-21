"""Cost estimation and aggregation for model-call accounting."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from browser_use_agent.db.models import CostEntry

DEFAULT_PRICE_FILE = Path("config/model_prices.yaml")
_THOUSAND = Decimal("1000")


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Price per 1,000 tokens plus an optional per-request fee."""

    input_per_1k: Decimal = Decimal("0")
    output_per_1k: Decimal = Decimal("0")
    request_fee: Decimal = Decimal("0")


def load_model_prices(path: str | Path | None = None) -> dict[str, ModelPrice]:
    """Load model prices from the configured YAML file.

    Args:
        path: Optional price-file override; otherwise ``MODEL_PRICES_FILE`` or
            ``config/model_prices.yaml`` is used.

    Returns:
        Model names mapped to their configured prices. Missing files produce an
        empty mapping so unpriced calls remain traceable without failing runs.
    """
    price_path = Path(path or os.environ.get("MODEL_PRICES_FILE", DEFAULT_PRICE_FILE))
    if not price_path.is_file():
        return {}
    raw = yaml.safe_load(price_path.read_text(encoding="utf-8")) or {}
    models = raw.get("models", raw) if isinstance(raw, Mapping) else {}
    if not isinstance(models, Mapping):
        raise ValueError("model prices must be a mapping")
    return {str(model): _model_price(value) for model, value in models.items()}


def estimate_model_cost(
    model_name: str | None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    *,
    call_kind: str | None = None,
    prices: Mapping[str, ModelPrice] | None = None,
    price_file: str | Path | None = None,
) -> Decimal | None:
    """Estimate one model-call cost from tokens and the price table.

    Args:
        model_name: Provider model identifier.
        prompt_tokens: Number of input tokens, when reported.
        completion_tokens: Number of output tokens, when reported.
        call_kind: Optional fallback key such as ``jev``.
        prices: Optional already-loaded prices.
        price_file: Optional price-file override.

    Returns:
        Decimal USD cost, or ``None`` when no matching price exists.
    """
    table = prices if prices is not None else load_model_prices(price_file)
    price = table.get(model_name or "") or (table.get(call_kind or "") if call_kind else None)
    if price is None:
        return None
    return (
        price.request_fee
        + Decimal(prompt_tokens or 0) * price.input_per_1k / _THOUSAND
        + Decimal(completion_tokens or 0) * price.output_per_1k / _THOUSAND
    ).quantize(Decimal("0.00000001"))


def record_cost_entry(
    session: Session,
    *,
    run_id: uuid.UUID,
    event_id: uuid.UUID | None,
    amount: Decimal,
    kind: str,
    model_name: str | None = None,
    call_kind: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> CostEntry:
    """Insert one redacted cost row for a model call."""
    metadata: dict[str, Any] = {
        key: value
        for key, value in {
            "model": model_name,
            "call_kind": call_kind,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }.items()
        if value is not None
    }
    row = CostEntry(
        id=uuid.uuid4(),
        run_id=run_id,
        event_id=event_id,
        kind=kind,
        amount=amount,
        metadata_=metadata,
    )
    session.add(row)
    session.flush()
    return row


def aggregate_costs(entries: Iterable[CostEntry]) -> dict[str, Any]:
    """Aggregate cost entries by total, UTC day, and model."""
    total = Decimal("0")
    by_run: dict[str, Decimal] = {}
    by_day: dict[str, Decimal] = {}
    by_model: dict[str, Decimal] = {}
    for entry in entries:
        amount = Decimal(entry.amount)
        total += amount
        run = str(entry.run_id)
        created_at = entry.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        day = created_at.astimezone(UTC).date().isoformat()
        model = str((entry.metadata_ or {}).get("model") or "unknown")
        by_run[run] = by_run.get(run, Decimal("0")) + amount
        by_day[day] = by_day.get(day, Decimal("0")) + amount
        by_model[model] = by_model.get(model, Decimal("0")) + amount
    return {"total": total, "by_run": by_run, "by_day": by_day, "by_model": by_model}


def run_cost(session: Session, run_id: uuid.UUID) -> Decimal | None:
    """Return the total USD cost for one run, or ``None`` with no entries."""
    return session.scalar(
        select(func.sum(CostEntry.amount)).where(
            CostEntry.run_id == run_id,
            CostEntry.currency == "USD",
        )
    )


def recent_cost_summary(session: Session, *, days: int = 30) -> dict[str, Any]:
    """Return cost totals for the recent UTC window."""
    since = datetime.now(UTC) - timedelta(days=max(1, days))
    entries = list(
        session.scalars(
            select(CostEntry).where(
                CostEntry.created_at >= since,
                CostEntry.currency == "USD",
            )
        ).all()
    )
    return {"since": since, **aggregate_costs(entries)}


def _model_price(value: Any) -> ModelPrice:
    """Parse one YAML model-price mapping."""
    if not isinstance(value, Mapping):
        raise ValueError("each model price must be a mapping")
    return ModelPrice(
        input_per_1k=_decimal(value, "input_per_1k_tokens", "input_per_1k"),
        output_per_1k=_decimal(value, "output_per_1k_tokens", "output_per_1k"),
        request_fee=_decimal(value, "request_fee_usd", "request_fee", "flat_fee"),
    )


def _decimal(value: Mapping[str, Any], *keys: str) -> Decimal:
    """Read the first present decimal value from a mapping."""
    for key in keys:
        if key in value and value[key] is not None:
            return Decimal(str(value[key]))
    return Decimal("0")
