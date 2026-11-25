"""Read-only detection for the first-run wizard.

Every probe is bounded (a short HTTP timeout, a short FFmpeg subprocess) because the wizard has to
open quickly on the worst machine: no Ollama, no GPU, no network. Nothing here installs, downloads or
mutates state; a probe that cannot answer returns `None` plus the reason instead of a guess.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil

from ...core.settings import Settings

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
PROBE_TIMEOUT_S = 1.5
FFMPEG_TIMEOUT_S = 15


@dataclass(frozen=True)
class ModelFact:
    """One model the local runtime reports. Sizes are whatever Ollama reported, not an estimate."""

    name: str
    size_bytes: int | None
    modified_at: str | None


@dataclass(frozen=True)
class OllamaFacts:
    """What is actually on this machine, which is not the same as "what the user needs"."""

    installed: bool
    install_path: str | None
    version: str | None
    endpoint: str
    reachable: bool
    models: tuple[ModelFact, ...]
    error: str | None


@dataclass(frozen=True)
class FfmpegFacts:
    """Presence and self-reported version of the media toolchain."""

    present: bool
    ffprobe_present: bool
    path: str | None
    version: str | None
    error: str | None


@dataclass(frozen=True)
class DiskFacts:
    """Space and writability of the data root the application will actually use."""

    root: str
    exists: bool
    writable: bool
    free_bytes: int
    total_bytes: int
    error: str | None


def probe_ollama(endpoint: str = DEFAULT_ENDPOINT, timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """Ask the local runtime two cheap questions. Never raises, never retries."""
    try:
        with httpx.Client(timeout=timeout) as client:
            version_payload = client.get(f"{endpoint}/api/version").json()
            tags_payload = client.get(f"{endpoint}/api/tags").json()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return {"reachable": False, "version": None, "models": [], "error": f"{type(exc).__name__}: {exc}"}

    models: list[dict[str, Any]] = []
    raw_models = tags_payload.get("models", []) if isinstance(tags_payload, dict) else []
    for raw in raw_models if isinstance(raw_models, list) else []:
        if not isinstance(raw, dict):
            continue
        size = raw.get("size")
        models.append(
            {
                "name": str(raw.get("name") or raw.get("model") or ""),
                "size_bytes": int(size) if isinstance(size, int) else None,
                "modified_at": raw.get("modified_at"),
            }
        )
    version = version_payload.get("version") if isinstance(version_payload, dict) else None
    return {"reachable": True, "version": str(version) if version else None, "models": models, "error": None}


def _find_ollama_install() -> str | None:
    found = shutil.which("ollama")
    if found:
        return str(Path(found))
    candidates: list[Path] = []
    for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Programs" / "Ollama" / "ollama.exe")
            candidates.append(Path(base) / "Ollama" / "ollama.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def detect_ollama(
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    probe: Callable[[str], dict[str, Any]] | None = None,
) -> OllamaFacts:
    """Combine a live probe with an on-disk install check.

    The two answers are independent: a stopped service is still an installed runtime, and a reachable
    endpoint on a remote address is not a local install. Both are reported rather than merged.
    """
    prober = probe or probe_ollama
    result = prober(endpoint)
    install_path = _find_ollama_install()
    reachable = bool(result.get("reachable"))
    models = tuple(
        ModelFact(
            name=str(item.get("name") or ""),
            size_bytes=item.get("size_bytes") if isinstance(item.get("size_bytes"), int) else None,
            modified_at=item.get("modified_at") if isinstance(item.get("modified_at"), str) else None,
        )
        for item in (result.get("models") or [])
        if item.get("name")
    )
    return OllamaFacts(
        installed=bool(install_path) or reachable,
        install_path=install_path,
        version=str(result["version"]) if result.get("version") else None,
        endpoint=endpoint,
        reachable=reachable,
        models=models,
        error=result.get("error"),
    )


def detect_ffmpeg(settings: Settings) -> FfmpegFacts:
    """Report the bundled tool path the backend will really spawn, and what it says about itself."""
    bin_dir = settings.ffmpeg_bin
    ffmpeg = bin_dir / "ffmpeg.exe"
    ffprobe = bin_dir / "ffprobe.exe"
    if not ffmpeg.is_file():
        return FfmpegFacts(False, ffprobe.is_file(), None, None, f"no ffmpeg.exe under {bin_dir}")
    try:
        completed = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return FfmpegFacts(True, ffprobe.is_file(), str(ffmpeg), None, f"{type(exc).__name__}: {exc}")
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    version = output[0].strip() if output else None
    error = None if completed.returncode == 0 else f"ffmpeg -version exited {completed.returncode}"
    return FfmpegFacts(True, ffprobe.is_file(), str(ffmpeg), version, error)


def disk_facts(root: Path) -> DiskFacts:
    """Prove the data root is writable instead of assuming a path exists and is usable."""
    exists = root.is_dir()
    usage_error: str | None = None
    free_bytes = 0
    total_bytes = 0
    try:
        usage = shutil.disk_usage(root if exists else root.parent)
        free_bytes, total_bytes = usage.free, usage.total
    except OSError as exc:
        usage_error = f"{type(exc).__name__}: {exc}"

    writable = False
    if exists:
        probe = root / f".setup-write-{uuid.uuid4().hex[:8]}.tmp"
        try:
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
            writable = True
        except OSError as exc:
            usage_error = usage_error or f"{type(exc).__name__}: {exc}"
    return DiskFacts(str(root), exists, writable, free_bytes, total_bytes, usage_error)


def system_facts() -> dict[str, Any]:
    """Static facts about the host. A value that cannot be read is `None`."""
    memory_total: int | None = None
    memory_available: int | None = None
    try:
        memory = psutil.virtual_memory()
        memory_total, memory_available = int(memory.total), int(memory.available)
    except (psutil.Error, OSError):
        pass
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "architecture": platform.architecture()[0],
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": memory_total,
        "memory_available_bytes": memory_available,
    }
