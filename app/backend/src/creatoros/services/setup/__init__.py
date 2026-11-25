"""First-run setup: detection, pinned prerequisites and verification.

Nothing in this package downloads, installs or trusts a component on its own. Detection reads
local state, verification compares bytes and signatures against the pinned manifest, and the API
surfaces both so the renderer can ask the user before anything happens.
"""
from __future__ import annotations

from .manifest import (
    Component,
    Manifest,
    ManifestError,
    clear_manifest_cache,
    load_manifest,
    parse_manifest,
)

__all__ = [
    "Component",
    "Manifest",
    "ManifestError",
    "clear_manifest_cache",
    "load_manifest",
    "parse_manifest",
]
