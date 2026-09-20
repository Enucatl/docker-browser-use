"""Run the agent controller FastAPI app with uvicorn."""

from __future__ import annotations

import uvicorn

from browser_use_agent.api.app import create_app
from browser_use_agent.config import load_app_settings


def main() -> None:
    """Load settings and serve the controller API until interrupted."""
    settings = load_app_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
