"""Artifact store path settings from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARTIFACTS_ROOT = Path("/var/lib/browser-use/artifacts")


@dataclass(frozen=True, slots=True)
class ArtifactStoreSettings:
    """Filesystem root for content-addressed artifact blobs.

    Attributes:
        root: Directory that holds ``ab/cd/<sha256>`` objects.
    """

    root: Path


def load_artifact_store_settings() -> ArtifactStoreSettings:
    """Load artifact store settings from ``ARTIFACTS_ROOT``.

    Returns:
        Settings using ``ARTIFACTS_ROOT`` when set, otherwise the compose default
        mount path ``/var/lib/browser-use/artifacts``.
    """
    raw = os.environ.get("ARTIFACTS_ROOT")
    root = Path(raw) if raw else DEFAULT_ARTIFACTS_ROOT
    return ArtifactStoreSettings(root=root)
