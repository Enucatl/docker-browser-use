"""Run the agent controller FastAPI app with uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from browser_use_agent.api.app import create_app
from browser_use_agent.config import load_app_settings
from browser_use_agent.db.migrate import upgrade_head

logger = logging.getLogger(__name__)


def main() -> None:
    """Load settings, apply pending DB migrations, then serve the API."""
    settings = load_app_settings()
    # Cold compose bring-up: schema must exist before the first run create.
    # Idempotent (Alembic upgrade head). Tests use create_app() directly and
    # migrate via fixtures instead of this entrypoint.
    if settings.database is not None:
        logger.info("Applying database migrations to head")
        upgrade_head(settings.database)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
