"""Tests for T029 cost estimation and aggregation."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from browser_use_agent.audit.costs import ModelPrice, aggregate_costs, estimate_model_cost
from browser_use_agent.db.models import CostEntry


def test_estimate_model_cost_uses_token_prices_and_request_fee() -> None:
    """Input/output tokens and a flat fee are summed exactly."""
    cost = estimate_model_cost(
        "jev-test",
        prompt_tokens=1_500,
        completion_tokens=500,
        prices={
            "jev-test": ModelPrice(
                input_per_1k=Decimal("0.002"),
                output_per_1k=Decimal("0.004"),
                request_fee=Decimal("0.0001"),
            )
        },
    )
    assert cost == Decimal("0.00510000")


def test_aggregate_costs_groups_by_utc_day_and_model() -> None:
    """Aggregation sums the total and both dashboard dimensions."""
    entries = [
        CostEntry(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            amount=Decimal("0.10"),
            metadata_={"model": "alpha"},
            created_at=datetime(2026, 9, 20, 23, 30, tzinfo=UTC),
        ),
        CostEntry(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            amount=Decimal("0.25"),
            metadata_={"model": "alpha"},
            created_at=datetime(2026, 9, 21, 1, 0, tzinfo=UTC),
        ),
        CostEntry(
            run_id=UUID("00000000-0000-0000-0000-000000000002"),
            amount=Decimal("0.05"),
            metadata_={"model": "beta"},
            created_at=datetime(2026, 9, 21, 2, 0, tzinfo=UTC),
        ),
    ]

    summary = aggregate_costs(entries)

    assert summary == {
        "total": Decimal("0.40"),
        "by_run": {
            "00000000-0000-0000-0000-000000000001": Decimal("0.35"),
            "00000000-0000-0000-0000-000000000002": Decimal("0.05"),
        },
        "by_day": {"2026-09-20": Decimal("0.10"), "2026-09-21": Decimal("0.30")},
        "by_model": {"alpha": Decimal("0.35"), "beta": Decimal("0.05")},
    }
