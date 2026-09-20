"""Tests for compressed browser-state checkpoints (T018)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.audit.checkpoints import (
    CHECKPOINT_EVENT_TYPE,
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointReason,
    CheckpointSettings,
    CheckpointWriter,
    StateFingerprint,
    build_checkpoint_payload,
    compute_dom_hash,
    decide_checkpoint,
    fingerprint_observation,
    load_checkpoint_payload,
    observation_from_checkpoint,
    serialize_checkpoint,
)
from browser_use_agent.policy.actions import BrowserObservation, CandidateElement
from browser_use_agent.security.redaction import REDACTED, redact_for_audit


@dataclass
class _RecordedEvent:
    """One audit event captured by :class:`_RecordingAudit`."""

    run_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    actor: str
    step_id: uuid.UUID | None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class _RecordingAudit:
    """In-memory audit sink that redacts payloads."""

    def __init__(self) -> None:
        """Create an empty recording writer."""
        self.events: list[_RecordedEvent] = []

    def append(
        self,
        run_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        step_id: uuid.UUID | None = None,
        parent_event_id: uuid.UUID | None = None,
        url: str | None = None,
        tab_id: str | None = None,
        duration_ms: int | None = None,
        occurred_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
    ) -> _RecordedEvent:
        """Append a redacted event."""
        del parent_event_id, url, tab_id, duration_ms, occurred_at
        raw = dict(payload or {})
        redacted = redact_for_audit(raw)
        if not isinstance(redacted, dict):
            redacted = {"value": redacted}
        event = _RecordedEvent(
            run_id=run_id,
            event_type=event_type,
            payload=redacted,
            actor=actor,
            step_id=step_id,
            id=event_id or uuid.uuid4(),
        )
        self.events.append(event)
        return event


def _obs(
    *,
    url: str = "https://example.test/",
    title: str = "Example",
    candidates: list[CandidateElement] | None = None,
    browser_errors: list[str] | None = None,
) -> BrowserObservation:
    """Build a small observation fixture."""
    return BrowserObservation(
        url=url,
        title=title,
        candidates=candidates
        or [
            CandidateElement(index=0, tag="a", role="link", name="Home"),
            CandidateElement(index=1, tag="button", role="button", name="Go"),
        ],
        browser_errors=list(browser_errors or []),
    )


def test_no_checkpoint_when_nothing_changed(tmp_path: Path) -> None:
    """Identical observations after the first do not write another checkpoint."""
    store = FilesystemArtifactStore(tmp_path)
    audit = _RecordingAudit()
    writer = CheckpointWriter(
        audit,
        store,
        settings=CheckpointSettings(enabled=True, interval_steps=0, dom_change_ratio=0.25),
    )
    run_id = uuid.uuid4()
    step_a = uuid.uuid4()
    step_b = uuid.uuid4()
    observation = _obs()

    first = writer.maybe_checkpoint(run_id, step_a, observation)
    second = writer.maybe_checkpoint(run_id, step_b, observation)

    assert first.skipped is False
    assert first.reason == CheckpointReason.FIRST
    assert second.skipped is True
    assert sum(1 for e in audit.events if e.event_type == CHECKPOINT_EVENT_TYPE) == 1


def test_checkpoint_on_url_change(tmp_path: Path) -> None:
    """A URL change triggers a new checkpoint with a small event payload."""
    store = FilesystemArtifactStore(tmp_path)
    audit = _RecordingAudit()
    writer = CheckpointWriter(
        audit,
        store,
        settings=CheckpointSettings(enabled=True, interval_steps=0, dom_change_ratio=0.25),
    )
    run_id = uuid.uuid4()
    first_obs = _obs(url="https://example.test/a")
    second_obs = _obs(url="https://example.test/b")

    writer.maybe_checkpoint(run_id, uuid.uuid4(), first_obs)
    result = writer.maybe_checkpoint(run_id, uuid.uuid4(), second_obs)

    assert result.skipped is False
    assert result.reason == CheckpointReason.URL_CHANGE
    assert result.storage_key is not None
    events = [e for e in audit.events if e.event_type == CHECKPOINT_EVENT_TYPE]
    assert len(events) == 2
    payload = events[-1].payload
    # Event stays small: refs only, no candidate dump.
    assert "candidates" not in payload
    assert "observation" not in payload
    assert payload["reason"] == CheckpointReason.URL_CHANGE.value
    assert payload["storage_key"] == result.storage_key
    assert payload["sha256"] == result.sha256
    assert len(payload["sha256"]) == 64


def test_replay_observation_from_artifact_id(tmp_path: Path) -> None:
    """Replay-relevant observation can be loaded from the stored artifact."""
    store = FilesystemArtifactStore(tmp_path)
    audit = _RecordingAudit()
    writer = CheckpointWriter(
        audit,
        store,
        settings=CheckpointSettings(enabled=True, interval_steps=0),
    )
    run_id = uuid.uuid4()
    observation = _obs(
        url="https://example.test/form",
        title="Login",
        candidates=[
            CandidateElement(
                index=0, tag="input", input_type="email", is_editable=True, name="Email"
            ),
            CandidateElement(
                index=1,
                tag="input",
                input_type="password",
                is_password_field=True,
                is_editable=True,
                name="Password",
            ),
        ],
    )
    result = writer.maybe_checkpoint(run_id, uuid.uuid4(), observation)
    assert result.storage_key is not None

    envelope = writer.load_by_storage_key(result.storage_key)
    assert envelope["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    restored = observation_from_checkpoint(envelope)
    assert restored.url == observation.url
    assert restored.title == observation.title
    assert len(restored.candidates) == 2
    assert restored.candidates[1].is_password_field is True


def test_secrets_redacted_before_serialize() -> None:
    """Checkpoint serialization redacts secret-looking fields."""
    observation = _obs()
    fingerprint = fingerprint_observation(observation)
    payload = build_checkpoint_payload(
        observation,
        run_id=uuid.uuid4(),
        step_id=uuid.uuid4(),
        reason=CheckpointReason.FIRST,
        fingerprint=fingerprint,
        extra={"password": "super-secret", "token": "abc"},
    )
    compressed = serialize_checkpoint(payload)
    loaded = load_checkpoint_payload(compressed)
    assert loaded["extra"]["password"] == REDACTED
    assert loaded["extra"]["token"] == REDACTED


def test_decide_checkpoint_policy_reasons() -> None:
    """Heuristic covers first, title, major DOM, browser error, and periodic."""
    settings = CheckpointSettings(enabled=True, interval_steps=3, dom_change_ratio=0.25)
    base = fingerprint_observation(_obs())

    assert decide_checkpoint(None, base, steps_since_checkpoint=1, settings=settings).reason == (
        CheckpointReason.FIRST
    )

    same = decide_checkpoint(base, base, steps_since_checkpoint=1, settings=settings)
    assert same.should_write is False

    titled = StateFingerprint(
        url=base.url,
        title="Other",
        dom_hash=base.dom_hash,
        candidate_signatures=base.candidate_signatures,
        browser_error_count=base.browser_error_count,
    )
    assert (
        decide_checkpoint(base, titled, steps_since_checkpoint=1, settings=settings).reason
        == CheckpointReason.TITLE_CHANGE
    )

    major = fingerprint_observation(
        _obs(
            candidates=[
                CandidateElement(index=10, tag="div", name="A"),
                CandidateElement(index=11, tag="div", name="B"),
                CandidateElement(index=12, tag="div", name="C"),
            ]
        )
    )
    assert major.dom_hash != base.dom_hash
    assert (
        decide_checkpoint(base, major, steps_since_checkpoint=1, settings=settings).reason
        == CheckpointReason.DOM_HASH_CHANGE
    )

    errored = StateFingerprint(
        url=base.url,
        title=base.title,
        dom_hash=base.dom_hash,
        candidate_signatures=base.candidate_signatures,
        browser_error_count=base.browser_error_count + 1,
    )
    assert (
        decide_checkpoint(base, errored, steps_since_checkpoint=1, settings=settings).reason
        == CheckpointReason.BROWSER_ERROR
    )

    periodic = decide_checkpoint(base, base, steps_since_checkpoint=3, settings=settings)
    assert periodic.reason == CheckpointReason.PERIODIC

    forced = decide_checkpoint(
        base,
        base,
        steps_since_checkpoint=1,
        settings=settings,
        force_reason="approval",
    )
    assert forced.reason == CheckpointReason.APPROVAL


def test_identical_payloads_dedupe(tmp_path: Path) -> None:
    """Forcing two identical checkpoints reuses the same storage key."""
    store = FilesystemArtifactStore(tmp_path)
    run_id = uuid.uuid4()
    observation = _obs()
    # captured_at would differ across writer calls; put a fixed envelope twice.
    fp = fingerprint_observation(observation)
    envelope = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "kind": "browser_state",
        "reason": "first",
        "run_id": str(run_id),
        "step_id": str(uuid.uuid4()),
        "captured_at": "2026-01-01T00:00:00+00:00",
        "fingerprint": {
            "url": fp.url,
            "title": fp.title,
            "dom_hash": fp.dom_hash,
            "browser_error_count": fp.browser_error_count,
            "candidate_count": len(fp.candidate_signatures),
        },
        "observation": observation.model_dump(mode="json"),
    }
    blob = serialize_checkpoint(envelope)
    first = store.put(blob, media_type="application/zstd+json", kind="browser_state")
    second = store.put(blob, media_type="application/zstd+json", kind="browser_state")
    assert first.storage_key == second.storage_key
    assert first.wrote_blob is True
    assert second.wrote_blob is False


def test_dom_hash_stable_for_same_candidates() -> None:
    """DOM hash is deterministic for equal candidate structures."""
    a = _obs()
    b = _obs()
    assert compute_dom_hash(a) == compute_dom_hash(b)
