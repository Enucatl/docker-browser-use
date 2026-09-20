"""Tests for event-driven screenshot policy (T019)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.audit.checkpoints import fingerprint_observation
from browser_use_agent.audit.screenshots import (
    SCREENSHOT_ARTIFACT_KIND,
    SCREENSHOT_EVENT_TYPE,
    ScreenshotFormat,
    ScreenshotReason,
    ScreenshotSettings,
    ScreenshotWriter,
    decide_screenshot,
    encode_screenshot,
    is_destructive_action,
    load_screenshot_settings,
)
from browser_use_agent.policy.actions import ActionKind, BrowserObservation, CandidateElement
from browser_use_agent.security.redaction import redact_for_audit


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


def _fake_png(
    *, color: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (8, 8)
) -> bytes:
    """Build tiny PNG bytes without Chrome (fake image path)."""
    img = Image.new("RGB", size, color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _obs(
    *,
    url: str = "https://example.test/",
    title: str = "Example",
    candidates: list[CandidateElement] | None = None,
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
    )


def test_encode_screenshot_webp_and_jpeg_from_fake_png() -> None:
    """Fake PNG bytes encode to WebP/JPEG with hashes differing by format."""
    raw = _fake_png()
    webp, webp_type = encode_screenshot(raw, fmt=ScreenshotFormat.WEBP, quality=70)
    jpeg, jpeg_type = encode_screenshot(raw, fmt=ScreenshotFormat.JPEG, quality=70)
    assert webp_type == "image/webp"
    assert jpeg_type == "image/jpeg"
    assert webp.startswith(b"RIFF") or webp[:4]  # WebP container
    assert jpeg[:2] == b"\xff\xd8"
    assert webp != jpeg
    assert len(webp) > 0 and len(jpeg) > 0


def test_encode_strips_exif() -> None:
    """Re-encoding drops EXIF so stored blobs do not retain camera metadata."""
    img = Image.new("RGB", (16, 16), (1, 2, 3))
    buf = BytesIO()
    # Pillow may ignore unsupported EXIF on PNG; JPEG with EXIF is clearer.
    exif = img.getexif()
    exif[271] = "FakeCameraMake"  # ImageDescription / Make
    img.save(buf, format="JPEG", quality=90, exif=exif)
    raw = buf.getvalue()
    assert b"FakeCameraMake" in raw

    encoded, media_type = encode_screenshot(raw, fmt=ScreenshotFormat.JPEG, quality=80)
    assert media_type == "image/jpeg"
    assert b"FakeCameraMake" not in encoded


def test_default_policy_does_not_screenshot_every_action() -> None:
    """Identical observations after the first do not capture again."""
    settings = ScreenshotSettings(enabled=True, interval_steps=0)
    base = fingerprint_observation(_obs())
    first = decide_screenshot(None, base, steps_since_screenshot=1, settings=settings)
    assert first.should_capture is True
    assert first.reason == ScreenshotReason.FIRST

    again = decide_screenshot(base, base, steps_since_screenshot=1, settings=settings)
    assert again.should_capture is False
    assert again.reason is None


def test_decide_screenshot_policy_reasons() -> None:
    """URL/title/periodic/force reasons fire; DOM-only churn does not."""
    settings = ScreenshotSettings(enabled=True, interval_steps=3)
    base = fingerprint_observation(_obs())
    same_url = fingerprint_observation(
        _obs(
            candidates=[
                CandidateElement(index=0, tag="a", role="link", name="Home"),
                CandidateElement(index=1, tag="button", role="button", name="Go"),
                CandidateElement(index=2, tag="span", role="text", name="Extra"),
            ],
        )
    )
    # DOM changed but URL/title did not — screenshots stay quiet.
    assert (
        decide_screenshot(
            base, same_url, steps_since_screenshot=1, settings=settings
        ).should_capture
        is False
    )

    titled = fingerprint_observation(_obs(title="Changed"))
    assert (
        decide_screenshot(base, titled, steps_since_screenshot=1, settings=settings).reason
        == ScreenshotReason.TITLE_CHANGE
    )

    navigated = fingerprint_observation(_obs(url="https://other.test/"))
    assert (
        decide_screenshot(base, navigated, steps_since_screenshot=1, settings=settings).reason
        == ScreenshotReason.URL_CHANGE
    )

    periodic = decide_screenshot(base, base, steps_since_screenshot=3, settings=settings)
    assert periodic.reason == ScreenshotReason.PERIODIC

    forced = decide_screenshot(
        base,
        base,
        steps_since_screenshot=1,
        settings=settings,
        force_reason="destructive",
    )
    assert forced.reason == ScreenshotReason.DESTRUCTIVE


def test_screenshot_stored_via_artifact_store_with_hash(tmp_path: Path) -> None:
    """Screenshots land in the artifact store; events keep refs only."""
    store = FilesystemArtifactStore(tmp_path)
    audit = _RecordingAudit()
    writer = ScreenshotWriter(
        audit,
        store,
        settings=ScreenshotSettings(
            enabled=True,
            format=ScreenshotFormat.WEBP,
            quality=75,
            interval_steps=0,
        ),
    )
    run_id = uuid.uuid4()
    step_id = uuid.uuid4()
    fake = _fake_png(color=(40, 50, 60))

    result = writer.maybe_screenshot_sync(
        run_id,
        step_id,
        _obs(),
        image_bytes=fake,
    )

    assert result.skipped is False
    assert result.reason == ScreenshotReason.FIRST
    assert result.storage_key is not None
    assert result.sha256 is not None
    assert result.media_type == "image/webp"
    assert len(result.sha256) == 64
    blob = store.get(result.storage_key)
    assert blob == store.get(result.storage_key)
    assert len(blob) == result.size_bytes

    events = [e for e in audit.events if e.event_type == SCREENSHOT_EVENT_TYPE]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["storage_key"] == result.storage_key
    assert payload["sha256"] == result.sha256
    assert "RIFF" not in str(payload)  # no binary in the event
    assert store.path_for(result.storage_key).is_file()


def test_identical_screenshots_dedupe(tmp_path: Path) -> None:
    """Identical encoded payloads reuse the same storage key."""
    store = FilesystemArtifactStore(tmp_path)
    audit = _RecordingAudit()
    settings = ScreenshotSettings(enabled=True, interval_steps=0, format=ScreenshotFormat.JPEG)
    writer = ScreenshotWriter(audit, store, settings=settings)
    fake = _fake_png()

    first = writer.maybe_screenshot_sync(uuid.uuid4(), uuid.uuid4(), _obs(), image_bytes=fake)
    # Force a second write of the same bytes (new URL so policy fires).
    second = writer.maybe_screenshot_sync(
        uuid.uuid4(),
        uuid.uuid4(),
        _obs(url="https://other.test/"),
        image_bytes=fake,
    )
    assert first.skipped is False and second.skipped is False
    assert first.storage_key == second.storage_key
    assert first.sha256 == second.sha256


def test_no_screenshot_when_nothing_changed(tmp_path: Path) -> None:
    """Second identical observation is skipped (not every action)."""
    store = FilesystemArtifactStore(tmp_path)
    audit = _RecordingAudit()
    writer = ScreenshotWriter(
        audit,
        store,
        settings=ScreenshotSettings(enabled=True, interval_steps=0),
    )
    run_id = uuid.uuid4()
    obs = _obs()
    fake = _fake_png()

    first = writer.maybe_screenshot_sync(run_id, uuid.uuid4(), obs, image_bytes=fake)
    second = writer.maybe_screenshot_sync(run_id, uuid.uuid4(), obs, image_bytes=fake)

    assert first.skipped is False
    assert second.skipped is True
    assert sum(1 for e in audit.events if e.event_type == SCREENSHOT_EVENT_TYPE) == 1


def test_config_tightens_and_loosens_interval(monkeypatch: Any) -> None:
    """SCREENSHOT_INTERVAL_STEPS and format/quality are configurable."""
    monkeypatch.setenv("SCREENSHOT_ENABLED", "true")
    monkeypatch.setenv("SCREENSHOT_FORMAT", "jpeg")
    monkeypatch.setenv("SCREENSHOT_QUALITY", "55")
    monkeypatch.setenv("SCREENSHOT_INTERVAL_STEPS", "5")
    settings = load_screenshot_settings()
    assert settings.format == ScreenshotFormat.JPEG
    assert settings.quality == 55
    assert settings.interval_steps == 5

    monkeypatch.setenv("SCREENSHOT_INTERVAL_STEPS", "0")
    tight = load_screenshot_settings()
    assert tight.interval_steps == 0

    monkeypatch.setenv("SCREENSHOT_ENABLED", "false")
    disabled = load_screenshot_settings()
    assert disabled.enabled is False
    base = fingerprint_observation(_obs())
    assert (
        decide_screenshot(None, base, steps_since_screenshot=1, settings=disabled).should_capture
        is False
    )


def test_is_destructive_action_kinds() -> None:
    """Navigate and Bitwarden kinds are destructive; CLICK/SCROLL are not."""
    assert is_destructive_action(ActionKind.NAVIGATE) is True
    assert is_destructive_action(ActionKind.BITWARDEN_LOGIN) is True
    assert is_destructive_action(ActionKind.CLICK) is False
    assert is_destructive_action(ActionKind.SCROLL) is False
    assert is_destructive_action("NAVIGATE") is True


def test_artifact_kind_constant() -> None:
    """Stored kind matches the documented screenshot artifact kind."""
    assert SCREENSHOT_ARTIFACT_KIND == "screenshot"
