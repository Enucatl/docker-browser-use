"""Optional Docker Compose start/stop for the Chromium worker."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from browser_use_agent.browser.settings import BrowserSettings

logger = logging.getLogger(__name__)


class ComposeControlError(RuntimeError):
    """Raised when Compose browser start/stop fails."""


def _compose_cmd(settings: BrowserSettings, *args: str) -> list[str]:
    """Build a ``docker compose`` argv for the browser service.

    Args:
        settings: Browser settings with compose service name.
        *args: Compose subcommand tokens (e.g. ``up``, ``-d``).

    Returns:
        Full argv list.
    """
    docker = shutil.which("docker")
    if not docker:
        raise ComposeControlError(
            "docker CLI not found on PATH (needed for BROWSER_COMPOSE_CONTROL)"
        )
    return [docker, "compose", *args, settings.compose_browser_service]


async def compose_up_browser(settings: BrowserSettings) -> None:
    """Start the Compose browser service in the background.

    Args:
        settings: Must have ``compose_control`` semantics; project dir optional.

    Raises:
        ComposeControlError: When the CLI is missing or exits non-zero.
    """
    cmd = _compose_cmd(settings, "up", "-d", "--no-deps")
    cwd = settings.compose_project_dir
    logger.info("Starting browser via compose: %s (cwd=%s)", " ".join(cmd), cwd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ComposeControlError(
            f"docker compose up failed ({proc.returncode}): "
            f"{stderr.decode(errors='replace') or stdout.decode(errors='replace')}"
        )


async def compose_stop_browser(settings: BrowserSettings) -> None:
    """Stop the Compose browser service without removing the profile volume.

    Args:
        settings: Browser settings with compose service name.

    Raises:
        ComposeControlError: When the CLI is missing or exits non-zero.
    """
    cmd = _compose_cmd(settings, "stop")
    cwd = settings.compose_project_dir
    logger.info("Stopping browser via compose: %s (cwd=%s)", " ".join(cmd), cwd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ComposeControlError(
            f"docker compose stop failed ({proc.returncode}): "
            f"{stderr.decode(errors='replace') or stdout.decode(errors='replace')}"
        )


def compose_project_dir_exists(settings: BrowserSettings) -> bool:
    """Return whether ``compose_project_dir`` is set and exists.

    Args:
        settings: Browser settings.

    Returns:
        True when a project directory is configured and present on disk.
    """
    if not settings.compose_project_dir:
        return True
    return Path(settings.compose_project_dir).is_dir()
