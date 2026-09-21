"""Always-on self-hosted browser-agent package."""

from importlib.metadata import version

from browser_use_agent.db import DatabaseSettings, build_database_url, load_database_settings

__version__ = version("browser-use-agent")

__all__ = [
    "DatabaseSettings",
    "__version__",
    "build_database_url",
    "load_database_settings",
]
