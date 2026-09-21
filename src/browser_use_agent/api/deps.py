"""FastAPI dependencies for database sessions, settings, and auth."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from browser_use_agent.api.auth import User, require_user

# Dependency alias for routes that need the Authelia-backed principal.
CurrentUser = Annotated[User, Depends(require_user)]


def get_session(request: Request) -> Iterator[Session]:
    """Yield a request-scoped SQLAlchemy session and commit on success.

    Args:
        request: Current request (provides ``session_factory`` on app state).

    Yields:
        An open :class:`~sqlalchemy.orm.Session`.

    Raises:
        RuntimeError: When the app was started without a database engine.
    """
    factory: sessionmaker[Session] | None = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("Database is not configured; set DATABASE_HOST and related vars")

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
