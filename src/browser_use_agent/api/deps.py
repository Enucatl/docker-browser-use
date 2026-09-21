"""FastAPI dependencies for database sessions, settings, and auth."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from browser_use_agent.api.auth import User, require_user

# Dependency alias for routes that need the Authelia-backed principal.
CurrentUser = Annotated[User, Depends(require_user)]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped SQLAlchemy session and commit on success.

    Args:
        request: Current request (provides ``session_factory`` on app state).

    Yields:
        An open :class:`~sqlalchemy.orm.Session`.

    Raises:
        RuntimeError: When the app was started without a database engine.
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "async_session_factory", None
    )
    if factory is None:
        raise RuntimeError("Database is not configured; set DATABASE_HOST and related vars")

    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
