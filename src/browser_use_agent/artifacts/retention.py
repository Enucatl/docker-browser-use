"""Safe retention planning and application for large audit artifacts."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from browser_use_agent.artifacts.settings import load_artifact_store_settings
from browser_use_agent.artifacts.state_diff import StateDiffSettings
from browser_use_agent.artifacts.store import ArtifactStore, create_artifact_store
from browser_use_agent.db.engine import create_engine_from_settings
from browser_use_agent.db.models import Artifact

SCREENSHOT_KIND = "screenshot"
CHECKPOINT_KIND = "browser_state"
SOFT_DELETE_KEY = "retention_deleted_at"
IMPORTANT_BOUNDARY_REASONS = frozenset(
    {"first", "approval", "error", "browser_error", "destructive"}
)


def _optional_days(name: str) -> int | None:
    """Read a non-negative optional retention period."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """Retention policy; unset periods retain artifacts indefinitely.

    Attributes:
        screenshot_after_days: Age after which screenshots are downsampled.
        checkpoint_after_days: Age after which checkpoints are downsampled.
        screenshot_keep_every: Keep each Nth old screenshot per run.
        checkpoint_keep_every: Keep each Nth old checkpoint per run.
        state_diffs: Optional state-diff settings.
    """

    screenshot_after_days: int | None = None
    checkpoint_after_days: int | None = None
    screenshot_keep_every: int = 10
    checkpoint_keep_every: int = 10
    state_diffs: StateDiffSettings = field(default_factory=StateDiffSettings)

    def __post_init__(self) -> None:
        """Reject invalid downsampling intervals."""
        if self.screenshot_keep_every < 1 or self.checkpoint_keep_every < 1:
            raise ValueError("retention keep_every values must be at least 1")

    @classmethod
    def from_env(cls) -> RetentionSettings:
        """Load retention settings from environment variables."""
        return cls(
            screenshot_after_days=_optional_days("SCREENSHOT_RETENTION_DAYS"),
            checkpoint_after_days=_optional_days("CHECKPOINT_RETENTION_DAYS"),
            screenshot_keep_every=int(os.environ.get("SCREENSHOT_RETENTION_KEEP_EVERY", "10")),
            checkpoint_keep_every=int(os.environ.get("CHECKPOINT_RETENTION_KEEP_EVERY", "10")),
            state_diffs=StateDiffSettings.from_env(),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Small artifact inventory record used by the policy planner."""

    id: uuid.UUID | str
    kind: str
    storage_key: str
    created_at: datetime
    run_id: uuid.UUID | str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """Keep/delete decision for one artifact metadata row."""

    artifact: ArtifactRecord
    action: Literal["keep", "soft_delete"]
    reason: str


def _utc(value: datetime) -> datetime:
    """Normalize a database timestamp for age comparisons."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _reason(record: ArtifactRecord) -> str | None:
    """Read the writer-supplied policy reason from artifact metadata."""
    value = record.metadata.get("reason")
    return value if isinstance(value, str) else None


def is_soft_deleted(record: ArtifactRecord) -> bool:
    """Return whether retention already marked a metadata row deleted."""
    return bool(record.metadata.get(SOFT_DELETE_KEY))


def plan_retention(
    inventory: Iterable[ArtifactRecord],
    *,
    settings: RetentionSettings,
    now: datetime | None = None,
) -> list[RetentionDecision]:
    """Plan safe artifact compaction without querying or changing events.

    Recent artifacts and unsupported kinds are retained. Old checkpoint and
    screenshot boundary reasons are retained; remaining old rows are sampled
    per run at the configured interval.
    """
    current = _utc(now or datetime.now(UTC))
    records = sorted(inventory, key=lambda item: (_utc(item.created_at), str(item.id)))
    old_ordinals: defaultdict[tuple[str, str], int] = defaultdict(int)
    decisions: list[RetentionDecision] = []
    for record in records:
        if is_soft_deleted(record):
            decisions.append(RetentionDecision(record, "keep", "already_soft_deleted"))
            continue
        days = {
            SCREENSHOT_KIND: settings.screenshot_after_days,
            CHECKPOINT_KIND: settings.checkpoint_after_days,
        }.get(record.kind)
        if days is None:
            decisions.append(RetentionDecision(record, "keep", "retention_disabled"))
            continue
        if _utc(record.created_at) >= current - timedelta(days=days):
            decisions.append(RetentionDecision(record, "keep", "within_retention"))
            continue

        key = (record.kind, str(record.run_id))
        ordinal = old_ordinals[key]
        old_ordinals[key] += 1
        keep_every = (
            settings.screenshot_keep_every
            if record.kind == SCREENSHOT_KIND
            else settings.checkpoint_keep_every
        )
        if _reason(record) in IMPORTANT_BOUNDARY_REASONS:
            decisions.append(RetentionDecision(record, "keep", "boundary"))
        elif ordinal % keep_every == 0:
            decisions.append(RetentionDecision(record, "keep", "downsample_boundary"))
        else:
            decisions.append(RetentionDecision(record, "soft_delete", "downsampled"))
    return decisions


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Outcome of applying a retention plan."""

    soft_deleted: int
    blobs_removed: int
    removed_storage_keys: tuple[str, ...]


def apply_retention(
    plan: Iterable[RetentionDecision],
    *,
    session: Session,
    store: ArtifactStore,
    now: datetime | None = None,
) -> RetentionResult:
    """Soft-delete metadata and remove only unreferenced content blobs.

    This function only selects and updates ``artifacts`` rows. It never issues
    SQL against ``agent_events``; immutable event history remains untouched.
    Metadata rows are retained because other audit tables may FK-reference
    them. Their event references become intentionally unavailable when the
    final content blob is compacted; the marker preserves that fact.
    """
    decisions = list(plan)
    rows = {str(row.id): row for row in session.scalars(select(Artifact)).all()}
    timestamp = _utc(now or datetime.now(UTC)).isoformat()
    deleted_keys: set[str] = set()
    soft_deleted = 0
    for decision in decisions:
        if decision.action != "soft_delete":
            continue
        row = rows.get(str(decision.artifact.id))
        if row is None or row.metadata_.get(SOFT_DELETE_KEY):
            continue
        row.metadata_ = {
            **row.metadata_,
            SOFT_DELETE_KEY: timestamp,
            "retention_reason": decision.reason,
        }
        deleted_keys.add(row.storage_key)
        soft_deleted += 1
    session.flush()

    live_keys = {row.storage_key for row in rows.values() if not row.metadata_.get(SOFT_DELETE_KEY)}
    removable = sorted(deleted_keys - live_keys)
    session.commit()
    removed: list[str] = []
    for storage_key in removable:
        if store.delete(storage_key):
            removed.append(storage_key)
    return RetentionResult(soft_deleted, len(removed), tuple(removed))


def _cli(argv: list[str] | None = None) -> int:
    """Run the retention planner; mutation requires ``--apply``."""
    parser = argparse.ArgumentParser(description="Plan or apply artifact retention.")
    parser.add_argument("--apply", action="store_true", help="soft-delete and compact artifacts")
    parser.add_argument("--screenshot-days", type=int)
    parser.add_argument("--checkpoint-days", type=int)
    args = parser.parse_args(argv)
    settings = RetentionSettings.from_env()
    if args.screenshot_days is not None:
        settings = RetentionSettings(
            screenshot_after_days=args.screenshot_days,
            checkpoint_after_days=settings.checkpoint_after_days,
            screenshot_keep_every=settings.screenshot_keep_every,
            checkpoint_keep_every=settings.checkpoint_keep_every,
            state_diffs=settings.state_diffs,
        )
    if args.checkpoint_days is not None:
        settings = RetentionSettings(
            screenshot_after_days=settings.screenshot_after_days,
            checkpoint_after_days=args.checkpoint_days,
            screenshot_keep_every=settings.screenshot_keep_every,
            checkpoint_keep_every=settings.checkpoint_keep_every,
            state_diffs=settings.state_diffs,
        )
    engine = create_engine_from_settings()
    if engine is None:
        print("database settings incomplete", file=sys.stderr)
        return 1
    with Session(engine) as session:
        rows = session.scalars(select(Artifact)).all()
        inventory = [
            ArtifactRecord(
                id=row.id,
                kind=row.kind,
                storage_key=row.storage_key,
                created_at=row.created_at,
                run_id=row.run_id,
                metadata=row.metadata_,
            )
            for row in rows
        ]
        plan = plan_retention(inventory, settings=settings)
        deletions = [item for item in plan if item.action == "soft_delete"]
        print(f"planned soft deletes: {len(deletions)}")
        if args.apply:
            result = apply_retention(
                plan,
                session=session,
                store=create_artifact_store(load_artifact_store_settings()),
            )
            print(f"soft deleted: {result.soft_deleted}; blobs removed: {result.blobs_removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
