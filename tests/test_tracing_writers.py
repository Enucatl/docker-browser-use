"""Tests for model-call and browser-action tracing writers (T017)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from browser_use_agent.agent.browser_port import FakeBrowserPort
from browser_use_agent.agent.loop import AgentLoop
from browser_use_agent.artifacts.store import FilesystemArtifactStore
from browser_use_agent.audit.browser_actions import BrowserActionWriter
from browser_use_agent.audit.model_calls import ModelCallWriter
from browser_use_agent.audit.writer import AuditWriter
from browser_use_agent.db.migrate import upgrade_head
from browser_use_agent.db.models import BrowserAction, CostEntry, ModelCall, Run
from browser_use_agent.policy.actions import (
    BrowserObservation,
    CandidateElement,
)
from browser_use_agent.policy.jev_client import FakeDecision, FakeJevClient
from browser_use_agent.policy.text_llm import FakeTextLLMClient
from browser_use_agent.runs.status import RunStatus
from browser_use_agent.security.redaction import REDACTED


def _docker_available() -> bool:
    """Return True when the Docker CLI can talk to a daemon."""
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """Yield a SQLAlchemy URL for an empty Postgres 18 database."""
    existing = os.environ.get("TEST_DATABASE_URL")
    if existing:
        yield existing
        return

    if not _docker_available():
        pytest.skip("Docker is required for tracing writer tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-trace-{uuid.uuid4().hex[:8]}"
    password = "testpass"
    user = "browser_use"
    dbname = "browser_use"

    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_USER={user}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            f"POSTGRES_DB={dbname}",
            "-P",
            "postgres:18",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(f"Could not start postgres:18 container: {run.stderr.strip()}")

    port_proc = subprocess.run(
        ["docker", "port", name, "5432/tcp"],
        check=False,
        capture_output=True,
        text=True,
    )
    if port_proc.returncode != 0 or not port_proc.stdout.strip():
        subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)
        pytest.skip(f"Could not resolve published Postgres port: {port_proc.stderr.strip()}")

    host_port = port_proc.stdout.strip().rsplit(":", 1)[-1]
    url = f"postgresql+psycopg://{user}:{password}@127.0.0.1:{host_port}/{dbname}"
    deadline = time.time() + 60
    last_error: Exception | None = None
    try:
        while time.time() < deadline:
            try:
                engine = create_engine(url, pool_pre_ping=True)
                with engine.connect() as conn:
                    conn.execute(select(1))
                engine.dispose()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        else:
            subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)
            pytest.skip(f"Postgres did not become ready: {last_error}")

        upgrade_head(database_url=url)
        yield url
    finally:
        subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)


@pytest.fixture
def db_session(postgres_url: str) -> Iterator[Session]:
    """Yield a SQLAlchemy session against the migrated test database."""
    engine = create_engine(postgres_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()


def _create_run(session: Session, *, goal: str = "trace test") -> Run:
    """Insert a minimal run row.

    Args:
        session: Active session.
        goal: Run goal text.

    Returns:
        Persisted run.
    """
    run = Run(id=uuid.uuid4(), goal=goal, status=RunStatus.RUNNING.value)
    session.add(run)
    session.flush()
    return run


def test_model_call_writer_redacts_and_links_event(db_session: Session) -> None:
    """ModelCallWriter persists redacted fields linked to agent_events.seq."""
    run = _create_run(db_session)
    audit = AuditWriter(db_session)
    event = audit.append(
        run.id,
        "model_call",
        {"call_kind": "text_llm"},
        actor="agent",
        step_id=uuid.uuid4(),
    )
    writer = ModelCallWriter(db_session)
    row = writer.record(
        event=event,
        call_kind="text_llm",
        provider="openrouter",
        model_name="gpt-test",
        status="ok",
        retries=1,
        request_id="req-123",
        prompt_tokens=10,
        completion_tokens=4,
        latency_ms=42,
        cost_usd=0.001,
        request_meta={"messages": [{"role": "user", "content": "hi"}], "password": "secret"},
        response_meta={"parsed_output": {"text": "hello"}, "api_key": "sk-leak"},
    )
    db_session.commit()

    loaded = db_session.get(ModelCall, row.id)
    assert loaded is not None
    assert loaded.event_id == event.id
    assert loaded.event_seq == event.seq
    assert loaded.step_id == event.step_id
    assert loaded.call_kind == "text_llm"
    assert loaded.provider == "openrouter"
    assert loaded.model_name == "gpt-test"
    assert loaded.retries == 1
    assert loaded.request_id == "req-123"
    assert loaded.latency_ms == 42
    assert loaded.request_meta.get("password") == REDACTED
    assert loaded.response_meta.get("api_key") == REDACTED
    assert loaded.response_meta["parsed_output"]["text"] == "hello"


def test_model_call_writer_offloads_large_payload(
    db_session: Session,
    tmp_path: Path,
) -> None:
    """Large request bodies become artifact refs instead of inline megabytes."""
    run = _create_run(db_session)
    audit = AuditWriter(db_session)
    event = audit.append(run.id, "model_call", {"call_kind": "jev"}, actor="agent")
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    writer = ModelCallWriter(db_session, artifact_store=store, inline_limit_bytes=256)
    huge = {"prompt": "x" * 2000, "token": "should-redact"}
    row = writer.record(
        event=event,
        call_kind="jev",
        provider="jev",
        model_name="jev-fake",
        request_meta=huge,
        response_meta={"ok": True},
    )
    db_session.commit()

    assert row.request_artifact_id is not None
    assert row.request_meta.get("offloaded") is True
    assert "artifact_id" in row.request_meta
    assert row.response_artifact_id is None
    assert row.response_meta == {"ok": True}


def test_browser_action_writer_redacts_and_links_event(db_session: Session) -> None:
    """BrowserActionWriter stores forensic fields linked to the audit event."""
    run = _create_run(db_session)
    audit = AuditWriter(db_session)
    event = audit.append(
        run.id,
        "action_completed",
        {"kind": "CLICK"},
        actor="agent",
        step_id=uuid.uuid4(),
        url="https://example.test/page",
        duration_ms=15,
    )
    writer = BrowserActionWriter(db_session)
    row = writer.record(
        event=event,
        action_type="CLICK",
        status="completed",
        target='button "Submit"',
        title="Example",
        element_index=3,
        page_changed=False,
        result="clicked",
        metadata={
            "accessible_name": "Submit",
            "role": "button",
            "attributes": {"tag": "button"},
            "bounds": {"x": 1, "y": 2, "w": 10, "h": 4},
            "password": "nope",
        },
    )
    db_session.commit()

    loaded = db_session.get(BrowserAction, row.id)
    assert loaded is not None
    assert loaded.event_id == event.id
    assert loaded.event_seq == event.seq
    assert loaded.step_id == event.step_id
    assert loaded.action_type == "CLICK"
    assert loaded.element_index == 3
    assert loaded.url == "https://example.test/page"
    assert loaded.title == "Example"
    assert loaded.page_changed is False
    assert loaded.metadata_["accessible_name"] == "Submit"
    assert loaded.metadata_["password"] == REDACTED


def test_fake_run_writes_model_calls_and_browser_actions(
    db_session: Session,
    tmp_path: Path,
) -> None:
    """A completed fake AgentLoop run produces queryable tracing rows."""
    run = _create_run(db_session, goal="click then done")
    observation = BrowserObservation(
        url="https://example.test/",
        title="Home",
        candidates=[
            CandidateElement(index=0, tag="button", role="button", name="Go"),
        ],
    )
    browser = FakeBrowserPort([observation])
    jev = FakeJevClient(
        [
            FakeDecision(operation="CLICK", target_key="0"),
            FakeDecision(operation="DONE", done_message="finished"),
        ]
    )
    audit = AuditWriter(db_session)
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    model_calls = ModelCallWriter(db_session, artifact_store=store)
    browser_actions = BrowserActionWriter(db_session)

    loop = AgentLoop(
        run_id=run.id,
        goal=run.goal,
        browser=browser,
        jev=jev,
        audit=audit,
        model_calls=model_calls,
        browser_actions=browser_actions,
        text_llm=FakeTextLLMClient(texts=("unused",)),
        max_steps=5,
        commit=db_session.commit,
    )
    outcome = asyncio.run(loop.run())
    assert outcome.status == RunStatus.SUCCEEDED

    calls = list(db_session.scalars(select(ModelCall).where(ModelCall.run_id == run.id)).all())
    costs = list(db_session.scalars(select(CostEntry).where(CostEntry.run_id == run.id)).all())
    actions = list(
        db_session.scalars(select(BrowserAction).where(BrowserAction.run_id == run.id)).all()
    )

    assert any(c.call_kind == "jev" for c in calls)
    assert len(costs) == len(calls)
    assert sum(cost.amount for cost in costs) > 0
    jev_call = next(c for c in calls if c.call_kind == "jev")
    assert jev_call.event_id is not None
    assert jev_call.event_seq is not None
    assert jev_call.response_meta.get("selected_action") == "CLICK"
    assert "probabilities" in jev_call.response_meta

    assert any(a.action_type == "CLICK" and a.status == "completed" for a in actions)
    click = next(a for a in actions if a.action_type == "CLICK" and a.status == "completed")
    assert click.element_index == 0
    assert click.metadata_.get("accessible_name") == "Go"
    assert click.metadata_.get("role") == "button"
    assert click.event_seq is not None


def test_type_text_model_call_row(db_session: Session) -> None:
    """TYPE_TEXT via text LLM produces a text_llm model_calls row."""
    run = _create_run(db_session, goal="type invoice month")
    observation = BrowserObservation(
        url="https://example.test/form",
        title="Form",
        candidates=[
            CandidateElement(
                index=2,
                tag="input",
                role="textbox",
                name="Month",
                is_editable=True,
            ),
        ],
    )
    browser = FakeBrowserPort([observation])
    jev = FakeJevClient(
        [
            FakeDecision(operation="TYPE_TEXT", target_key="2"),
            FakeDecision(operation="DONE", done_message="typed"),
        ]
    )
    audit = AuditWriter(db_session)
    model_calls = ModelCallWriter(db_session)
    browser_actions = BrowserActionWriter(db_session)

    loop = AgentLoop(
        run_id=run.id,
        goal=run.goal,
        browser=browser,
        jev=jev,
        audit=audit,
        model_calls=model_calls,
        browser_actions=browser_actions,
        text_llm=FakeTextLLMClient(texts=("2024-01",)),
        max_steps=5,
        commit=db_session.commit,
    )
    outcome = asyncio.run(loop.run())
    assert outcome.status == RunStatus.SUCCEEDED

    calls = list(db_session.scalars(select(ModelCall).where(ModelCall.run_id == run.id)).all())
    kinds = {c.call_kind for c in calls}
    assert "jev" in kinds
    assert "text_llm" in kinds
    text_call = next(c for c in calls if c.call_kind == "text_llm")
    assert text_call.status == "ok"
    assert text_call.response_meta.get("parsed_output", {}).get("text") == "2024-01"
