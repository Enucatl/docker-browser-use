"""Always-on self-hosted browser-agent package."""

from browser_use_agent.db import DatabaseSettings, build_database_url, load_database_settings

__version__ = "0.1.0"

__all__ = [
    "DatabaseSettings",
    "__version__",
    "build_database_url",
    "load_database_settings",
]
