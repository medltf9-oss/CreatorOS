"""Pydantic v2 request/response schemas.

All identifiers are strings (ULID); datetimes are timezone-aware UTC on the way
out. Responses never include saleable secrets because the models never hold any.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..db.enums import (
    AccountType,
    ApprovalStatus,
    ArchiveState,
    ConnectionStatus,
    Platform,
    ProjectStatus,
    PublishState,
    QueueState,
    RightsStatus,
    Severity,
    TaskState,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None
    details: dict[str, Any] | None = None


# --------------------------------------------------------------------- settings
class SettingValue(BaseModel):
    key: str
    value_json: dict[str, Any] = Field(default_factory=dict)
    scope: str = "app"


class PathsInfo(BaseModel):
    root: str
    paths: dict[str, str]
    free_gb: float
    total_gb: float


# ------------------------------------------------------------------------ media
class MediaAssetCreate(BaseModel):
    source_path: str = Field(min_length=1, max_length=1024)
    original_filename: str = Field(min_length=1, max_length=512)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    has_audio: bool = False
    rights_status: RightsStatus = RightsStatus.UNCERTAIN
    rights_notes: str | None = None
    tags_json: list[str] = Field(default_factory=list)


class MediaAssetUpdate(BaseModel):
    rights_status: RightsStatus | None = None
    rights_notes: str | None = None
    tags_json: list[str] | None = None
    archive_state: ArchiveState | None = None
    thumbnail_path: str | None = None


class MediaAssetOut(ORMModel):
    id: str
    source_path: str
    original_filename: str
    sha256: str
    size_bytes: int
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    has_audio: bool
    rights_status: RightsStatus
    rights_notes: str | None
    tags_json: list[str]
    archive_state: ArchiveState
    created_at: datetime


# ---------------------------------------------------------------------- projects
class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    brief: str | None = None
    source_asset_id: str | None = None
    campaign_id: str | None = None
    brand_profile_id: str | None = None


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    brief: str | None = None
    status: ProjectStatus | None = None


class ProjectOut(ORMModel):
    id: str
    title: str
    brief: str | None
    status: ProjectStatus
    source_asset_id: str | None
    campaign_id: str | None
    brand_profile_id: str | None
    created_at: datetime
    updated_at: datetime


class VariantCreate(BaseModel):
    platform: Platform
    target_account_id: str | None = None
    caption: str = ""
    hashtags_json: list[str] = Field(default_factory=list)
    aspect_ratio: str = "9:16"


class VariantUpdate(BaseModel):
    caption: str | None = None
    hashtags_json: list[str] | None = None
    cta: str | None = None
    privacy_setting: str | None = None
    cover_path: str | None = None
    output_path: str | None = None
    approval_status: ApprovalStatus | None = None


class VariantOut(ORMModel):
    id: str
    project_id: str
    platform: Platform
    target_account_id: str | None
    caption: str
    hashtags_json: list[str]
    aspect_ratio: str
    privacy_setting: str
    approval_status: ApprovalStatus
    validation_report_json: dict[str, Any]
    created_at: datetime


# ---------------------------------------------------------------------- accounts
class AccountCreate(BaseModel):
    platform: Platform
    provider_account_id: str = Field(min_length=1, max_length=190)
    display_name: str = Field(min_length=1, max_length=200)
    username: str | None = None
    account_type: AccountType = AccountType.PERSONAL_UNSUPPORTED
    scopes_json: list[str] = Field(default_factory=list)
    connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED


class AccountOut(ORMModel):
    id: str
    platform: Platform
    provider_account_id: str
    display_name: str
    username: str | None
    account_type: AccountType
    scopes_json: list[str]
    capabilities_json: dict[str, Any]
    connection_status: ConnectionStatus
    token_expires_at: datetime | None
    last_verified_at: datetime | None
    created_at: datetime


# ------------------------------------------------------------------------ agents
class AgentDefinitionOut(ORMModel):
    agent_id: str
    name: str
    team_id: str
    mission: str
    model_profile: str
    risk_level: str
    approval_requirement: str
    allowed_tools_json: list[str]
    timeout_s: int
    memory_scope: str
    enabled: bool


class AgentTaskCreate(BaseModel):
    agent_id: str = Field(min_length=3, max_length=8)
    title: str | None = Field(default=None, max_length=300)
    input_json: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)


class AgentTaskOut(ORMModel):
    id: str
    agent_id: str
    team_id: str
    title: str | None
    state: TaskState
    priority: int
    retries: int
    input_json: dict[str, Any]
    output_json: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# ------------------------------------------------------------------------- queue
class QueueJobOut(ORMModel):
    id: str
    queue: str
    job_type: str
    state: QueueState
    priority: int
    attempts: int
    max_attempts: int
    run_after: datetime
    last_error: str | None
    finished_at: datetime | None
    created_at: datetime


class QueueEnqueueRequest(BaseModel):
    job_type: str = Field(min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    queue: str = "default"
    priority: int = Field(default=50, ge=1, le=100)
    delay_seconds: float = Field(default=0, ge=0, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    idempotency_key: str | None = Field(default=None, max_length=64)


# ------------------------------------------------------------------------- audit
class AuditOut(ORMModel):
    id: str
    actor: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    metadata_redacted_json: dict[str, Any]
    correlation_id: str | None
    created_at: datetime


class NotificationOut(ORMModel):
    id: str
    severity: Severity
    title: str
    body: str | None
    route: str | None
    read_at: datetime | None
    created_at: datetime


class PublishJobOut(ORMModel):
    id: str
    variant_id: str
    platform: Platform
    account_id: str
    job_state: PublishState
    scheduled_at_utc: datetime | None
    attempts: int
    idempotency_key: str
    remote_post_id: str | None
    last_error_code: str | None
    last_error_detail: str | None
    created_at: datetime
