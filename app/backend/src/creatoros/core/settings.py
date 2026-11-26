"""Application settings.

Every path derives from a single configurable root (INV-6 / ADR-01).
The root is resolved in this order:
  1. CREATOROS_ROOT environment variable
  2. config/bootstrap.json  {"root": "<data root>"}
  3. compiled default %USERPROFILE%\\CreatorOS
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PathKind = Literal[
    "database", "media_incoming", "media_working", "media_exports",
    "media_thumbnails", "media_subtitles", "media_archive", "agent_memory",
    "media_originals", "media_previews", "media_proxies", "media_transcodes",
    "media_audio", "media_waveforms", "media_staging", "media_quarantine",
    "backups", "models", "logs", "temp", "cache", "config", "secrets",
    "runtime", "docs",
]

_RELATIVE_KINDS: dict[str, str] = {
    "database": "data/database",
    "media_incoming": "data/media/incoming",
    "media_working": "data/media/working",
    "media_exports": "data/media/exports",
    "media_thumbnails": "data/media/thumbnails",
    "media_subtitles": "data/media/subtitles",
    "media_archive": "data/media/archive",
    "media_originals": "data/media/originals",
    "media_previews": "data/media/previews",
    "media_proxies": "data/media/proxies",
    "media_transcodes": "data/media/transcodes",
    "media_audio": "data/media/audio",
    "media_waveforms": "data/media/waveforms",
    "media_staging": "data/media/staging",
    "media_quarantine": "data/media/quarantine",
    "agent_memory": "data/agent_memory",
    "backups": "data/backups",
    "models": "models/ollama",
    "logs": "logs",
    "temp": "temp",
    "cache": "cache",
    "config": "config",
    "secrets": "config/secrets",
    "runtime": "config/runtime",
    "docs": "docs",
}

def _compiled_default_root() -> str:
    """Default data root for a fresh install: %USERPROFILE%\\CreatorOS (ADR-01).

    No machine-specific path is compiled into the source. An existing install keeps
    its location through CREATOROS_ROOT or config/bootstrap.json; this default only
    applies to a first run on a new machine.
    """
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return str(Path(home) / "CreatorOS")


DEFAULT_ROOT = _compiled_default_root()


def _root_from_bootstrap() -> str | None:
    candidate = Path(DEFAULT_ROOT) / "config" / "bootstrap.json"
    if candidate.is_file():
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        root = data.get("root")
        if isinstance(root, str) and root.strip():
            return root
    return None


@lru_cache(maxsize=1)
def resolve_root() -> Path:
    env = os.environ.get("CREATOROS_ROOT")
    if env:
        return Path(env).resolve()
    bootstrap = _root_from_bootstrap()
    if bootstrap:
        return Path(bootstrap).resolve()
    return Path(DEFAULT_ROOT)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREATOROS_", extra="ignore")

    app_name: str = "CreatorOS Studio"
    version: str = "0.8.0"
    host: str = "127.0.0.1"
    port: int = 0  # 0 = let the OS choose; Electron passes the chosen port back
    session_token: str = Field(default="", description="Random per-launch token minted by the shell.")
    log_level: str = "INFO"
    ollama_url: str = "http://127.0.0.1:11434"
    ffmpeg_dir: str = ""

    # --- Phase 5: media policy -------------------------------------------------
    media_max_import_bytes: int = 4 * 1024**3          # 4 GiB per file
    media_max_duration_s: float = 4 * 3600.0           # refuse absurd durations
    media_min_free_gb: float = 1.0                     # never fill the drive
    media_probe_timeout_s: float = 30.0
    media_transcode_timeout_s: float = 3600.0
    media_thumbnail_timeout_s: float = 60.0
    media_thumbnail_size: int = 480                    # library thumbnail long edge
    media_max_concurrent_ffmpeg: int = 1               # measured policy, not a guess
    media_auto_thumbnail: bool = True
    media_auto_preview: bool = False                   # user opts in per import
    media_keep_originals: bool = True                  # deletion needs explicit confirm

    # --- Phase 6: social accounts, OAuth and the credential vault --------------
    social_callback_port: int = 47821                  # fixed: providers compare redirect URIs exactly
    social_callback_enabled: bool = True               # the loopback listener starts on first connect
    social_oauth_state_ttl_s: int = 600                # 10 minutes, single use
    social_token_expiry_warn_hours: float = 6.0        # "expiring soon" (TikTok tokens live 24h)
    social_refresh_lock_stale_s: int = 300             # abandon a crashed refresh after 5 minutes
    social_max_accounts_per_claim: int = 100           # a Page-heavy user can have many
    social_diagnostics_timeout_s: float = 20.0

    @property
    def root(self) -> Path:
        return resolve_root()

    def path(self, kind: PathKind) -> Path:
        return self.root / _RELATIVE_KINDS[kind]

    @property
    def ffmpeg_bin(self) -> Path:
        if self.ffmpeg_dir:
            return Path(self.ffmpeg_dir)
        return self.root / "tools" / "ffmpeg" / "bin"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
