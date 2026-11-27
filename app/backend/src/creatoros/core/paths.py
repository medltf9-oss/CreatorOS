"""PathPolicy - the single write gateway (INV-6).

Nothing in this application may write to a path that did not come out of
`resolve_under_root`. The function rejects traversal, absolute escapes,
Windows device paths, reserved names and UNC paths so that a malicious
filename or a compromised model output cannot touch C: or any other location.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .settings import PathKind, get_settings

RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_WINDOWS_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')


class PathPolicyError(ValueError):
    """Raised when a path would escape the configured data root."""


def _validate_segment(segment: str) -> str:
    if segment in ("", ".", ".."):
        raise PathPolicyError(f"illegal path segment: {segment!r}")
    if _WINDOWS_ILLEGAL.search(segment):
        raise PathPolicyError(f"illegal characters in path segment: {segment!r}")
    stem = segment.split(".")[0].strip().lower()
    if stem in RESERVED_NAMES:
        raise PathPolicyError(f"reserved Windows name: {segment!r}")
    if segment.endswith((" ", ".")):
        raise PathPolicyError(f"segment may not end with space or dot: {segment!r}")
    return segment


def resolve_under_root(kind: PathKind, *parts: str) -> Path:
    """Return an absolute path inside the configured root for the given kind."""
    root = get_settings().path(kind).resolve()
    target = root
    for part in parts:
        if not isinstance(part, str):
            raise PathPolicyError("path parts must be strings")
        if Path(part).is_absolute() or part.startswith(("\\", "/")):
            raise PathPolicyError(f"absolute paths are not allowed: {part!r}")
        if part.startswith(("\\\\", "//")):
            raise PathPolicyError(f"UNC paths are not allowed: {part!r}")
        for segment in part.replace("\\", "/").split("/"):
            target = target / _validate_segment(segment)
    resolved = target.resolve()
    if not _is_relative_to(resolved, root):
        raise PathPolicyError(f"path escapes the {kind} root: {resolved}")
    return resolved


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def ensure_dir(kind: PathKind, *parts: str) -> Path:
    """Create (if needed) and return a directory inside the root."""
    path = resolve_under_root(kind, *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_inside_root(path: str | os.PathLike[str]) -> bool:
    return _is_relative_to(Path(path).resolve(), get_settings().root.resolve())
