"""Minimal Agent Web UI (server-rendered templates + light JS)."""

__all__ = [
    "mount_web_ui",
]


def __getattr__(name: str) -> object:
    """Lazy-export ``mount_web_ui`` to avoid circular imports with the API package.

    Args:
        name: Attribute name.

    Returns:
        The requested attribute.

    Raises:
        AttributeError: When ``name`` is unknown.
    """
    if name == "mount_web_ui":
        from browser_use_agent.web.routes import mount_web_ui

        return mount_web_ui
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
