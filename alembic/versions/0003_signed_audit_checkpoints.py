"""Add signed audit-chain checkpoint records (T035)."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_signed_audit_checkpoints"
down_revision: str | None = "0002_tracing_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only signed checkpoint table."""
    op.create_table(
        "audit_checkpoints",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("chain_hash", sa.Text(), nullable=False),
        sa.Column("key_id", sa.Text(), server_default=sa.text("'default'"), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("run_id", "seq"),
    )
    op.create_index("ix_audit_checkpoints_run_id", "audit_checkpoints", ["run_id"])


def downgrade() -> None:
    """Remove signed checkpoint records."""
    op.drop_index("ix_audit_checkpoints_run_id", table_name="audit_checkpoints")
    op.drop_table("audit_checkpoints")
