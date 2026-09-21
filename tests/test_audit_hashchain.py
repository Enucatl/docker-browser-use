"""Tests for audit hash chaining and AuditWriter (T009)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from browser_use_agent.audit import (
    GENESIS_PREV_HASH,
    AuditWriter,
    AuditWriterError,
    canonicalize_event_fields,
    compute_event_hash,
    event_hash_fields,
    verify_events_chain,
    verify_run_chain,
)
from browser_use_agent.db.migrate import upgrade_head
from browser_use_agent.db.models import AgentEvent, Run
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
        pytest.skip("Docker is required for audit writer tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-audit-{uuid.uuid4().hex[:8]}"
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
                    conn.execute(text("SELECT 1"))
                engine.dispose()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        else:
            pytest.fail(f"Postgres container did not become ready: {last_error}")

        yield url
    finally:
        subprocess.run(["docker", "stop", "-t", "2", name], check=False, capture_output=True)


@pytest.fixture
def db_session(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """Migrated database session; truncates audit tables between tests."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    upgrade_head(database_url=postgres_url)

    engine = create_engine(postgres_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        session.execute(text("TRUNCATE runs CASCADE"))
        session.commit()
        yield session
        session.rollback()
    finally:
        session.close()
        engine.dispose()


def _make_run(session: Session, goal: str = "audit test") -> uuid.UUID:
    """Insert a run row and return its id."""
    run_id = uuid.uuid4()
    session.add(Run(id=run_id, goal=goal))
    session.flush()
    return run_id


def test_canonicalize_sorts_keys_and_utc() -> None:
    """Canonical JSON sorts keys and normalizes datetimes to UTC Z."""
    when = datetime(2024, 1, 2, 3, 4, 5, 6000, tzinfo=UTC)
    fields = event_hash_fields(
        run_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        seq=1,
        event_type="task_received",
        occurred_at=when,
        actor="system",
        metadata={"z": 1, "a": {"b": 2}},
        prev_hash=GENESIS_PREV_HASH,
    )
    text = canonicalize_event_fields(fields)
    assert text.index('"a"') < text.index('"z"')
    assert "2024-01-02T03:04:05.006000Z" in text
    assert "event_hash" not in text
    assert '"id"' not in text


def test_compute_event_hash_is_stable() -> None:
    """Identical canonical fields yield the same SHA-256 digest."""
    when = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    run_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    fields = event_hash_fields(
        run_id=run_id,
        seq=1,
        event_type="page_opened",
        occurred_at=when,
        actor="agent",
        metadata={"url_host": "example.com"},
        prev_hash=GENESIS_PREV_HASH,
        url="https://example.com/",
    )
    assert compute_event_hash(fields) == compute_event_hash(dict(fields))
    assert len(compute_event_hash(fields)) == 64


def test_verify_events_chain_detects_tampered_metadata() -> None:
    """In-memory chain verification fails when metadata is altered."""
    when = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    run_id = uuid.uuid4()
    fields = event_hash_fields(
        run_id=run_id,
        seq=1,
        event_type="task_received",
        occurred_at=when,
        actor="system",
        metadata={"goal": "ok"},
        prev_hash=GENESIS_PREV_HASH,
    )
    digest = compute_event_hash(fields)
    good = AgentEvent(
        id=uuid.uuid4(),
        run_id=run_id,
        seq=1,
        event_type="task_received",
        occurred_at=when,
        actor="system",
        metadata_={"goal": "ok"},
        prev_hash=GENESIS_PREV_HASH,
        event_hash=digest,
    )
    assert verify_events_chain([good]) is True

    bad = AgentEvent(
        id=good.id,
        run_id=run_id,
        seq=1,
        event_type="task_received",
        occurred_at=when,
        actor="system",
        metadata_={"goal": "tampered"},
        prev_hash=GENESIS_PREV_HASH,
        event_hash=digest,
    )
    assert verify_events_chain([bad]) is False


def test_append_redacts_and_chains(db_session: Session) -> None:
    """Append redacts secrets, assigns seq, and verifies the happy-path chain."""
    run_id = _make_run(db_session)
    writer = AuditWriter(db_session)

    with patch(
        "browser_use_agent.audit.writer.redact_for_audit",
        wraps=__import__(
            "browser_use_agent.security.redaction",
            fromlist=["redact_for_audit"],
        ).redact_for_audit,
    ) as mocked_redact:
        first = writer.append(
            run_id,
            "task_received",
            {"password": "s3cret", "note": "start"},
            actor="system",
        )
        second = writer.append(
            run_id,
            "page_opened",
            {"path": "/home"},
            actor="agent",
            url="https://example.com/home",
        )
        db_session.commit()

    assert mocked_redact.call_count == 2
    assert first.seq == 1
    assert first.prev_hash == GENESIS_PREV_HASH
    assert first.event_hash
    assert first.metadata_["password"] == REDACTED
    assert first.metadata_["note"] == "start"

    assert second.seq == 2
    assert second.prev_hash == first.event_hash
    assert verify_run_chain(db_session, run_id) is True


def test_append_unknown_run_raises(db_session: Session) -> None:
    """Appending to a missing run fails without inserting events."""
    writer = AuditWriter(db_session)
    with pytest.raises(AuditWriterError, match="does not exist"):
        writer.append(uuid.uuid4(), "task_received", {})
    db_session.rollback()


def test_append_invalid_actor_raises(db_session: Session) -> None:
    """Actors outside agent/human/system are rejected."""
    run_id = _make_run(db_session)
    writer = AuditWriter(db_session)
    with pytest.raises(AuditWriterError, match="actor"):
        writer.append(run_id, "task_received", {}, actor="robot")  # type: ignore[arg-type]
    db_session.rollback()


def test_events_are_insert_only(db_session: Session) -> None:
    """Database triggers reject UPDATE on agent_events."""
    run_id = _make_run(db_session)
    event = AuditWriter(db_session).append(run_id, "task_received", {"ok": True})
    db_session.commit()

    with pytest.raises(Exception, match="append-only"):
        with db_session.begin_nested():
            db_session.execute(
                text("UPDATE agent_events SET event_type = 'tampered' WHERE id = :id"),
                {"id": event.id},
            )
    db_session.rollback()


def test_tampered_row_fails_verification(db_session: Session) -> None:
    """Mutating a stored event (triggers off) breaks verify_run_chain."""
    run_id = _make_run(db_session)
    writer = AuditWriter(db_session)
    writer.append(run_id, "task_received", {"step": 1})
    writer.append(run_id, "decision", {"action": "click"})
    db_session.commit()
    assert verify_run_chain(db_session, run_id) is True

    db_session.execute(text("ALTER TABLE agent_events DISABLE TRIGGER USER"))
    db_session.execute(
        text(
            "UPDATE agent_events SET metadata = CAST(:meta AS jsonb) "
            "WHERE run_id = :run_id AND seq = 2"
        ),
        {"meta": '{"action": "tampered"}', "run_id": run_id},
    )
    db_session.execute(text("ALTER TABLE agent_events ENABLE TRIGGER USER"))
    db_session.commit()

    assert verify_run_chain(db_session, run_id) is False


def test_broken_prev_hash_fails_verification(db_session: Session) -> None:
    """A forged prev_hash link fails verification."""
    run_id = _make_run(db_session)
    writer = AuditWriter(db_session)
    first = writer.append(run_id, "task_received", {})
    writer.append(run_id, "page_opened", {})
    db_session.commit()

    db_session.execute(text("ALTER TABLE agent_events DISABLE TRIGGER USER"))
    db_session.execute(
        text("UPDATE agent_events SET prev_hash = :bogus WHERE run_id = :run_id AND seq = 2"),
        {"bogus": "a" * 64, "run_id": run_id},
    )
    db_session.execute(text("ALTER TABLE agent_events ENABLE TRIGGER USER"))
    db_session.commit()

    assert first.event_hash != "a" * 64
    assert verify_run_chain(db_session, run_id) is False
