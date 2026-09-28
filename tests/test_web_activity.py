"""Run activity contracts and safe artifact access for the operator UI."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web_ui import api_client as api_client
from test_web_ui import auth_client as auth_client
from test_web_ui import postgres_url as postgres_url

from browser_use_agent.api.events_bus import message_from_event
from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.db.models import AgentEvent, Artifact
from browser_use_agent.services.runs import create_run
from browser_use_agent.web.routes import _event_row


def test_display_omits_model_reasoning_and_matches_live() -> None:
    """Bootstrap and WS share observable details with raw reasoning closed away."""
    event = AgentEvent(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step_id=uuid.uuid4(),
        seq=1,
        event_type="decision",
        actor="agent",
        occurred_at=datetime.now(UTC),
        url="https://example.test",
        duration_ms=42,
        metadata_={
            "kind": "CLICK",
            "target_index": 0,
            "model": "test-model",
            "rationale": "private rationale",
            "raw_answers": {"thought": "secret"},
            "params": {"text": "private input"},
        },
    )
    initial = _event_row(event)
    live = message_from_event(event, source="live").to_dict()
    assert initial["display"] == live["display"]
    assert initial["step_id"] == live["step_id"] == str(event.step_id)
    assert initial["display"]["details"] == {
        "Action": "CLICK",
        "Element": 0,
        "Model": "test-model",
        "URL": "https://example.test",
        "Duration (ms)": 42,
    }
    assert "private" not in str(initial["display"])
    assert "secret" not in str(initial["display"])
    assert initial["payload"]["rationale"] == "private rationale"
    event.event_type = "unknown_event"
    assert _event_row(event)["display"]["category"] == "other"


def test_activity_counts_full_run_and_preserves_older_screenshot(
    api_client: TestClient,
    tmp_path: Path,
) -> None:
    """Totals and latest screenshot remain accurate beyond the display limits."""
    store = FilesystemArtifactStore(tmp_path)
    with api_client.app.state.session_factory() as session:
        run = create_run(session, "recorded activity")
        run.started_at = datetime.now(UTC) - timedelta(seconds=20)
        run.finished_at = run.started_at + timedelta(seconds=10)
        run.status = "succeeded"
        writer = AuditWriter(session)
        step_id = uuid.uuid4()
        screenshot = store.put(
            b"image",
            kind="screenshot",
            media_type="image/png",
            run_id=run.id,
            metadata={"step_id": str(step_id), "url": "https://example.test", "title": "Example"},
            session=session,
        )
        artifact = session.get(Artifact, screenshot.artifact_id)
        assert artifact is not None
        artifact.created_at = datetime.now(UTC) - timedelta(days=1)
        shot_event = writer.append(
            run.id,
            "screenshot_captured",
            {"artifact_id": str(screenshot.artifact_id)},
            step_id=step_id,
        )
        screenshot_seq = shot_event.seq
        for index in range(101):
            event = writer.append(run.id, "action_requested", {"kind": "CLICK"}, step_id=step_id)
            store.put(
                str(index).encode(),
                kind="download",
                media_type="text/plain",
                run_id=run.id,
                event_id=event.id,
                session=session,
            )
        writer.append(run.id, "action_completed", {"kind": "CLICK"}, step_id=uuid.uuid4())
        session.commit()
        run_id = run.id
    response = api_client.get(f"/runs/{run_id}/activity")
    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == {"steps": 2, "actions": 101, "artifacts": 102, "elapsed_seconds": 10}
    assert len(data["events"]) == data["event_limit"] == 100
    assert len(data["artifacts"]) == data["artifact_limit"] == 100
    assert data["events"][0]["seq"] < data["events"][-1]["seq"]
    assert data["latest_screenshot"]["id"] == str(screenshot.artifact_id)
    assert data["latest_screenshot"]["event_seq"] == screenshot_seq
    assert data["latest_screenshot"]["step_id"] == str(step_id)
    assert data["latest_screenshot"]["preview_url"].endswith("?preview=1")
    assert all(item["event_seq"] is not None for item in data["artifacts"])
    assert data["status"] == "succeeded"
    assert data["cost"] is None
    assert api_client.get(f"/runs/{uuid.uuid4()}/activity").status_code == 404


def test_artifacts_are_scoped_downloads_and_raster_only_previews(
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-run IDs, retained-away blobs, unsafe previews and keys are denied."""
    monkeypatch.setenv("ARTIFACTS_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_STORE", "fs")
    store = FilesystemArtifactStore(tmp_path)
    with api_client.app.state.session_factory() as session:
        run = create_run(session, "artifacts")
        other = create_run(session, "other")
        png = store.put(
            b"\x89PNG\r\n\x1a\n",
            kind="screenshot",
            media_type="image/png",
            run_id=run.id,
            session=session,
        )
        html = store.put(
            b"<script>alert(1)</script>",
            kind="download",
            media_type="text/html",
            run_id=run.id,
            session=session,
        )
        session.commit()
        run_id, other_id = run.id, other.id
    png_url = f"/runs/{run_id}/artifacts/{png.artifact_id}"
    html_url = f"/runs/{run_id}/artifacts/{html.artifact_id}"
    assert api_client.get(f"/runs/{other_id}/artifacts/{png.artifact_id}").status_code == 404
    response = api_client.get(png_url + "?preview=1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    download = api_client.get(html_url)
    assert download.content == b"<script>alert(1)</script>"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["content-type"] == "application/octet-stream"
    assert api_client.get(html_url + "?preview=1").status_code == 415
    store.delete(png.storage_key)
    assert api_client.get(png_url).status_code == 404
    with api_client.app.state.session_factory() as session:
        artifact = session.get(Artifact, html.artifact_id)
        assert artifact is not None
        artifact.storage_key = "../../etc/passwd"
        session.commit()
    assert api_client.get(html_url).status_code == 404


def test_artifact_and_activity_routes_require_identity(auth_client: TestClient) -> None:
    """New UI data routes use the same authentication as the rest of the UI."""
    path = f"/runs/{uuid.uuid4()}"
    assert auth_client.get(f"{path}/activity").status_code == 401
    assert auth_client.get(f"{path}/artifacts/{uuid.uuid4()}").status_code == 401


@pytest.mark.parametrize(
    ("kind", "requested", "completed"),
    [
        ("CLICK", "Click requested", "Clicked"),
        ("TYPE_TEXT", "Text entry requested", "Text entered"),
        ("SCROLL", "Scroll requested", "Scrolled"),
        ("GO_BACK", "Back navigation requested", "Went back"),
        ("NAVIGATE", "Navigation requested", "Navigated"),
        ("SWITCH_TAB", "Tab switch requested", "Tab switched"),
        ("DONE", "Task completion requested", "Task action completed"),
        ("BITWARDEN_LOGIN", "Login fill requested", "Login filled"),
        ("BITWARDEN_IDENTITY", "Identity fill requested", "Identity filled"),
        ("BITWARDEN_CARD", "Card fill requested", "Card filled"),
        ("click", "Click requested", "Clicked"),
        ("FUTURE_ACTION", "Browser action started", "Browser action completed"),
        ({"unexpected": "shape"}, "Browser action started", "Browser action completed"),
    ],
)
def test_action_labels_describe_requested_and_completed_operations(
    kind: object,
    requested: str,
    completed: str,
) -> None:
    """Known operations get distinct human titles without guessing unknown kinds."""
    event = AgentEvent(
        seq=1,
        event_type="action_requested",
        actor="agent",
        occurred_at=datetime.now(UTC),
        metadata_={"kind": kind, "message": "Actual recorded outcome"},
    )
    assert _event_row(event)["display"]["title"] == requested
    event.event_type = "action_completed"
    display = _event_row(event)["display"]
    assert display["title"] == completed
    assert display["summary"] == "Actual recorded outcome"
    assert display["tone"] == "success"


@pytest.mark.parametrize(
    ("event_type", "title", "summary"),
    [
        ("browser_started", "Browser connected", "The agent connected to the persistent browser."),
        ("browser_stopped", "Browser detached", "The agent detached from the browser."),
    ],
)
def test_browser_lifecycle_describes_connection_not_process_shutdown(
    event_type: str,
    title: str,
    summary: str,
) -> None:
    """Persistent browser attach/detach events never imply closing Chromium."""
    event = AgentEvent(
        seq=1,
        event_type=event_type,
        actor="system",
        occurred_at=datetime.now(UTC),
        metadata_={"mode": "detach", "cdp_url": "internal-address", "user_data_dir": "/private"},
    )
    display = _event_row(event)["display"]
    assert display["title"] == title
    assert display["summary"] == summary
    assert display["category"] == "browser"
    assert "internal-address" not in str(display)
    assert "/private" not in str(display)
