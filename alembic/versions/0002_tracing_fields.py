"""Add forensic columns for model_calls and browser_actions tracing (T017).

Revision ID: 0002_tracing_fields
Revises: 0001_event_sourcing
Create Date: 2026-09-20

Links normalized traces to ``agent_events.seq``, supports artifact offload for
large prompts/responses, and adds commonly queried browser-action fields.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_tracing_fields"
down_revision: str | None = "0001_event_sourcing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add tracing columns and indexes on model_calls / browser_actions."""
    op.add_column(
        "model_calls",
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("model_calls", sa.Column("event_seq", sa.BigInteger(), nullable=True))
    op.add_column("model_calls", sa.Column("status", sa.Text(), nullable=True))
    op.add_column("model_calls", sa.Column("retries", sa.BigInteger(), nullable=True))
    op.add_column("model_calls", sa.Column("request_id", sa.Text(), nullable=True))
    op.add_column(
        "model_calls",
        sa.Column("cost_usd", sa.Numeric(20, 8), nullable=True),
    )
    op.add_column(
        "model_calls",
        sa.Column("request_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "model_calls",
        sa.Column("response_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_model_calls_request_artifact_id",
        "model_calls",
        "artifacts",
        ["request_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_model_calls_response_artifact_id",
        "model_calls",
        "artifacts",
        ["response_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_model_calls_event_id", "model_calls", ["event_id"])
    op.create_index("ix_model_calls_event_seq", "model_calls", ["run_id", "event_seq"])
    op.create_index("ix_model_calls_step_id", "model_calls", ["step_id"])

    op.add_column(
        "browser_actions",
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("browser_actions", sa.Column("event_seq", sa.BigInteger(), nullable=True))
    op.add_column("browser_actions", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("browser_actions", sa.Column("element_index", sa.BigInteger(), nullable=True))
    op.add_column("browser_actions", sa.Column("page_changed", sa.Boolean(), nullable=True))
    op.add_column("browser_actions", sa.Column("result", sa.Text(), nullable=True))
    op.add_column(
        "browser_actions",
        sa.Column("before_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "browser_actions",
        sa.Column("after_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_browser_actions_before_artifact_id",
        "browser_actions",
        "artifacts",
        ["before_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_browser_actions_after_artifact_id",
        "browser_actions",
        "artifacts",
        ["after_artifact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_browser_actions_event_id", "browser_actions", ["event_id"])
    op.create_index(
        "ix_browser_actions_event_seq",
        "browser_actions",
        ["run_id", "event_seq"],
    )
    op.create_index("ix_browser_actions_step_id", "browser_actions", ["step_id"])


def downgrade() -> None:
    """Drop T017 tracing columns and indexes."""
    op.drop_index("ix_browser_actions_step_id", table_name="browser_actions")
    op.drop_index("ix_browser_actions_event_seq", table_name="browser_actions")
    op.drop_index("ix_browser_actions_event_id", table_name="browser_actions")
    op.drop_constraint(
        "fk_browser_actions_after_artifact_id",
        "browser_actions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_browser_actions_before_artifact_id",
        "browser_actions",
        type_="foreignkey",
    )
    op.drop_column("browser_actions", "after_artifact_id")
    op.drop_column("browser_actions", "before_artifact_id")
    op.drop_column("browser_actions", "result")
    op.drop_column("browser_actions", "page_changed")
    op.drop_column("browser_actions", "element_index")
    op.drop_column("browser_actions", "title")
    op.drop_column("browser_actions", "event_seq")
    op.drop_column("browser_actions", "step_id")

    op.drop_index("ix_model_calls_step_id", table_name="model_calls")
    op.drop_index("ix_model_calls_event_seq", table_name="model_calls")
    op.drop_index("ix_model_calls_event_id", table_name="model_calls")
    op.drop_constraint(
        "fk_model_calls_response_artifact_id",
        "model_calls",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_model_calls_request_artifact_id",
        "model_calls",
        type_="foreignkey",
    )
    op.drop_column("model_calls", "response_artifact_id")
    op.drop_column("model_calls", "request_artifact_id")
    op.drop_column("model_calls", "cost_usd")
    op.drop_column("model_calls", "request_id")
    op.drop_column("model_calls", "retries")
    op.drop_column("model_calls", "status")
    op.drop_column("model_calls", "event_seq")
    op.drop_column("model_calls", "step_id")
