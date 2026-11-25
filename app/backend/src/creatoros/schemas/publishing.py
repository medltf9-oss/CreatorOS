"""Publishing request and response schemas.

Two properties are enforced by shape rather than by convention:

* no request accepts a secret, a file path or a provider URL. The delivery bucket's credentials are
  a *vault secret id* the Electron shell wrote, never a value;
* no response model has a field for a token, an upload URL or a signed URL, so a serialiser mistake
  cannot leak one. The deep ledgers are typed as `dict` because they come from
  `services/publishing/history.py`, which already strips credential-shaped keys.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..db.enums import Platform, PublishMode, ReconciliationDecision


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ composer input


class CampaignCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    objective: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class CampaignUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    objective: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class IntentCreate(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    campaign_id: str | None = None
    source_asset_id: str | None = None
    notes: str | None = Field(default=None, max_length=2000)


class IntentUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
    campaign_id: str | None = None
    source_asset_id: str | None = None


class IntentDuplicate(StrictModel):
    title: str | None = Field(default=None, max_length=500)


class DestinationCreate(StrictModel):
    platform: Platform
    account_id: str
    mode: PublishMode | None = None
    title: str | None = Field(default=None, max_length=500)
    caption: str | None = Field(default=None, max_length=5000)
    hashtags: list[str] | None = None
    options: dict[str, Any] | None = None
    privacy_setting: str | None = Field(default=None, max_length=64)
    scheduled_at_utc: datetime | None = None
    display_timezone: str | None = Field(default=None, max_length=64)
    asset_id: str | None = None
    #: Which agent (if any) wrote each field. Provenance, not permission.
    field_sources: dict[str, str] | None = None


class DestinationUpdate(StrictModel):
    fields: dict[str, Any]
    field_sources: dict[str, str] | None = None

    @field_validator("fields")
    @classmethod
    def _no_paths(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {"media_path", "source_path", "output_path", "cover_path", "url", "upload_url"}
        offending = sorted(set(value) & forbidden)
        if offending:
            raise ValueError(f"{', '.join(offending)} cannot be set through the API")
        return value


class RevalidateRequest(StrictModel):
    creator_info: dict[str, Any] | None = None


class ApprovalRequestCreate(StrictModel):
    summary: str | None = Field(default=None, max_length=500)
    risks: list[str] | None = None
    ttl_hours: int | None = Field(default=72, ge=1, le=720)


class ApprovalDecision(StrictModel):
    approve: bool
    note: str | None = Field(default=None, max_length=500)


class ScheduleRequest(StrictModel):
    scheduled_at_utc: datetime | None = None
    display_timezone: str | None = Field(default=None, max_length=64)


class LatePolicyUpdate(StrictModel):
    policy: str
    grace_seconds: int | None = Field(default=None, ge=0, le=86400)


class CancelRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=300)


class ReconcileResolveRequest(StrictModel):
    decision: ReconciliationDecision
    note: str = Field(min_length=1, max_length=500)
    permalink: str | None = Field(default=None, max_length=1024)


class DeliveryConfigUpdate(StrictModel):
    provider: str = Field(min_length=1, max_length=40)
    bucket: str = Field(min_length=3, max_length=190)
    region: str = Field(min_length=1, max_length=64)
    public_base_url: str | None = Field(default=None, max_length=500)
    endpoint_url: str | None = Field(default=None, max_length=500)
    prefix: str = Field(default="creatoros", max_length=120)
    #: A vault secret id holding the access key/secret pair. Never a value.
    credentials_secret_id: str | None = Field(default=None, max_length=26)
    ttl_seconds: int = Field(default=3600, ge=300, le=86400)


class ValidateRequest(StrictModel):
    platform: Platform
    mode: PublishMode | None = None
    title: str | None = Field(default=None, max_length=500)
    caption: str | None = Field(default=None, max_length=5000)
    hashtags: list[str] | None = None
    options: dict[str, Any] | None = None
    asset_id: str | None = None
    creator_info: dict[str, Any] | None = None


# ----------------------------------------------------------------- --- responses


class ActionResponse(StrictModel):
    action: str
    state: str
    queued: bool = False
    reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class JobListResponse(StrictModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class JobDetailResponse(StrictModel):
    job: dict[str, Any]


class ActionAffordances(StrictModel):
    state: str
    publish_now: dict[str, Any]
    retry: dict[str, Any]
    cancel: dict[str, Any]
    reconcile: dict[str, Any]
    history: dict[str, Any]


class OverviewResponse(StrictModel):
    summary: dict[str, Any]
    delivery: dict[str, Any]
    schedule: dict[str, Any]
    composer: dict[str, Any]
    capabilities: list[dict[str, Any]]


class CampaignListResponse(StrictModel):
    items: list[dict[str, Any]]
    total: int


class IntentListResponse(StrictModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class IntentDetailResponse(StrictModel):
    project: dict[str, Any]
    destinations: list[dict[str, Any]]
    validation: dict[str, Any] | None = None


class DestinationResponse(StrictModel):
    variant: dict[str, Any]
    job: dict[str, Any]


class ValidationResponse(StrictModel):
    report: dict[str, Any]


class ApprovalStatusResponse(StrictModel):
    pending: bool
    request_id: str | None
    state: str | None
    payload_hash: str | None
    approved_hash: str | None
    payload: dict[str, Any] | None
    consent_text_version: str
    can_request: bool
    reason: str | None = None


class ScheduleResponse(StrictModel):
    now: datetime
    scheduled: list[dict[str, Any]]
    missed: list[dict[str, Any]]
    missed_total: int
    policy: dict[str, Any]


class QueueResponse(StrictModel):
    running: list[dict[str, Any]]
    running_total: int
    unknown: list[dict[str, Any]]
    unknown_total: int
    leases_held: int


class DeliveryResponse(StrictModel):
    configured: bool
    provider: str | None
    bucket: str | None
    region: str | None
    public_base_url_present: bool
    prefix: str | None
    ttl_seconds: int | None
    credentials_present: bool
    reason: str | None
    objects: list[dict[str, Any]]
    supported_providers: list[str]


class DeliveryCheckResponse(StrictModel):
    ok: bool
    reason: str
    detail: dict[str, Any] = Field(default_factory=dict)


class LimitsResponse(StrictModel):
    platform: str
    mode: str
    rules: dict[str, Any]


class ReconciliationListResponse(StrictModel):
    items: list[dict[str, Any]]
    total: int


class AttemptListResponse(StrictModel):
    items: list[dict[str, Any]]
    total: int


class AuditTrailResponse(StrictModel):
    items: list[dict[str, Any]]
