"""The tool surface agents are allowed to call.

Twelve deterministic, local tools. Every one of them reads or writes real rows or real
files; none of them can reach the shell, arbitrary paths, the network or secrets.
Authorization is decided twice: the database's `tool_registry` must declare the tool
and mark it enabled, and the agent definition must explicitly grant it. A call is
recorded in the audit log with its duration and outcome.

Handlers are synchronous because the durable queue worker that drives orchestration is
synchronous; the only slow handler runs ffprobe with a hard timeout.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..core.paths import ensure_dir, is_inside_root, resolve_under_root
from ..core.settings import get_settings
from ..db.enums import (
    ApprovalRequirement,
    ConnectionStatus,
    MemoryScope,
    PublishState,
    RightsStatus,
    ToolRisk,
    parse_publish_state,
)
from ..db.models import (
    AgentMemory,
    ApprovalRequest,
    AppSetting,
    BrandProfile,
    ContentProject,
    MediaAsset,
    PlatformVariant,
    PublishJob,
    SocialAccount,
    Transcript,
)
from ..db.models import (
    ToolRegistry as ToolRegistryRow,
)
from .analytics.tools import (
    AnalyticsComparisonReadInput,
    AnalyticsMetricsReadInput,
    AnalyticsPublicationsReadInput,
    AnalyticsRecommendationProposeInput,
    AnalyticsRecommendationsReadInput,
    analytics_comparison_read,
    analytics_metrics_read,
    analytics_publications_read,
    analytics_recommendation_propose,
    analytics_recommendations_read,
)
from .backup import AuditService

log = get_logger(__name__)


# --------------------------------------------------------------------- errors
class ToolError(Exception):
    code = "TOOL_FAILED"
    status = 400

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail
        # A handler may narrow the code (e.g. NO_TRANSCRIPT) without inventing a class.
        if isinstance(detail.get("code"), str):
            self.code = detail["code"]


class ToolNotFound(ToolError):
    code = "TOOL_NOT_FOUND"
    status = 404


class ToolDenied(ToolError):
    code = "TOOL_DENIED"
    status = 403


class ToolInvalidInput(ToolError):
    code = "TOOL_INVALID_INPUT"
    status = 422


class ToolTimeout(ToolError):
    code = "TOOL_TIMEOUT"
    status = 504


class ToolUnavailable(ToolError):
    code = "TOOL_UNAVAILABLE"
    status = 409


# --------------------------------------------------------------------- inputs
class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectRef(_Base):
    project_id: str = Field(min_length=1, max_length=26)


class ContentRef(_Base):
    variant_id: str | None = Field(default=None, max_length=26)
    project_id: str | None = Field(default=None, max_length=26)

    def require_one(self) -> None:
        if not self.variant_id and not self.project_id:
            raise ToolInvalidInput("provide variant_id or project_id")


class AssetRef(_Base):
    asset_id: str = Field(min_length=1, max_length=26)


class SceneScanInput(_Base):
    """Bounded scene scan: the agent cannot ask for an unbounded decode."""

    asset_id: str = Field(min_length=1, max_length=26)
    threshold: float = Field(default=0.35, ge=0.1, le=0.9)
    max_scenes: int = Field(default=12, ge=1, le=30)


class QualityInput(_Base):
    asset_id: str = Field(min_length=1, max_length=26)
    platform: str = Field(default="instagram_reels", max_length=40)


class SocialAccountsInput(_Base):
    """Read account metadata. There is no field for a token, and no way to name one."""

    platform: Literal["facebook", "instagram", "tiktok"] | None = None
    capability: str | None = Field(default=None, max_length=64)


class SocialCapabilitiesInput(_Base):
    account_id: str | None = Field(default=None, max_length=26)
    platform: Literal["facebook", "instagram", "tiktok"] | None = None


class PublishQueueInput(_Base):
    """Read the publishing queue. There is no field for a URL, a path or a credential."""

    platform: Literal["facebook", "instagram", "tiktok"] | None = None
    account_id: str | None = Field(default=None, max_length=26)
    state: str | None = Field(default=None, max_length=40)
    limit: int = Field(default=20, ge=1, le=50)


class PublishDestinationInput(_Base):
    job_id: str = Field(min_length=1, max_length=26)


class PublishDraftInput(_Base):
    """Create a draft destination. Approval and publishing are deliberately not expressible here."""

    title: str = Field(min_length=1, max_length=300)
    platform: Literal["facebook", "instagram", "tiktok"]
    account_id: str = Field(min_length=1, max_length=26)
    caption: str = Field(default="", max_length=2000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    asset_id: str | None = Field(default=None, max_length=26)
    project_id: str | None = Field(default=None, max_length=26)
    scheduled_at_utc: datetime | None = None
    display_timezone: str | None = Field(default=None, max_length=64)


class TranscriptRef(_Base):
    transcript_id: str | None = Field(default=None, max_length=26)
    asset_id: str | None = Field(default=None, max_length=26)


class BrandRef(_Base):
    brand_profile_id: str | None = Field(default=None, max_length=26)


class SearchInput(_Base):
    query: str = Field(min_length=2, max_length=200)
    project_id: str | None = Field(default=None, max_length=26)
    limit: int = Field(default=8, ge=1, le=25)


class CaptionInput(_Base):
    platform: Literal["facebook", "instagram", "tiktok"] = "instagram"
    hook: str = Field(min_length=1, max_length=400)
    body: str = Field(default="", max_length=1600)
    cta: str = Field(default="", max_length=200)
    hashtags: list[str] = Field(default_factory=list, max_length=30)


class ReadinessInput(_Base):
    project_id: str = Field(min_length=1, max_length=26)
    platforms: list[Literal["facebook", "instagram", "tiktok"]] = Field(default_factory=list, max_length=3)


class PlanInput(_Base):
    project_id: str = Field(min_length=1, max_length=26)
    platforms: list[Literal["facebook", "instagram", "tiktok"]] = Field(default_factory=list, max_length=3)
    scheduled_at_utc: datetime | None = None
    display_timezone: str = Field(default="UTC", max_length=64)


class ReportSectionInput(_Base):
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)


class ReportInput(_Base):
    title: str = Field(min_length=1, max_length=200)
    sections: list[ReportSectionInput] = Field(min_length=1, max_length=15)


class ApprovalInput(_Base):
    action: str = Field(min_length=3, max_length=80)
    target_type: str = Field(min_length=2, max_length=60)
    target_id: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=3, max_length=1000)
    rationale: str = Field(default="", max_length=2000)
    risks: list[str] = Field(default_factory=list, max_length=10)
    payload: dict[str, Any] = Field(default_factory=dict)
    ttl_hours: int = Field(default=72, ge=1, le=720)


# -------------------------------------------------------------------- context
@dataclass(slots=True)
class ToolContext:
    session: Session
    agent_id: str
    task_id: str | None = None
    objective_id: str | None = None
    correlation_id: str | None = None
    actor: str = "agent"


@dataclass(slots=True)
class ToolSpec:
    id: str
    name: str
    description: str
    category: Literal["read", "compute", "write"]
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], dict[str, Any]]
    timeout_s: float = 10.0
    risk: ToolRisk = ToolRisk.READ
    approval: ApprovalRequirement = ApprovalRequirement.NONE
    audit: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------------ helpers
def _require(value: Any, code: str, message: str) -> Any:
    if value is None:
        raise ToolUnavailable(message, code=code)
    return value


def _project(session: Session, project_id: str) -> ContentProject:
    project = session.get(ContentProject, project_id)
    if project is None:
        raise ToolInvalidInput(f"content project {project_id} does not exist")
    return project


def _variants(session: Session, project_id: str) -> list[PlatformVariant]:
    return list(
        session.scalars(
            sa.select(PlatformVariant).where(PlatformVariant.project_id == project_id).order_by(PlatformVariant.platform)
        )
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ handlers
def read_project_metadata(ctx: ToolContext, payload: ProjectRef) -> dict[str, Any]:
    project = _project(ctx.session, payload.project_id)
    brand = ctx.session.get(BrandProfile, project.brand_profile_id) if project.brand_profile_id else None
    asset = ctx.session.get(MediaAsset, project.source_asset_id) if project.source_asset_id else None
    return {
        "project_id": project.id,
        "title": project.title,
        "brief": project.brief,
        "status": str(project.status),
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "brand": None
        if brand is None
        else {
            "id": brand.id,
            "name": brand.name,
            "voice": brand.voice_json,
            "banned_terms": brand.banned_terms_json,
        },
        "source_asset": None
        if asset is None
        else {
            "id": asset.id,
            "duration_ms": asset.duration_ms,
            "width": asset.width,
            "height": asset.height,
            "has_audio": asset.has_audio,
            "rights_status": str(asset.rights_status),
        },
        "variant_count": ctx.session.scalar(
            sa.select(sa.func.count()).select_from(PlatformVariant).where(PlatformVariant.project_id == project.id)
        )
        or 0,
    }


def read_content_metadata(ctx: ToolContext, payload: ContentRef) -> dict[str, Any]:
    payload.require_one()
    if payload.variant_id:
        variant = ctx.session.get(PlatformVariant, payload.variant_id)
        if variant is None:
            raise ToolInvalidInput(f"variant {payload.variant_id} does not exist")
        return {
            "variant_id": variant.id,
            "project_id": variant.project_id,
            "platform": str(variant.platform),
            "aspect_ratio": variant.aspect_ratio,
            "duration_ms": variant.duration_ms,
            "caption": variant.caption,
            "caption_chars": len(variant.caption or ""),
            "hashtags": list(variant.hashtags_json or []),
            "cta": variant.cta,
            "approval_status": str(variant.approval_status),
            "has_output_file": bool(variant.output_path),
        }
    project = _project(ctx.session, str(payload.project_id))
    return {
        "project_id": project.id,
        "title": project.title,
        "brief": project.brief,
        "status": str(project.status),
        "variants": [
            {
                "variant_id": variant.id,
                "platform": str(variant.platform),
                "caption_chars": len(variant.caption or ""),
                "approval_status": str(variant.approval_status),
            }
            for variant in _variants(ctx.session, project.id)
        ],
    }


def list_content_variants(ctx: ToolContext, payload: ProjectRef) -> dict[str, Any]:
    project = _project(ctx.session, payload.project_id)
    variants = _variants(ctx.session, project.id)
    return {
        "project_id": project.id,
        "count": len(variants),
        "variants": [
            {
                "variant_id": variant.id,
                "platform": str(variant.platform),
                "aspect_ratio": variant.aspect_ratio,
                "duration_ms": variant.duration_ms,
                "caption_chars": len(variant.caption or ""),
                "hashtag_count": len(variant.hashtags_json or []),
                "approval_status": str(variant.approval_status),
                "output_path": variant.output_path,
            }
            for variant in variants
        ],
    }


def inspect_media(ctx: ToolContext, payload: AssetRef) -> dict[str, Any]:
    asset = ctx.session.get(MediaAsset, payload.asset_id)
    if asset is None:
        raise ToolInvalidInput(f"media asset {payload.asset_id} does not exist")
    result: dict[str, Any] = {
        "asset_id": asset.id,
        "sha256": asset.sha256,
        "original_filename": asset.original_filename,
        "size_bytes": asset.size_bytes,
        "stored": {
            "duration_ms": asset.duration_ms,
            "width": asset.width,
            "height": asset.height,
            "fps": asset.fps,
            "video_codec": asset.video_codec,
            "audio_codec": asset.audio_codec,
            "has_audio": asset.has_audio,
            "rights_status": str(asset.rights_status),
            "archive_state": str(asset.archive_state),
        },
        "probe": {"ran": False, "reason": None},
        "orientation": None,
    }
    if asset.width and asset.height:
        result["orientation"] = "portrait" if asset.width < asset.height else (
            "landscape" if asset.width > asset.height else "square"
        )

    path = Path(asset.source_path)
    if not path.exists():
        result["probe"] = {"ran": False, "reason": "source file is no longer on disk"}
        return result
    if not is_inside_root(path):
        # Never let an agent cause a read outside the data root, whatever the row says.
        result["probe"] = {"ran": False, "reason": "source path is outside the CreatorOS root"}
        return result

    ffprobe = get_settings().ffmpeg_bin / "ffprobe.exe"
    if not ffprobe.exists():
        result["probe"] = {"ran": False, "reason": "ffprobe is not installed"}
        return result
    try:
        completed = subprocess.run(
            [
                str(ffprobe),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name,avg_frame_rate:format=duration,bit_rate,size",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["probe"] = {"ran": False, "reason": "ffprobe timed out after 8s"}
        return result
    if completed.returncode != 0:
        result["probe"] = {"ran": False, "reason": f"ffprobe failed: {completed.stderr.strip()[:200]}"}
        return result
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        result["probe"] = {"ran": False, "reason": "ffprobe returned unparseable output"}
        return result
    stream = (parsed.get("streams") or [{}])[0]
    fmt = parsed.get("format") or {}
    result["probe"] = {
        "ran": True,
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
        "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
    }
    probed_w, probed_h = stream.get("width"), stream.get("height")
    if probed_w and probed_h and asset.width and asset.height:
        result["matches_stored"] = (probed_w == asset.width and probed_h == asset.height)
    return result


def read_transcript(ctx: ToolContext, payload: TranscriptRef) -> dict[str, Any]:
    stmt = sa.select(Transcript)
    if payload.transcript_id:
        stmt = stmt.where(Transcript.id == payload.transcript_id)
    elif payload.asset_id:
        stmt = stmt.where(Transcript.asset_id == payload.asset_id).order_by(Transcript.created_at.desc())
    else:
        raise ToolInvalidInput("provide transcript_id or asset_id")
    transcript = ctx.session.scalars(stmt.limit(1)).first()
    _require(transcript, "NO_TRANSCRIPT", "no transcript is stored for that media asset")
    segments = transcript.segments_json or []
    return {
        "transcript_id": transcript.id,
        "asset_id": transcript.asset_id,
        "language": transcript.language,
        "model": transcript.model,
        "text": transcript.text,
        "chars": len(transcript.text or ""),
        "segment_count": len(segments),
        "first_segments": segments[:5],
        "has_subtitle_files": bool(transcript.srt_path or transcript.vtt_path),
    }


def read_brand_rules(ctx: ToolContext, payload: BrandRef) -> dict[str, Any]:
    brand = None
    if payload.brand_profile_id:
        brand = ctx.session.get(BrandProfile, payload.brand_profile_id)
        if brand is None:
            raise ToolInvalidInput(f"brand profile {payload.brand_profile_id} does not exist")
    else:
        brand = ctx.session.scalars(sa.select(BrandProfile).order_by(BrandProfile.created_at)).first()
    setting = ctx.session.get(AppSetting, "brand.rules")
    rules = (setting.value_json if setting else None) or {}
    if brand is None:
        return {
            "brand": None,
            "rules": rules,
            "warnings": ["no brand profile exists yet; platform and tone rules are defaults"],
        }
    return {
        "brand": {
            "id": brand.id,
            "name": brand.name,
            "voice": brand.voice_json,
            "banned_terms": brand.banned_terms_json,
            "cta_phrases": brand.cta_phrases_json,
            "hashtag_sets": brand.hashtag_sets_json,
            "subtitle_style": brand.subtitle_style_json,
        },
        "rules": rules,
    }


def internal_search(ctx: ToolContext, payload: SearchInput) -> dict[str, Any]:
    needle = f"%{payload.query.strip().lower()}%"
    hits: list[dict[str, Any]] = []

    project_stmt = sa.select(ContentProject).where(
        sa.or_(
            sa.func.lower(ContentProject.title).like(needle),
            sa.func.lower(sa.func.coalesce(ContentProject.brief, "")).like(needle),
        )
    )
    if payload.project_id:
        project_stmt = project_stmt.where(ContentProject.id == payload.project_id)
    for project in ctx.session.scalars(project_stmt.limit(payload.limit)):
        hits.append(
            {
                "kind": "content_project",
                "id": project.id,
                "title": project.title,
                "excerpt": (project.brief or "")[:240],
            }
        )

    variant_stmt = sa.select(PlatformVariant).where(
        sa.func.lower(PlatformVariant.caption).like(needle)
    )
    if payload.project_id:
        variant_stmt = variant_stmt.where(PlatformVariant.project_id == payload.project_id)
    for variant in ctx.session.scalars(variant_stmt.limit(payload.limit)):
        hits.append(
            {
                "kind": "platform_variant",
                "id": variant.id,
                "title": f"{variant.platform} variant",
                "excerpt": (variant.caption or "")[:240],
            }
        )

    transcript_stmt = (
        sa.select(Transcript)
        .where(sa.func.lower(Transcript.text).like(needle))
        .limit(max(1, payload.limit // 2))
    )
    for transcript in ctx.session.scalars(transcript_stmt):
        index = (transcript.text or "").lower().find(payload.query.strip().lower())
        start = max(0, index - 80)
        hits.append(
            {
                "kind": "transcript",
                "id": transcript.id,
                "title": f"transcript ({transcript.language})",
                "excerpt": (transcript.text or "")[start : start + 240],
            }
        )

    memory_stmt = sa.select(AgentMemory).where(
        sa.or_(
            sa.func.lower(AgentMemory.title).like(needle),
            sa.func.lower(AgentMemory.content).like(needle),
        )
    )
    if payload.project_id:
        memory_stmt = memory_stmt.where(AgentMemory.project_id == payload.project_id)
    for memory in ctx.session.scalars(memory_stmt.limit(payload.limit)):
        hits.append(
            {
                "kind": "agent_memory",
                "id": memory.id,
                "title": memory.title or "(memory)",
                "excerpt": (memory.content or "")[:240],
            }
        )

    return {"query": payload.query, "count": len(hits), "results": hits[: payload.limit]}


PLATFORM_CAPTION_LIMITS = {"facebook": 63206, "instagram": 2200, "tiktok": 2200}
PLATFORM_HASHTAG_LIMITS = {"facebook": 30, "instagram": 30, "tiktok": 20}


def compose_caption(ctx: ToolContext, payload: CaptionInput) -> dict[str, Any]:
    """Deterministic caption assembly. No model call: same input, same output."""
    hashtags = []
    for raw in payload.hashtags:
        tag = raw.strip().lstrip("#").replace(" ", "")
        if tag and tag not in hashtags:
            hashtags.append(tag)
    limit = PLATFORM_CAPTION_LIMITS[payload.platform]
    tag_limit = PLATFORM_HASHTAG_LIMITS[payload.platform]
    warnings: list[str] = []
    if len(hashtags) > tag_limit:
        warnings.append(f"{payload.platform} allows at most {tag_limit} hashtags; keeping the first {tag_limit}")
        hashtags = hashtags[:tag_limit]

    blocks = [payload.hook.strip()]
    if payload.body.strip():
        blocks.append(payload.body.strip())
    if payload.cta.strip():
        blocks.append(payload.cta.strip())
    body_text = "\n\n".join(block for block in blocks if block)
    tag_line = " ".join(f"#{tag}" for tag in hashtags)
    caption = f"{body_text}\n\n{tag_line}".strip() if tag_line else body_text
    if len(caption) > limit:
        overflow = len(caption) - limit
        warnings.append(f"caption exceeds the {payload.platform} limit by {overflow} characters")
    text_hash = _sha256_text(caption)
    return {
        "platform": payload.platform,
        "caption": caption,
        "caption_hash": text_hash,
        "chars": len(caption),
        "limit": limit,
        "hashtags": hashtags,
        "over_limit": len(caption) > limit,
        "readability": {
            "first_line_chars": len(payload.hook.strip()),
            "hook_above_fold": len(payload.hook.strip()) <= 125,
        },
        "warnings": warnings,
    }


def check_readiness(ctx: ToolContext, payload: ReadinessInput) -> dict[str, Any]:
    project = _project(ctx.session, payload.project_id)
    variants = {str(variant.platform): variant for variant in _variants(ctx.session, project.id)}
    platforms = payload.platforms or list(variants.keys())
    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "check": "brief_present",
            "passed": bool(project.brief and project.brief.strip()),
            "detail": "project has a brief" if project.brief else "project brief is empty",
            "blocking": True,
        }
    )

    asset = ctx.session.get(MediaAsset, project.source_asset_id) if project.source_asset_id else None
    checks.append(
        {
            "check": "source_asset_linked",
            "passed": asset is not None,
            "detail": "source asset linked" if asset else "no source media asset is linked",
            "blocking": True,
        }
    )
    if asset is not None:
        rights_ok = asset.rights_status in (RightsStatus.OWNED, RightsStatus.LICENSED)
        checks.append(
            {
                "check": "rights_cleared",
                "passed": rights_ok,
                "detail": f"rights status is {asset.rights_status}",
                "blocking": True,
            }
        )

    for platform in platforms:
        variant = variants.get(platform)
        if variant is None:
            checks.append(
                {
                    "check": f"variant_{platform}",
                    "passed": False,
                    "detail": f"no {platform} variant exists",
                    "blocking": True,
                }
            )
            continue
        limit = PLATFORM_CAPTION_LIMITS.get(platform, 2200)
        checks.append(
            {
                "check": f"caption_{platform}",
                "passed": 0 < len(variant.caption or "") <= limit,
                "detail": f"{len(variant.caption or '')} characters (limit {limit})",
                "blocking": True,
            }
        )
        checks.append(
            {
                "check": f"media_{platform}",
                "passed": bool(variant.output_path),
                "detail": variant.output_path or "no rendered media for this platform",
                "blocking": True,
            }
        )
        account = ctx.session.scalars(
            sa.select(SocialAccount).where(
                SocialAccount.platform == platform,
                SocialAccount.connection_status == ConnectionStatus.CONNECTED,
            )
        ).first()
        checks.append(
            {
                "check": f"account_{platform}",
                "passed": account is not None,
                "detail": "connected account found" if account else "no connected account for this platform",
                "blocking": True,
            }
        )
        existing = ctx.session.scalars(
            sa.select(PublishJob).where(
                PublishJob.variant_id == variant.id,
                PublishJob.job_state.in_([PublishState.DRAFT, PublishState.SCHEDULED, PublishState.QUEUED]),
            )
        ).first()
        checks.append(
            {
                "check": f"no_open_job_{platform}",
                "passed": existing is None,
                "detail": "no open publish job" if existing is None else f"an open job already exists ({existing.id})",
                "blocking": True,
            }
        )

    blocking = [check for check in checks if check["blocking"] and not check["passed"]]
    return {
        "project_id": project.id,
        "platforms": platforms,
        "checks": checks,
        "blocking_failures": blocking,
        "ready": not blocking,
    }


def generate_publish_plan(ctx: ToolContext, payload: PlanInput) -> dict[str, Any]:
    """Create draft publish jobs. Nothing leaves the machine; approval is still required."""
    project = _project(ctx.session, payload.project_id)
    variants = {str(variant.platform): variant for variant in _variants(ctx.session, project.id)}
    platforms = payload.platforms or list(variants.keys())
    created: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for platform in platforms:
        variant = variants.get(platform)
        if variant is None:
            blocked.append({"platform": platform, "reason": "NO_VARIANT", "detail": "no adapted variant exists"})
            continue
        account = ctx.session.scalars(
            sa.select(SocialAccount).where(
                SocialAccount.platform == platform,
                SocialAccount.connection_status == ConnectionStatus.CONNECTED,
            )
        ).first()
        if account is None:
            blocked.append(
                {
                    "platform": platform,
                    "reason": "NO_CONNECTED_ACCOUNT",
                    "detail": "connect an account for this platform (Phase 6) before a job can be created",
                }
            )
            continue
        key_source = json.dumps(
            {
                "variant": variant.id,
                "platform": platform,
                "account": account.id,
                "scheduled": payload.scheduled_at_utc.isoformat() if payload.scheduled_at_utc else None,
            },
            sort_keys=True,
        )
        idempotency_key = _sha256_text(key_source)[:64]
        existing = ctx.session.scalars(
            sa.select(PublishJob).where(PublishJob.idempotency_key == idempotency_key)
        ).first()
        if existing is not None:
            blocked.append(
                {
                    "platform": platform,
                    "reason": "DUPLICATE_PLAN",
                    "detail": f"job {existing.id} already exists for this plan",
                }
            )
            continue
        job = PublishJob(
            variant_id=variant.id,
            platform=platform,
            account_id=account.id,
            scheduled_at_utc=payload.scheduled_at_utc,
            display_timezone=payload.display_timezone,
            job_state=PublishState.DRAFT,
            idempotency_key=idempotency_key,
        )
        ctx.session.add(job)
        ctx.session.flush()
        created.append(
            {
                "job_id": job.id,
                "platform": platform,
                "variant_id": variant.id,
                "account_id": account.id,
                "state": str(job.job_state),
                "scheduled_at_utc": payload.scheduled_at_utc.isoformat() if payload.scheduled_at_utc else None,
            }
        )

    return {
        "project_id": project.id,
        "created": created,
        "blocked": blocked,
        "external_actions_taken": False,
        "approval_required": True,
        "note": "Draft jobs only. Nothing is uploaded, and no platform was contacted.",
    }


def generate_report(ctx: ToolContext, payload: ReportInput) -> dict[str, Any]:
    """Write a Markdown report under the data root and return its identity."""
    folder = ensure_dir("media_exports", "reports")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    safe_title = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in payload.title.lower()
    )[:60].strip("-") or "report"
    filename = f"{stamp}-{safe_title}-{_sha256_text(payload.title)[:8]}.md"
    path = resolve_under_root("media_exports", "reports", filename)
    lines = [f"# {payload.title}", "", f"Generated: {datetime.now(UTC).isoformat()}", ""]
    for section in payload.sections:
        lines.extend([f"## {section.heading}", "", section.body.strip(), ""])
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8", newline="\n")
    return {
        "path": str(path),
        "filename": path.name,
        "sha256": _sha256_text(text),
        "bytes": len(text.encode("utf-8")),
        "sections": len(payload.sections),
        "folder": str(folder),
    }


def request_human_approval(ctx: ToolContext, payload: ApprovalInput) -> dict[str, Any]:
    """Record intent only. This creates a request a human must decide on."""
    canonical = json.dumps(payload.payload, sort_keys=True, separators=(",", ":"), default=str)
    payload_hash = _sha256_text(canonical)
    expires_at = datetime.now(UTC) + timedelta(hours=payload.ttl_hours)
    duplicate = ctx.session.scalars(
        sa.select(ApprovalRequest).where(
            ApprovalRequest.payload_hash == payload_hash,
            ApprovalRequest.state == "pending",
        )
    ).first()
    if duplicate is not None:
        raise ToolUnavailable(
            f"an identical approval request is already pending ({duplicate.id})",
            code="APPROVAL_ALREADY_PENDING",
        )
    request = ApprovalRequest(
        action=payload.action,
        state="pending",
        agent_id=ctx.agent_id,
        task_id=ctx.task_id,
        objective_id=ctx.objective_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        summary=payload.summary,
        rationale=payload.rationale or None,
        risks_json=list(payload.risks),
        payload_json=payload.payload,
        payload_hash=payload_hash,
        requested_by=ctx.agent_id,
        expires_at=expires_at,
    )
    ctx.session.add(request)
    ctx.session.flush()
    return {
        "approval_request_id": request.id,
        "action": payload.action,
        "state": str(request.state),
        "payload_hash": payload_hash,
        "expires_at": expires_at.isoformat(),
        "executed": False,
        "note": "Intent recorded. A human must approve before anything external can happen.",
    }


def list_media_derivatives(ctx: ToolContext, payload: AssetRef) -> dict[str, Any]:
    """List the derivatives that actually exist on disk for an asset."""
    from .media.library import MediaLibrary
    from .media.storage import MediaStorage

    library = MediaLibrary(ctx.session)
    asset = ctx.session.get(MediaAsset, payload.asset_id)
    if asset is None:
        raise ToolInvalidInput(f"media asset {payload.asset_id} does not exist")
    rows: list[dict[str, Any]] = []
    for row in library.derivatives(asset.id):
        # Resolve without raising: a row whose file vanished is a fact to report, not an error.
        path = None
        if row.rel_path:
            try:
                path = library.storage.resolve_stored(row.rel_path)
            except Exception:  # noqa: BLE001 - an unresolvable path is reported as on_disk=False
                path = None
        exists = bool(path and path.exists())
        rows.append(
            {
                "kind": str(row.kind),
                "profile": row.profile,
                "version": row.version,
                "state": str(row.state),
                "width": row.width,
                "height": row.height,
                "duration_ms": row.duration_ms,
                "size_bytes": row.size_bytes,
                "mime_type": row.mime_type,
                "on_disk": exists,
                "verified": exists and row.size_bytes > 0,
                "error_code": row.error_code,
            }
        )
    return {
        "asset_id": asset.id,
        "count": len(rows),
        "ready": sum(1 for row in rows if row["state"] == "ready" and row["on_disk"]),
        "derivatives": rows,
        "areas": {
            area: {"files": len(list(MediaStorage.area_dir(area).glob("*"))), "path": str(MediaStorage.area_dir(area))}
            for area in ("thumbnails", "previews", "proxies", "transcodes", "audio", "waveforms")
        },
        "note": "Only rows whose files exist on disk are marked verified; nothing here is estimated.",
    }


def scan_media_scenes(ctx: ToolContext, payload: SceneScanInput) -> dict[str, Any]:
    """Measure scene-change timestamps with FFmpeg's scene score. Bounded and local."""
    asset = ctx.session.get(MediaAsset, payload.asset_id)
    if asset is None:
        raise ToolInvalidInput(f"media asset {payload.asset_id} does not exist")
    path = Path(asset.source_path)
    if not path.exists():
        raise ToolUnavailable("the original file is no longer on disk", code="SOURCE_MISSING")
    if not is_inside_root(path):
        raise ToolDenied("the source path is outside the CreatorOS root")
    if str(asset.kind) != "video":
        raise ToolUnavailable("scene scanning needs a video asset", code="NOT_A_VIDEO")
    ffmpeg = get_settings().ffmpeg_bin / "ffmpeg.exe"
    if not ffmpeg.exists():
        raise ToolUnavailable("ffmpeg is not installed", code="FFMPEG_MISSING")

    duration_s = (asset.duration_ms or 0) / 1000
    command = [
        str(ffmpeg), "-hide_banner", "-nostats", "-loglevel", "info",
        "-i", str(path),
        "-filter_complex",
        f"select='gt(scene,{payload.threshold})',metadata=print:file=-",
        "-an", "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=25, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeout("scene scan exceeded its 25s budget") from exc
    if completed.returncode != 0:
        tail = completed.stderr.strip().splitlines()
        raise ToolUnavailable(
            f"ffmpeg scene detection failed: {tail[-1][:180] if tail else 'no output'}",
            code="SCENE_SCAN_FAILED",
        )
    timestamps: list[float] = []
    for line in (completed.stdout or "").splitlines():
        if "pts_time:" in line:
            raw = line.split("pts_time:", 1)[1].split()[0]
            try:
                timestamps.append(round(float(raw), 3))
            except ValueError:
                continue
    timestamps = sorted(set(timestamps))[: payload.max_scenes]
    gaps = [round(b - a, 3) for a, b in itertools.pairwise(timestamps)]
    return {
        "asset_id": asset.id,
        "threshold": payload.threshold,
        "duration_s": round(duration_s, 3) if duration_s else None,
        "scene_count": len(timestamps),
        "scenes": timestamps,
        "typical_gap_s": round(sum(gaps) / len(gaps), 3) if gaps else None,
        "truncated": len(timestamps) >= payload.max_scenes,
        "method": "ffmpeg select=gt(scene,threshold) metadata=print",
        "note": "Timestamps are measured frame positions, not guessed cut points.",
    }


def recommend_media_transcode(ctx: ToolContext, payload: AssetRef) -> dict[str, Any]:
    """Suggest a transcode profile from stored probe facts, with an explicit reason."""
    from .media.profiles import list_profiles, recommend_profile

    asset = ctx.session.get(MediaAsset, payload.asset_id)
    if asset is None:
        raise ToolInvalidInput(f"media asset {payload.asset_id} does not exist")
    kind = str(asset.kind)
    if kind == "unknown":
        raise ToolUnavailable("this asset has no probe result yet", code="NOT_PROBED")
    recommendation = recommend_profile(
        kind=kind,
        width=asset.width,
        height=asset.height,
        duration_ms=asset.duration_ms,
        has_audio=bool(asset.has_audio),
    )
    chosen = recommendation["profile"]
    usable = [item for item in list_profiles() if (item["kind"] == "audio") == (kind == "audio")]
    return {
        "asset_id": asset.id,
        "kind": kind,
        "recommendation": recommendation,
        "alternatives": [item["id"] for item in usable if item["id"] != chosen],
        "facts": {
            "width": asset.width,
            "height": asset.height,
            "duration_ms": asset.duration_ms,
            "fps": asset.fps,
            "has_audio": bool(asset.has_audio),
            "size_bytes": asset.size_bytes,
        },
        "executed": False,
        "note": "This is a rule over measured facts. Nothing was encoded.",
    }


def media_quality_report(ctx: ToolContext, payload: QualityInput) -> dict[str, Any]:
    """Deterministic technical quality check against a platform's published limits."""
    from .media.library import MediaLibrary
    from .media.profiles import recommend_profile

    asset = ctx.session.get(MediaAsset, payload.asset_id)
    if asset is None:
        raise ToolInvalidInput(f"media asset {payload.asset_id} does not exist")
    limits = {
        "instagram_reels": {"max_s": 90, "max_mb": 1000, "min_edge": 500, "aspect": (0.5, 0.6)},
        "tiktok": {"max_s": 600, "max_mb": 4000, "min_edge": 540, "aspect": (0.5, 0.6)},
        "youtube_shorts": {"max_s": 180, "max_mb": 256000, "min_edge": 720, "aspect": (0.5, 0.6)},
        "facebook_reels": {"max_s": 90, "max_mb": 1000, "min_edge": 500, "aspect": (0.5, 0.6)},
    }
    spec = limits.get(payload.platform)
    if spec is None:
        raise ToolInvalidInput(f"unknown platform {payload.platform}; use one of {sorted(limits)}")

    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    duration_s = (asset.duration_ms or 0) / 1000
    if duration_s and duration_s > spec["max_s"]:
        issues.append({"code": "TOO_LONG", "detail": f"{duration_s:.1f}s exceeds the {spec['max_s']}s limit"})
    size_mb = (asset.size_bytes or 0) / (1024 * 1024)
    if size_mb > spec["max_mb"]:
        issues.append({"code": "FILE_TOO_LARGE", "detail": f"{size_mb:.0f} MB exceeds the {spec['max_mb']} MB limit"})
    if str(asset.kind) != "video":
        issues.append(
            {"code": "NO_VIDEO", "detail": f"{payload.platform} needs a video stream; this asset is {asset.kind}"}
        )
    short_edge = min(asset.width or 0, asset.height or 0)
    if short_edge and short_edge < spec["min_edge"]:
        issues.append(
            {"code": "LOW_RESOLUTION", "detail": f"{short_edge}px short edge is below {spec['min_edge']}px"}
        )
    ratio = (asset.width / asset.height) if (asset.width and asset.height) else None
    if ratio is not None:
        low, high = spec["aspect"]
        if not low <= ratio <= high:
            warnings.append(
                {"code": "NOT_VERTICAL", "detail": f"aspect {ratio:.2f} is outside the 9:16 band; expect letterboxing"}
            )
    if not asset.has_audio:
        warnings.append({"code": "NO_AUDIO", "detail": "the file has no audio stream"})
    if str(asset.rights_status) not in ("owned", "licensed"):
        warnings.append({"code": "RIGHTS_UNCONFIRMED", "detail": f"rights status is {asset.rights_status}"})

    library = MediaLibrary(ctx.session)
    ready = [row for row in library.derivatives(asset.id) if str(row.state) == "ready"]
    return {
        "asset_id": asset.id,
        "platform": payload.platform,
        "limits": spec,
        "facts": {
            "duration_s": round(duration_s, 2) if duration_s else None,
            "size_mb": round(size_mb, 2),
            "width": asset.width,
            "height": asset.height,
            "fps": asset.fps,
            "aspect_ratio": round(ratio, 3) if ratio else None,
            "video_codec": asset.video_codec,
            "audio_codec": asset.audio_codec,
            "has_audio": bool(asset.has_audio),
        },
        "issues": issues,
        "warnings": warnings,
        "verdict": "blocked" if issues else ("pass_with_warnings" if warnings else "pass"),
        "derivatives_ready": [f"{row.kind}:{row.profile}" for row in ready],
        "recommended_profile": recommend_profile(
            kind=str(asset.kind),
            width=asset.width,
            height=asset.height,
            duration_ms=asset.duration_ms,
            has_audio=bool(asset.has_audio),
        ),
        "note": "All checks are arithmetic over stored probe values; no model was involved.",
    }


# ------------------------------------------------------------------ social (Phase 6)
#
# Read-only by construction: these two tools list account metadata and capability states. They
# cannot return a token, cannot refresh one, and cannot publish anything. An agent that needs to
# know "can we post Reels to this account?" gets the stored verdict with its reason, which is
# exactly the decision Phase 7 will make.

SOCIAL_CAPABILITY_KEYS = (
    "facebook.page.publish_video",
    "facebook.page.publish_text",
    "instagram.publish_reels",
    "instagram.publish_image",
    "tiktok.upload_draft",
    "tiktok.direct_post",
    "manual.export_package",
)


def social_accounts_list(ctx: ToolContext, payload: SocialAccountsInput) -> dict[str, Any]:
    from ..db.models.platform import AccountCapability

    stmt = sa.select(SocialAccount)
    if payload.platform:
        stmt = stmt.where(SocialAccount.platform == payload.platform)
    accounts = list(ctx.session.execute(stmt.order_by(SocialAccount.platform, SocialAccount.display_name)).scalars())
    capability_rows = list(ctx.session.execute(sa.select(AccountCapability)).scalars())
    by_account: dict[str, dict[str, str]] = {}
    for row in capability_rows:
        state = row.state.value if hasattr(row.state, "value") else str(row.state)
        by_account.setdefault(row.account_id, {})[row.capability] = state

    items = []
    for account in accounts:
        capabilities = by_account.get(account.id, {})
        if payload.capability and account.id not in by_account:
            continue
        items.append(
            {
                "account_id": account.id,
                "platform": account.platform.value,
                "route": account.route.value if account.route else None,
                "account_type": account.account_type.value,
                "label": account.internal_label or account.display_name,
                "connection_status": account.connection_status.value,
                "token_state": account.token_state.value,
                "token_expires_at": account.token_expires_at.isoformat() if account.token_expires_at else None,
                "scopes": list(account.scopes_json or []),
                "capabilities": capabilities,
                "requested_capability": (
                    capabilities.get(payload.capability) if payload.capability else None
                ),
                "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else None,
            }
        )
    if payload.capability:
        items = [item for item in items if item["requested_capability"] is not None]
    return {
        "accounts": items,
        "count": len(items),
        "requested_capability": payload.capability,
        "note": (
            "Account metadata and stored capability states only. No credential leaves the vault, "
            "and this tool cannot post anything."
        ),
    }


def social_capabilities_read(ctx: ToolContext, payload: SocialCapabilitiesInput) -> dict[str, Any]:
    from ..db.models.platform import AccountCapability

    stmt = sa.select(AccountCapability)
    account_ids = None
    if payload.account_id:
        account = ctx.session.get(SocialAccount, payload.account_id)
        if account is None:
            raise ToolInvalidInput(f"account {payload.account_id} does not exist")
        account_ids = [account.id]
    elif payload.platform:
        account_ids = [
            row.id
            for row in ctx.session.execute(
                sa.select(SocialAccount).where(SocialAccount.platform == payload.platform)
            ).scalars()
        ]
    if account_ids is not None:
        if not account_ids:
            return {"capabilities": [], "count": 0, "note": "no account matches that filter"}
        stmt = stmt.where(AccountCapability.account_id.in_(account_ids))
    rows = list(ctx.session.execute(stmt.order_by(AccountCapability.account_id, AccountCapability.capability)).scalars())
    labels = {
        account.id: account.internal_label or account.display_name
        for account in ctx.session.execute(sa.select(SocialAccount)).scalars()
    }
    return {
        "capabilities": [
            {
                "account_id": row.account_id,
                "account_label": labels.get(row.account_id),
                "capability": row.capability,
                "state": row.state.value if hasattr(row.state, "value") else str(row.state),
                "reason": row.reason,
                "required_scopes": list(row.required_json or []),
                "missing_scopes": list(row.missing_json or []),
                "verified_at": row.verified_at.isoformat(),
                "api_version": row.api_version,
            }
            for row in rows
        ],
        "count": len(rows),
        "note": "Stored capability verdicts with their reasons. Publishing is Phase 7.",
    }


# ------------------------------------------------------------------ registry
def publishing_queue_read(ctx: ToolContext, payload: PublishQueueInput) -> dict[str, Any]:
    """What is queued, what is scheduled and what is stuck - without a credential or a path."""
    from ..services.publishing.history import JobQuery, PublishHistory
    from ..services.publishing.scheduler import PublishScheduler

    history = PublishHistory(ctx.session)
    states: tuple[Any, ...] = ()
    if payload.state:
        try:
            states = (parse_publish_state(payload.state),)
        except ValueError as exc:
            raise ToolInvalidInput(f"{payload.state!r} is not a publish state") from exc
    rows, total = history.jobs(
        JobQuery(
            platform=payload.platform,
            account_id=payload.account_id,
            state=states,
            limit=payload.limit,
        )
    )
    policy = PublishScheduler(ctx.session, executor=None).load_policy()
    return {
        "items": [
            {
                "job_id": row["id"],
                "platform": row["platform"],
                "mode": row["mode"],
                "account": row["account_name"],
                "state": row["state"],
                "state_reason": row["state_reason"],
                "scheduled_at_utc": row["scheduled_at_utc"],
                "attempts": row["attempts"],
                "permalink": row["provider_permalink"],
                "needs_human": row["state"]
                in {
                    "blocked",
                    "requires_user_action",
                    "unknown_remote_state",
                    "needs_reconciliation",
                    "failed",
                },
            }
            for row in rows
        ],
        "count": total,
        "late_policy": policy.as_dict(),
        "note": "An agent can read this queue. Only a human can approve or publish from it.",
    }


def publishing_destination_read(ctx: ToolContext, payload: PublishDestinationInput) -> dict[str, Any]:
    """One destination's history, with the same redaction the GUI gets."""
    from ..services.publishing.history import PublishHistory

    detail = PublishHistory(ctx.session).job(payload.job_id)
    if detail is None:
        raise ToolInvalidInput(f"destination {payload.job_id} does not exist")
    # Tool output is persisted as JSON, so instants become ISO strings here rather than failing later.
    return json.loads(json.dumps(detail, default=str))


def publishing_draft_create(ctx: ToolContext, payload: PublishDraftInput) -> dict[str, Any]:
    """Create an intent and one draft destination. It cannot approve, schedule a run or publish.

    The return value says what happens next, so a caller cannot read this as "it is going out".
    """
    from ..services.publishing.composer import ComposerService
    from ..services.publishing.errors import PublishingError

    composer = ComposerService(ctx.session)
    try:
        project_id = payload.project_id
        if project_id is None:
            project_id = composer.create_intent(
                title=payload.title, source_asset_id=payload.asset_id, brief=f"drafted by {ctx.agent_id}"
            ).id
        destination = composer.add_destination(
            project_id,
            platform=payload.platform,
            account_id=payload.account_id,
            title=payload.title,
            caption=payload.caption,
            hashtags=payload.hashtags,
            asset_id=payload.asset_id,
            scheduled_at_utc=payload.scheduled_at_utc,
            display_timezone=payload.display_timezone,
            field_sources={
                "title": f"agent:{ctx.agent_id}",
                "caption": f"agent:{ctx.agent_id}",
                "hashtags": f"agent:{ctx.agent_id}",
            },
        )
    except PublishingError as error:
        raise ToolInvalidInput(f"{error.code}: {error.message}") from error
    return {
        "project_id": project_id,
        "variant_id": destination.variant.id,
        "job_id": destination.job.id,
        "state": str(destination.job.job_state),
        "validation": dict(destination.variant.validation_report_json or {}),
        "next_step": "A human must request and grant approval; nothing is uploaded until they do.",
    }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "project.metadata.read",
        "Read project metadata",
        "Read a content project's title, brief, brand and source asset summary.",
        "read",
        ProjectRef,
        read_project_metadata,
    ),
    ToolSpec(
        "content.metadata.read",
        "Read content metadata",
        "Read a project or one platform variant's caption, hashtags and approval state.",
        "read",
        ContentRef,
        read_content_metadata,
    ),
    ToolSpec(
        "content.variants.list",
        "List platform variants",
        "List every platform variant of a project with its technical metadata.",
        "read",
        ProjectRef,
        list_content_variants,
    ),
    ToolSpec(
        "media.inspect",
        "Inspect media",
        "Verify a media asset's stored properties against a live ffprobe read of the file.",
        "compute",
        AssetRef,
        inspect_media,
        timeout_s=15.0,
        risk=ToolRisk.COMPUTE,
    ),
    ToolSpec(
        "media.derivatives.list",
        "List media derivatives",
        "List thumbnails, previews, proxies, transcodes, audio and waveforms for an asset, verifying each file on disk.",
        "read",
        AssetRef,
        list_media_derivatives,
    ),
    ToolSpec(
        "media.scene.scan",
        "Scan scene changes",
        "Measure scene-change timestamps in a video with FFmpeg's scene score, within a bounded budget.",
        "compute",
        SceneScanInput,
        scan_media_scenes,
        timeout_s=30.0,
        risk=ToolRisk.COMPUTE,
    ),
    ToolSpec(
        "media.transcode.recommend",
        "Recommend transcode profile",
        "Suggest a transcode profile from probed facts and state the reason. It never encodes.",
        "compute",
        AssetRef,
        recommend_media_transcode,
        risk=ToolRisk.COMPUTE,
    ),
    ToolSpec(
        "media.quality.report",
        "Media quality report",
        "Check an asset against a platform's published technical limits with deterministic rules.",
        "compute",
        QualityInput,
        media_quality_report,
        risk=ToolRisk.COMPUTE,
    ),
    ToolSpec(
        "transcript.read",
        "Read transcript",
        "Read a stored transcript and its segment count.",
        "read",
        TranscriptRef,
        read_transcript,
    ),
    ToolSpec(
        "brand.rules.read",
        "Read brand rules",
        "Read the active brand profile and stored brand rules.",
        "read",
        BrandRef,
        read_brand_rules,
    ),
    ToolSpec(
        "internal.search",
        "Search local content",
        "Search projects, variants, transcripts and agent memory inside the local database.",
        "read",
        SearchInput,
        internal_search,
    ),
    ToolSpec(
        "caption.compose",
        "Compose caption",
        "Deterministically assemble a caption from hook, body, CTA and hashtags and check platform limits.",
        "compute",
        CaptionInput,
        compose_caption,
        risk=ToolRisk.COMPUTE,
    ),
    ToolSpec(
        "content.readiness.check",
        "Check publish readiness",
        "Run the deterministic pre-publish checklist for a project and platform set.",
        "compute",
        ReadinessInput,
        check_readiness,
        risk=ToolRisk.COMPUTE,
    ),
    ToolSpec(
        "publish.plan.generate",
        "Generate publish plan",
        "Create draft publish jobs for connected accounts. Nothing is uploaded.",
        "write",
        PlanInput,
        generate_publish_plan,
        risk=ToolRisk.WRITE,
        approval=ApprovalRequirement.ON_WRITE,
    ),
    ToolSpec(
        "report.generate",
        "Generate report",
        "Write a Markdown report under the data root and return its path and hash.",
        "write",
        ReportInput,
        generate_report,
        risk=ToolRisk.WRITE,
    ),
    ToolSpec(
        "social.accounts.list",
        "List connected social accounts",
        "List social accounts with their connection status, token state and stored capability states. Read-only; no credential is returned.",
        "read",
        SocialAccountsInput,
        social_accounts_list,
    ),
    ToolSpec(
        "social.capabilities.read",
        "Read account capabilities",
        "Read the stored capability verdicts, required and missing scopes and the reason for each, for one account or a platform.",
        "read",
        SocialCapabilitiesInput,
        social_capabilities_read,
    ),
    ToolSpec(
        "publishing.queue.read",
        "Read the publishing queue",
        "List publish destinations with their state, the reason for that state and the schedule. Read-only; no credential, URL or path is returned.",
        "read",
        PublishQueueInput,
        publishing_queue_read,
    ),
    ToolSpec(
        "publishing.destination.read",
        "Read one publish destination",
        "Read a destination's approval, attempt, delivery and reconciliation history. Read-only.",
        "read",
        PublishDestinationInput,
        publishing_destination_read,
    ),
    ToolSpec(
        "publishing.draft.create",
        "Draft a publish destination",
        "Create an intent and one draft destination for a connected account. It cannot approve or publish anything, and says so in its result.",
        "write",
        PublishDraftInput,
        publishing_draft_create,
        risk=ToolRisk.WRITE,
        approval=ApprovalRequirement.ON_WRITE,
    ),
    ToolSpec(
        "analytics.metrics.read",
        "Read observed metrics",
        "Read aggregated metrics for an account, publication or campaign at one performance window, with each metric's availability and the ids behind it. An unavailable metric is reported as unavailable, never as zero.",
        "read",
        AnalyticsMetricsReadInput,
        analytics_metrics_read,
    ),
    ToolSpec(
        "analytics.publications.read",
        "Read measured publications",
        "List published objects with their provider id, publish time, campaign and format, and whether any metrics have been collected for them.",
        "read",
        AnalyticsPublicationsReadInput,
        analytics_publications_read,
    ),
    ToolSpec(
        "analytics.comparison.read",
        "Compare performance groups",
        "Compare one metric at one window across up to five groups, returning medians, intervals, sample sizes and explicit refusals. Never ranks groups whose intervals overlap.",
        "compute",
        AnalyticsComparisonReadInput,
        analytics_comparison_read,
        timeout_s=30.0,
        risk=ToolRisk.COMPUTE,
        tags=("analytics", "statistics"),
    ),
    ToolSpec(
        "analytics.recommendations.read",
        "Read recommendations",
        "Read stored recommendations with their evidence, sample size, intervals and limitations.",
        "read",
        AnalyticsRecommendationsReadInput,
        analytics_recommendations_read,
    ),
    ToolSpec(
        "analytics.recommendation.propose",
        "Propose a recommendation",
        "Store a draft recommendation. Every number in the observation must already exist in the cited evidence; it cannot approve, schedule or publish.",
        "write",
        AnalyticsRecommendationProposeInput,
        analytics_recommendation_propose,
        risk=ToolRisk.WRITE,
        approval=ApprovalRequirement.ON_WRITE,
        tags=("analytics",),
    ),
    ToolSpec(
        "human.approval.request",
        "Request human approval",
        "Record a hash-bound approval request. It never performs the action itself.",
        "write",
        ApprovalInput,
        request_human_approval,
        risk=ToolRisk.WRITE,
        approval=ApprovalRequirement.ALWAYS_HUMAN,
    ),
)

TOOLS_BY_ID: dict[str, ToolSpec] = {spec.id: spec for spec in TOOL_SPECS}


def ensure_tools_seeded(session: Session) -> dict[str, int]:
    """Mirror the declared tool surface into the database. Idempotent."""
    created = 0
    updated = 0
    for spec in TOOL_SPECS:
        row = session.get(ToolRegistryRow, spec.id)
        schema = spec.input_model.model_json_schema()
        if row is None:
            session.add(
                ToolRegistryRow(
                    name=spec.id,
                    risk=spec.risk,
                    approval_requirement=spec.approval,
                    schema_json=schema,
                    allowed_roles_json=["agent"],
                    description=f"{spec.name}. {spec.description}",
                    enabled=True,
                )
            )
            created += 1
            continue
        if row.schema_json != schema or row.description != f"{spec.name}. {spec.description}":
            row.schema_json = schema
            row.description = f"{spec.name}. {spec.description}"
            updated += 1
    session.commit()
    return {"created": created, "updated": updated, "total": len(TOOL_SPECS)}


@dataclass(slots=True)
class ToolOutcome:
    tool_id: str
    ok: bool
    output: dict[str, Any]
    duration_ms: int
    error_code: str | None = None
    error: str | None = None


class ToolExecutor:
    """Authorize, validate, execute and audit one tool call."""

    def __init__(self, session: Session, *, enforce_grant: bool = True) -> None:
        self.session = session
        self.enforce_grant = enforce_grant
        self.audit = AuditService(session)

    def _authorize(self, spec: ToolSpec, agent_id: str) -> None:
        row = self.session.get(ToolRegistryRow, spec.id)
        if row is None:
            raise ToolNotFound(f"tool {spec.id} is not registered")
        if not row.enabled:
            raise ToolDenied(f"tool {spec.id} is disabled")
        if not self.enforce_grant:
            return
        from .registry import AgentRegistry

        try:
            AgentRegistry(self.session).assert_tool_permitted(agent_id, spec.id)
        except PermissionError as exc:
            raise ToolDenied(str(exc)) from exc
        except KeyError as exc:
            raise ToolDenied(f"unknown agent {agent_id}") from exc

    def execute(
        self,
        tool_id: str,
        *,
        agent_id: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        objective_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ToolOutcome:
        started = time.perf_counter()
        spec = TOOLS_BY_ID.get(tool_id)
        if spec is None:
            raise ToolNotFound(f"unknown tool {tool_id}")
        try:
            self._authorize(spec, agent_id)
            validated = spec.input_model.model_validate(payload or {})
        except (ToolError, PermissionError):
            raise
        except Exception as exc:
            raise ToolInvalidInput(f"invalid input for {tool_id}: {exc}") from exc

        ctx = ToolContext(
            session=self.session,
            agent_id=agent_id,
            task_id=task_id,
            objective_id=objective_id,
            correlation_id=correlation_id,
        )
        try:
            output = _run_with_timeout(spec, ctx, validated)
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.audit.record(
                "agent.tool.call",
                resource_type="tool",
                resource_id=tool_id,
                outcome="success",
                actor=agent_id,
                metadata={
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "objective_id": objective_id,
                    "duration_ms": duration_ms,
                    "category": spec.category,
                    "output_keys": sorted(output.keys())[:12],
                },
                correlation_id=correlation_id,
            )
            return ToolOutcome(tool_id=tool_id, ok=True, output=output, duration_ms=duration_ms)
        except ToolError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.audit.record(
                "agent.tool.call",
                resource_type="tool",
                resource_id=tool_id,
                outcome="failure",
                actor=agent_id,
                metadata={
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "duration_ms": duration_ms,
                    "error_code": exc.code,
                    "error": str(exc)[:300],
                },
                correlation_id=correlation_id,
            )
            return ToolOutcome(
                tool_id=tool_id,
                ok=False,
                output={},
                duration_ms=duration_ms,
                error_code=exc.code,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - unexpected handler fault: never hide it
            duration_ms = int((time.perf_counter() - started) * 1000)
            log.warning("tool %s failed unexpectedly: %s", tool_id, exc)
            self.audit.record(
                "agent.tool.call",
                resource_type="tool",
                resource_id=tool_id,
                outcome="error",
                actor=agent_id,
                metadata={"agent_id": agent_id, "duration_ms": duration_ms, "error": str(exc)[:300]},
                correlation_id=correlation_id,
            )
            return ToolOutcome(
                tool_id=tool_id,
                ok=False,
                output={},
                duration_ms=duration_ms,
                error_code="TOOL_FAILED",
                error=str(exc),
            )

    def catalog(self) -> list[dict[str, Any]]:
        rows = {
            row.name: row
            for row in self.session.scalars(sa.select(ToolRegistryRow))
        }
        surface: list[dict[str, Any]] = []
        for spec in TOOL_SPECS:
            row = rows.get(spec.id)
            surface.append(
                {
                    "tool_id": spec.id,
                    "name": spec.name,
                    "description": spec.description,
                    "category": spec.category,
                    "risk": str(spec.risk),
                    "approval_requirement": str(spec.approval),
                    "timeout_s": spec.timeout_s,
                    "enabled": bool(row.enabled) if row else False,
                    "registered": row is not None,
                    "input_schema": spec.input_model.model_json_schema(),
                }
            )
        return surface


def _run_with_timeout(spec: ToolSpec, ctx: ToolContext, payload: BaseModel) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _call_with_deadline(spec, ctx, payload)
    # Called from async context: run the sync handler in a short-lived helper thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_call_with_deadline, spec, ctx, payload).result(timeout=spec.timeout_s + 1)


def _call_with_deadline(spec: ToolSpec, ctx: ToolContext, payload: BaseModel) -> dict[str, Any]:
    import threading

    result: dict[str, Any] = {}
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.update(spec.handler(ctx, payload))
        except BaseException as exc:  # noqa: BLE001 - propagate to the caller thread
            failure.append(exc)

    worker = threading.Thread(target=target, name=f"tool-{spec.id}", daemon=True)
    worker.start()
    worker.join(spec.timeout_s)
    if worker.is_alive():
        raise ToolTimeout(f"{spec.id} exceeded its {spec.timeout_s:.0f}s budget")
    if failure:
        raise failure[0]
    return result


# --------------------------------------------------------------------- misc
def tool_summary() -> dict[str, Any]:
    return {
        "count": len(TOOL_SPECS),
        "by_category": {
            category: sum(1 for spec in TOOL_SPECS if spec.category == category)
            for category in ("read", "compute", "write")
        },
        "requires_approval": [spec.id for spec in TOOL_SPECS if spec.approval != ApprovalRequirement.NONE],
    }


def memory_scopes() -> list[str]:
    return [str(scope) for scope in MemoryScope]


__all__ = [
    "TOOLS_BY_ID",
    "TOOL_SPECS",
    "ToolContext",
    "ToolDenied",
    "ToolError",
    "ToolExecutor",
    "ToolInvalidInput",
    "ToolNotFound",
    "ToolOutcome",
    "ToolSpec",
    "ToolTimeout",
    "ToolUnavailable",
    "ensure_tools_seeded",
    "tool_summary",
]

_ = ()
