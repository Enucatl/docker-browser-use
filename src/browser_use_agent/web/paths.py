"""Filesystem paths for Web UI templates and static assets."""

from __future__ import annotations

from pathlib import Path

WEB_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_ROOT / "templates"
STATIC_DIR = WEB_ROOT / "static"
