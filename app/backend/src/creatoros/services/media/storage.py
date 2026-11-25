"""Deterministic media storage.

Every media file in CreatorOS lives under ``data/media`` and its path is derived from
the content hash, never from a user-supplied filename. That single rule removes a whole
class of problems at once:

* two files called ``final.mp4`` cannot collide,
* re-importing the same bytes resolves to the same path (idempotency),
* a path can never escape the media root by carrying ``..`` or a drive letter,
* an original can never be silently replaced by a derivative, because derivatives live
  in different trees and are keyed by ``(sha, kind, profile, version)``.

Writes go to ``staging`` first and are moved into place with :func:`os.replace`, which is
atomic on the same volume. A crash mid-copy therefore leaves a ``.part`` file in staging,
which recovery deletes — never a truncated "original".
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ...core.logging import get_logger
from ...core.paths import PathPolicyError, ensure_dir, is_inside_root, resolve_under_root
from ...core.settings import PathKind, get_settings

log = get_logger(__name__)

Area = Literal[
    "originals",
    "thumbnails",
    "previews",
    "proxies",
    "transcodes",
    "audio",
    "waveforms",
    "staging",
    "quarantine",
]

AREA_KIND: dict[str, PathKind] = {
    "originals": "media_originals",
    "thumbnails": "media_thumbnails",
    "previews": "media_previews",
    "proxies": "media_proxies",
    "transcodes": "media_transcodes",
    "audio": "media_audio",
    "waveforms": "media_waveforms",
    "staging": "media_staging",
    "quarantine": "media_quarantine",
}

COPY_CHUNK = 1024 * 1024  # 1 MiB: bounded memory for any file size
PARTIAL_SUFFIX = ".part"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")


class MediaStorageError(ValueError):
    code = "MEDIA_STORAGE_ERROR"


class InsufficientStorage(MediaStorageError):
    code = "INSUFFICIENT_STORAGE"


class FileTooLarge(MediaStorageError):
    code = "FILE_TOO_LARGE"


class UnsafePath(MediaStorageError):
    code = "UNSAFE_PATH"


class StagingWriteError(MediaStorageError):
    code = "STAGING_WRITE_FAILED"


@dataclass(slots=True)
class CopyResult:
    sha256: str
    size_bytes: int
    staging_path: Path
    duration_s: float


@dataclass(slots=True)
class StagedFile:
    """A file that finished copying and is waiting to be promoted or discarded."""

    sha256: str
    size_bytes: int
    path: Path
    source_name: str
    copy_seconds: float

    def discard(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            log.warning("could not remove staged file %s", self.path)


def normalise_extension(name: str, *, fallback: str = ".bin") -> str:
    """Extension as a storage suffix: lower-case, alphanumeric, at most 8 chars."""
    suffix = Path(name).suffix.lower().lstrip(".")
    if not suffix or not _SAFE_EXT_RE.match(suffix):
        return fallback
    return f".{suffix}"


def sanitise_display_name(name: str) -> str:
    """A filename safe to display and store as metadata (path is never derived from it)."""
    base = os.path.basename(name.replace("\\", "/")).strip()
    base = base.replace("\x00", "")
    base = re.sub(r"[\r\n\t]", " ", base)
    if not base:
        base = "unnamed"
    return base[:500]


def available_bytes(path: Path | None = None) -> int:
    target = path or get_settings().root
    try:
        return shutil.disk_usage(str(target)).free
    except OSError:
        return 0


class MediaStorage:
    """The only module allowed to compute or create media paths."""

    def __init__(self, *, max_import_bytes: int | None = None, min_free_gb: float | None = None) -> None:
        settings = get_settings()
        self.max_import_bytes = max_import_bytes or settings.media_max_import_bytes
        self.min_free_bytes = int((min_free_gb if min_free_gb is not None else settings.media_min_free_gb) * 1024**3)

    # ---------------------------------------------------------------- layout
    @staticmethod
    def area_dir(area: Area) -> Path:
        return ensure_dir(AREA_KIND[area])

    def _shard(self, area: Area, sha256: str) -> Path:
        if not _SHA_RE.match(sha256 or ""):
            raise UnsafePath("a media path must be derived from a valid sha256")
        return ensure_dir(AREA_KIND[area], sha256[:2])

    def original_path(self, sha256: str, extension: str) -> Path:
        ext = extension if extension.startswith(".") else f".{extension}"
        if not _SAFE_EXT_RE.match(ext.lstrip(".")):
            raise UnsafePath(f"refusing unsafe extension {extension!r}")
        return self._shard("originals", sha256) / f"{sha256}{ext.lower()}"

    def derivative_path(self, *, area: Area, sha256: str, kind: str, profile: str, version: int, ext: str) -> Path:
        """Derived files are keyed by content hash + kind + profile + version.

        That tuple is also the cache key: regenerating with the same inputs returns the
        same path, so a second request is a verified cache hit rather than a new file.
        """
        if area in ("originals", "staging", "quarantine"):
            raise UnsafePath(f"derivatives may not live in {area}")
        safe_kind = re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-") or "derivative"
        safe_profile = re.sub(r"[^a-z0-9]+", "-", profile.lower()).strip("-") or "default"
        suffix = ext if ext.startswith(".") else f".{ext}"
        if not _SAFE_EXT_RE.match(suffix.lstrip(".")):
            raise UnsafePath(f"refusing unsafe extension {ext!r}")
        return self._shard(area, sha256) / f"{sha256}-{safe_kind}-{safe_profile}-v{version}{suffix.lower()}"

    def staging_path(self, hint: str, *, extension: str = ".part") -> Path:
        """A unique, non-guessable staging file.

        ``hint`` is only used for readability in logs; the random component makes the
        name unpredictable and collision-free.
        """
        safe_hint = re.sub(r"[^A-Za-z0-9_-]+", "-", hint)[:40] or "stage"
        token = os.urandom(8).hex()
        if extension == ".part":
            suffix = PARTIAL_SUFFIX
        else:
            bare = extension.lstrip(".").lower()
            if not _SAFE_EXT_RE.match(bare):
                raise UnsafePath(f"refusing unsafe staging extension {extension!r}")
            suffix = f".{bare}"
        self.area_dir("staging")
        return resolve_under_root(AREA_KIND["staging"], f"{safe_hint}-{token}{suffix}")

    def quarantine_path(self, sha256: str, extension: str) -> Path:
        return self._shard("quarantine", sha256) / f"{sha256}{normalise_extension('x' + extension)}"

    # --------------------------------------------------------------- policy
    def check_source(self, path: Path, *, expected_size: int | None = None) -> int:
        """Validate a candidate import source. Returns its size in bytes."""
        if not path.exists():
            raise MediaStorageError(f"source file does not exist: {path.name}")
        if path.is_symlink():
            # A symlink can point anywhere; importing through one would let a crafted
            # library entry read an arbitrary file.
            raise UnsafePath("symbolic links are not accepted as import sources")
        if not path.is_file():
            raise UnsafePath("only regular files can be imported")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise MediaStorageError(f"cannot stat source file: {exc}") from exc
        if size <= 0:
            raise MediaStorageError("source file is empty")
        if size > self.max_import_bytes:
            raise FileTooLarge(
                f"file is {size} bytes, above the {self.max_import_bytes} byte import limit"
            )
        if expected_size is not None and size != expected_size:
            raise MediaStorageError("source file changed size while it was being selected")
        self.require_space(size)
        return size

    def require_space(self, size: int, *, multiplier: float = 1.25) -> None:
        free = available_bytes()
        needed = int(size * multiplier) + self.min_free_bytes
        if free < needed:
            raise InsufficientStorage(
                f"needs about {needed / 1024**3:.1f} GiB free, only {free / 1024**3:.1f} GiB available"
            )

    # ----------------------------------------------------------------- copy
    def copy_to_staging(
        self,
        source: Path,
        *,
        hint: str = "import",
        progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> StagedFile:
        """Stream ``source`` into staging while hashing it. Bounded memory, atomic end state.

        The hash is computed over the bytes actually written, so a duplicate check can
        trust it even if the source is being modified concurrently (the size check above
        catches the common case; the hash is what gets stored).
        """
        size = self.check_source(source)
        target = self.staging_path(hint, extension=normalise_extension(source.name))
        digest = hashlib.sha256()
        written = 0
        started = time.perf_counter()
        try:
            with source.open("rb") as reader, target.open("wb") as writer:
                while True:
                    if should_cancel is not None and should_cancel():
                        raise StagingWriteError("import cancelled")
                    chunk = reader.read(COPY_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    writer.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(written, size)
                writer.flush()
                os.fsync(writer.fileno())
        except StagingWriteError:
            target.unlink(missing_ok=True)
            raise
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise StagingWriteError(f"copy failed: {exc}") from exc

        if written != size:
            target.unlink(missing_ok=True)
            raise StagingWriteError(f"copied {written} of {size} bytes; source changed during copy")

        return StagedFile(
            sha256=digest.hexdigest(),
            size_bytes=written,
            path=target,
            source_name=sanitise_display_name(source.name),
            copy_seconds=time.perf_counter() - started,
        )

    def promote(self, staged: StagedFile, *, extension: str) -> Path:
        """Atomically move a staged file into ``originals``."""
        destination = self.original_path(staged.sha256, extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            # Same bytes already exist: keep the canonical copy, drop the duplicate copy.
            staged.discard()
            return destination
        os.replace(staged.path, destination)
        return destination

    def move_to_quarantine(self, path: Path, *, sha256: str, extension: str) -> Path:
        if not is_inside_root(path):
            raise UnsafePath("refusing to quarantine a file outside the data root")
        destination = self.quarantine_path(sha256, extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)
        return destination

    # --------------------------------------------------------------- lookup
    def resolve_stored(self, rel_or_abs: str | Path) -> Path:
        """Resolve a stored path and prove it is inside the media root.

        Rows are written by this module, but a tampered row must not become a file-read
        primitive, so every read goes through this check.
        """
        candidate = Path(rel_or_abs)
        media_root = get_settings().path("media_originals").parent.resolve()
        if not candidate.is_absolute():
            candidate = (media_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if not is_inside_root(candidate) or not str(candidate).lower().startswith(str(media_root).lower()):
            raise UnsafePath("stored media path is outside the media root")
        return candidate

    def relative(self, path: Path) -> str:
        """Store paths relative to the media root so moving the root does not break rows."""
        media_root = get_settings().path("media_originals").parent.resolve()
        try:
            return str(path.resolve().relative_to(media_root)).replace("\\", "/")
        except ValueError as exc:
            raise UnsafePath("path is outside the media root") from exc

    # --------------------------------------------------------------- stats
    def usage(self) -> dict[str, dict[str, int]]:
        stats: dict[str, dict[str, int]] = {}
        for area, kind in AREA_KIND.items():
            directory = get_settings().path(kind)
            files = 0
            total = 0
            if directory.exists():
                for file in directory.rglob("*"):
                    if file.is_file():
                        files += 1
                        try:
                            total += file.stat().st_size
                        except OSError:  # pragma: no cover - racing deletion
                            continue
            stats[area] = {"files": files, "bytes": total}
        return stats

    def staging_leftovers(self, *, older_than_seconds: float = 0.0) -> list[Path]:
        directory = get_settings().path("media_staging")
        if not directory.exists():
            return []
        cutoff = time.time() - older_than_seconds
        leftovers = []
        for file in directory.rglob("*"):
            if not file.is_file():
                continue
            try:
                if file.stat().st_mtime <= cutoff:
                    leftovers.append(file)
            except OSError:  # pragma: no cover
                continue
        return leftovers

    def sweep_staging(self, *, older_than_seconds: float = 900.0) -> dict[str, int]:
        removed = 0
        freed = 0
        for file in self.staging_leftovers(older_than_seconds=older_than_seconds):
            try:
                size = file.stat().st_size
                file.unlink()
                removed += 1
                freed += size
            except OSError:  # pragma: no cover - locked file
                continue
        return {"removed": removed, "bytes_freed": freed}

    def sha256_of(self, path: Path, *, should_cancel: Callable[[], bool] | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                if should_cancel is not None and should_cancel():
                    raise StagingWriteError("hash cancelled")
                chunk = handle.read(COPY_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()


def read_prefix(path: Path, size: int = 64) -> bytes:
    """First bytes of a file, used for magic-number sniffing (never for trust)."""
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


__all__ = [
    "AREA_KIND",
    "Area",
    "CopyResult",
    "FileTooLarge",
    "InsufficientStorage",
    "MediaStorage",
    "MediaStorageError",
    "PathPolicyError",
    "StagedFile",
    "StagingWriteError",
    "UnsafePath",
    "available_bytes",
    "normalise_extension",
    "read_prefix",
    "sanitise_display_name",
]
