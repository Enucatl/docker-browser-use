"""Tests for T033 artifact retention decisions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from browser_use_agent.artifacts.retention import (
    ArtifactRecord,
    RetentionDecision,
    RetentionSettings,
    apply_retention,
    plan_retention,
)
from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.db.models import Artifact


def _record(
    *,
    kind: str,
    created_at: datetime,
    reason: str = "periodic",
    run_id: uuid.UUID | None = None,
) -> ArtifactRecord:
    """Build a fake inventory item."""
    return ArtifactRecord(
        id=uuid.uuid4(),
        kind=kind,
        storage_key="aa/bb/" + uuid.uuid4().hex.ljust(64, "0")[:64],
        created_at=created_at,
        run_id=run_id or uuid.uuid4(),
        metadata={"reason": reason},
    )


def test_old_screenshots_downsample_and_preserve_boundaries() -> None:
    """Old screenshots are sampled while important boundaries remain."""
    now = datetime(2026, 9, 21, tzinfo=UTC)
    run_id = uuid.uuid4()
    records = [
        _record(kind="screenshot", created_at=now - timedelta(days=10 + index), run_id=run_id)
        for index in range(4)
    ]
    records.append(
        _record(
            kind="screenshot",
            created_at=now - timedelta(days=30),
            reason="approval",
            run_id=run_id,
        )
    )
    decisions = plan_retention(
        records,
        settings=RetentionSettings(screenshot_after_days=7, screenshot_keep_every=2),
        now=now,
    )
    assert [decision.action for decision in decisions].count("soft_delete") == 2
    assert any(decision.reason == "boundary" for decision in decisions)


def test_defaults_keep_everything_and_non_blob_events_are_untouched() -> None:
    """Safe defaults retain artifacts and policy ignores logical event kinds."""
    now = datetime(2026, 9, 21, tzinfo=UTC)
    records = [
        _record(kind="screenshot", created_at=now - timedelta(days=999)),
        _record(kind="browser_state", created_at=now - timedelta(days=999)),
        _record(kind="model_request", created_at=now - timedelta(days=999)),
    ]
    decisions = plan_retention(records, settings=RetentionSettings(), now=now)
    assert all(decision.action == "keep" for decision in decisions)


def test_apply_soft_deletes_metadata_and_removes_unshared_blob(tmp_path: Path) -> None:
    """Applying a plan marks metadata and removes only the final blob ref."""
    store = FilesystemArtifactStore(tmp_path)
    key = store.put(b"old", media_type="image/webp", kind="screenshot").storage_key
    artifact = Artifact(
        id=uuid.uuid4(),
        kind="screenshot",
        storage_key=key,
        sha256="0" * 64,
        media_type="image/webp",
        size_bytes=3,
        metadata_={"reason": "periodic"},
    )
    record = ArtifactRecord(
        id=artifact.id,
        kind=artifact.kind,
        storage_key=artifact.storage_key,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        metadata=artifact.metadata_,
    )
    session = MagicMock()
    session.scalars.return_value.all.return_value = [artifact]
    result = apply_retention(
        [RetentionDecision(record, "soft_delete", "test")],
        session=session,
        store=store,
        now=datetime(2026, 9, 21, tzinfo=UTC),
    )
    assert artifact.metadata_["retention_deleted_at"]
    assert result.soft_deleted == 1
    assert result.blobs_removed == 1
    assert not store.exists(key)
    session.commit.assert_called_once()
