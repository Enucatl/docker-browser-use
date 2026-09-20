"""Integration tests: apply Alembic migrations against Postgres 18."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from browser_use_agent.db.migrate import upgrade_head

REQUIRED_TABLES = frozenset(
    {
        "runs",
        "agent_events",
        "agent_decisions",
        "model_calls",
        "browser_actions",
        "page_visits",
        "human_approvals",
        "cost_entries",
        "errors",
        "artifacts",
        "alembic_version",
    }
)


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
    """Yield a SQLAlchemy URL for an empty Postgres 18 database.

    Uses ``TEST_DATABASE_URL`` when set; otherwise starts an ephemeral
    ``postgres:18`` container via Docker.
    """
    existing = os.environ.get("TEST_DATABASE_URL")
    if existing:
        yield existing
        return

    if not _docker_available():
        pytest.skip("Docker is required for schema migration tests (or set TEST_DATABASE_URL)")

    name = f"browser-use-migrate-{uuid.uuid4().hex[:8]}"
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

    # docker port prints e.g. 0.0.0.0:32768
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


def test_upgrade_head_creates_schema(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Migrations apply cleanly and create all required tables."""
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.setenv("DATABASE_URL", postgres_url)

    upgrade_head(database_url=postgres_url)

    engine = create_engine(postgres_url)
    try:
        tables = set(inspect(engine).get_table_names())
        missing = REQUIRED_TABLES - tables
        assert not missing, f"missing tables: {sorted(missing)}"

        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == "0001_event_sourcing"

            # Append-only: INSERT works; UPDATE/DELETE are rejected by triggers.
            run_id = uuid.uuid4()
            event_id = uuid.uuid4()
            conn.execute(
                text("INSERT INTO runs (id, goal) VALUES (:id, :goal)"),
                {"id": run_id, "goal": "migrate test"},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_events (id, run_id, seq, event_type, actor) "
                    "VALUES (:id, :run_id, 1, 'task_received', 'system')"
                ),
                {"id": event_id, "run_id": run_id},
            )
            conn.commit()

            with pytest.raises(Exception, match="append-only"):
                with conn.begin():
                    conn.execute(
                        text("UPDATE agent_events SET event_type = 'tampered' WHERE id = :id"),
                        {"id": event_id},
                    )

            with pytest.raises(Exception, match="append-only"):
                with conn.begin():
                    conn.execute(
                        text("DELETE FROM agent_events WHERE id = :id"),
                        {"id": event_id},
                    )
    finally:
        engine.dispose()


def test_alembic_ini_present() -> None:
    """Project root ships Alembic config next to the package sources."""
    root = Path(__file__).resolve().parents[1]
    assert (root / "alembic.ini").is_file()
    assert (root / "alembic" / "versions" / "0001_event_sourcing.py").is_file()
