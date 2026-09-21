"""Alembic migration runner for the audit schema."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from alembic.config import Config

from alembic import command
from browser_use_agent.db.engine import build_sqlalchemy_url
from browser_use_agent.db.settings import DatabaseSettings


def find_alembic_root() -> Path:
    """Locate the directory that contains ``alembic.ini``.

    Search order: ``BROWSER_USE_ROOT`` / ``ALEMBIC_CONFIG``, process cwd,
    parents of this module, then ``/app`` (container convention).

    Returns:
        Directory containing ``alembic.ini``.

    Raises:
        FileNotFoundError: When no ``alembic.ini`` can be found.
    """
    env = os.environ.get("BROWSER_USE_ROOT") or os.environ.get("ALEMBIC_CONFIG")
    if env:
        path = Path(env).expanduser().resolve()
        if path.is_file() and path.name == "alembic.ini":
            return path.parent
        if (path / "alembic.ini").is_file():
            return path

    candidates = [Path.cwd(), *Path(__file__).resolve().parents, Path("/app")]
    for base in candidates:
        if (base / "alembic.ini").is_file():
            return base

    msg = (
        "alembic.ini not found; set BROWSER_USE_ROOT to the project root "
        "or run from a checkout that contains alembic.ini"
    )
    raise FileNotFoundError(msg)


def alembic_config(database_url: str | None = None) -> Config:
    """Build an Alembic config pointed at this repository.

    Args:
        database_url: Optional SQLAlchemy URL override for ``sqlalchemy.url``.

    Returns:
        Configured Alembic ``Config`` instance.
    """
    root = find_alembic_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def normalize_sqlalchemy_url(url: str) -> str:
    """Normalize a Postgres URL to the SQLAlchemy psycopg driver form.

    Args:
        url: Connection URL (``postgres://``, ``postgresql://``, or driver URL).

    Returns:
        URL using the ``postgresql+psycopg`` dialect.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://") and not url.startswith("postgresql+"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def resolve_database_url(settings: DatabaseSettings | None = None) -> str:
    """Resolve the SQLAlchemy URL used for migrations.

    Args:
        settings: Explicit settings; loads from the environment when omitted.

    Returns:
        ``postgresql+psycopg://`` URL.

    Raises:
        RuntimeError: When database settings are incomplete.
    """
    url = build_sqlalchemy_url(settings)
    if url is None:
        msg = (
            "Database settings incomplete; set DATABASE_HOST, DATABASE_NAME, "
            "DATABASE_USER, and DATABASE_PASSWORD_FILE"
        )
        raise RuntimeError(msg)
    return url


def upgrade_head(settings: DatabaseSettings | None = None, database_url: str | None = None) -> None:
    """Apply all pending migrations up to ``head``.

    Args:
        settings: Explicit DB settings when ``database_url`` is omitted.
        database_url: Optional SQLAlchemy URL override.
    """
    url = database_url if database_url is not None else resolve_database_url(settings)
    command.upgrade(alembic_config(url), "head")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m browser_use_agent.db.migrate``.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success).
    """
    parser = argparse.ArgumentParser(description="Apply browser-use audit schema migrations.")
    parser.add_argument(
        "command",
        nargs="?",
        default="upgrade",
        choices=("upgrade",),
        help="Migration command (only upgrade is supported today).",
    )
    parser.add_argument(
        "--revision",
        default="head",
        help="Target revision for upgrade (default: head).",
    )
    args = parser.parse_args(argv)

    try:
        url = resolve_database_url()
        if args.command == "upgrade":
            command.upgrade(alembic_config(url), args.revision)
    except (RuntimeError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
