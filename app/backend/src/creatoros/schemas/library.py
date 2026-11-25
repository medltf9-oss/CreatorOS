"""Request/response contracts for the content library."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..db.enums import DerivativeKind, DerivativeState, IntakeState, MediaKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ImportPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=3, max_length=1024)
    original_filename: str | None = Field(default=None, max_length=512)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[ImportPath] = Field(min_length=1, max_length=200)
    auto_thumbnail: bool | None = None
    auto_preview: bool = False
    priority: int = Field(default=40, ge=1, le=100)


class ImportDuplicate(BaseModel):
    path: str
    existing_asset_id: str
    original_filename: str
    sha256: str
    reason: str


class ImportAccepted(BaseModel):
    jobs: list[dict[str, Any]]
    duplicates: list[ImportDuplicate]
    rejected: list[dict[str, str]]


class DerivativeOut(ORMModel):
    id: str
    asset_id: str
    kind: DerivativeKind
    profile: str
    version: int
    state: DerivativeState
    rel_path: str | None
    size_bytes: int
    width: int | None
    height: int | None
    duration_ms: int | None
    mime_type: str | None
    job_id: str | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime
    urls: dict[str, str] = Field(default_factory=dict)


class AssetSummary(ORMModel):
    id: str
    original_filename: str
    sha256: str
    size_bytes: int
    kind: MediaKind
    intake_state: IntakeState
    mime_type: str | None
    format_name: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    rights_status: str
    archive_state: str
    tags_json: list[str]
    created_at: datetime
    updated_at: datetime
    thumbnail_url: str | None = None
    poster_url: str | None = None
    stream_url: str | None = None


class AssetDetail(AssetSummary):
    source_path: str
    rotation: int
    bitrate: int | None
    probe_version: str | None
    quarantine_reason: str | None
    last_processed_at: datetime | None
    probe: dict[str, Any] = Field(default_factory=dict)
    derivatives: list[DerivativeOut] = Field(default_factory=list)
    project_count: int = 0
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    storage: dict[str, Any] = Field(default_factory=dict)


class AssetPage(BaseModel):
    items: list[AssetSummary]
    total: int
    limit: int
    offset: int


class LibraryStats(BaseModel):
    assets_total: int
    by_kind: dict[str, int]
    by_state: dict[str, int]
    bytes_total: int
    bytes_by_kind: dict[str, int]
    derivatives_total: int
    derivatives_by_kind: dict[str, int]
    jobs_active: int
    jobs_failed: int
    jobs_completed: int
    storage: dict[str, dict[str, int]]
    disk: dict[str, float]
    recent: list[AssetSummary]
    capabilities: dict[str, Any]


class ThumbnailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: int | None = Field(default=None, ge=96, le=1280)
    timeline: int = Field(default=0, ge=0, le=40)
    force: bool = False


class TranscodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["fast_preview", "editing_proxy", "normalized"]
    height: int | None = Field(default=None, ge=144, le=2160)
    crf: int | None = Field(default=None, ge=14, le=34)
    fps: int | None = Field(default=None, ge=12, le=120)


class ProxyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    height: int = Field(default=480, ge=240, le=1080)
    crf: int | None = Field(default=None, ge=18, le=34)


class AudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["wav", "aac"] = "wav"


class WaveformRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buckets: int = Field(default=800, ge=50, le=4000)
    image: bool = False


class MetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rights_status: str | None = None
    rights_notes: str | None = Field(default=None, max_length=4000)
    tags_json: list[str] | None = Field(default=None, max_length=50)
    archive_state: str | None = None


class JobOut(BaseModel):
    job_id: str
    job_type: str
    state: str
    attempts: int
    max_attempts: int
    progress: float | None
    stage: str | None
    asset_id: str | None
    last_error: str | None
    cancel_requested: bool
    created_at: str | None
    updated_at: str | None
    finished_at: str | None
    result: dict[str, Any]


class MediaJobPage(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


class StorageReport(BaseModel):
    areas: dict[str, dict[str, int]]
    disk: dict[str, float]
    spacing_note: str


class RecoveryReport(BaseModel):
    staging_removed: int
    bytes_freed: int
    assets_quarantined: int
    derivatives_missing: int
    older_than_minutes: float


__all__ = [
    "AssetDetail",
    "AssetPage",
    "AssetSummary",
    "AudioRequest",
    "DerivativeOut",
    "ImportAccepted",
    "ImportDuplicate",
    "ImportPath",
    "ImportRequest",
    "JobOut",
    "LibraryStats",
    "MediaJobPage",
    "MetadataUpdate",
    "ProxyRequest",
    "RecoveryReport",
    "StorageReport",
    "ThumbnailRequest",
    "TranscodeRequest",
    "WaveformRequest",
]
