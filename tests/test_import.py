"""Smoke tests for package importability."""

from browser_use_agent import __version__


def test_package_version_is_defined() -> None:
    """Package exposes a non-empty public version string."""
    assert isinstance(__version__, str)
    assert __version__
