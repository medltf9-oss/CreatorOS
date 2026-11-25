"""Content-side tables: settings, brand, campaigns, media, transcripts, projects, variants."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, IdMixin, TimestampMixin, UtcDateTime, enum_column
from ..enums import (
    ApprovalStatus,
    ArchiveState,
    DerivativeKind,
    DerivativeState,
    IntakeState,
    MediaKind,
    Platform,
    ProjectStatus,
    RightsStatus,
)


class AppSetting(Base, TimestampMixin):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(sa.String(120), primary_key=True)
    value_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    scope: Mapped[str] = mapped_column(sa.String(32), default="app", nullable=False)


class BrandProfile(Base, IdMixin, TimestampMixin):
    __tablename__ = "brand_profile"

    name: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    voice_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    banned_terms_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    cta_phrases_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    hashtag_sets_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    subtitle_style_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    is_default: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)


class Campaign(Base, IdMixin, TimestampMixin):
    __tablename__ = "campaign"

    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    goal: Mapped[str | None] = mapped_column(sa.Text)
    audience_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    pillars_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    starts_on: Mapped[datetime | None] = mapped_column(UtcDateTime)
    ends_on: Mapped[datetime | None] = mapped_column(UtcDateTime)

    projects: Mapped[list[ContentProject]] = relationship(back_populates="campaign")


class MediaAsset(Base, IdMixin, TimestampMixin):
    __tablename__ = "media_asset"
    __table_args__ = (
        sa.UniqueConstraint("sha256", name="uq_media_asset_sha256"),
        sa.Index("ix_media_asset_created", "created_at"),
    )

    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    original_filename: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    fps: Mapped[float | None] = mapped_column(sa.Float)
    video_codec: Mapped[str | None] = mapped_column(sa.String(64))
    audio_codec: Mapped[str | None] = mapped_column(sa.String(64))
    rotation: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    bitrate: Mapped[int | None] = mapped_column(sa.BigInteger)
    has_audio: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    loudness_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    thumbnail_path: Mapped[str | None] = mapped_column(sa.String(1024))
    rights_status: Mapped[RightsStatus] = mapped_column(
        enum_column(RightsStatus, "rights_status"), default=RightsStatus.UNCERTAIN, nullable=False
    )
    rights_notes: Mapped[str | None] = mapped_column(sa.Text)
    tags_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    archive_state: Mapped[ArchiveState] = mapped_column(
        enum_column(ArchiveState, "archive_state"), default=ArchiveState.ACTIVE, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    # --- Phase 5: media intake -------------------------------------------------
    # `source_path` above remains the canonical absolute path of the stored original;
    # these columns record what the file actually is, decided by ffprobe.
    kind: Mapped[MediaKind] = mapped_column(
        enum_column(MediaKind, "media_kind"), default=MediaKind.UNKNOWN, nullable=False
    )
    intake_state: Mapped[IntakeState] = mapped_column(
        enum_column(IntakeState, "intake_state"), default=IntakeState.PENDING, nullable=False
    )
    mime_type: Mapped[str | None] = mapped_column(sa.String(120))
    format_name: Mapped[str | None] = mapped_column(sa.String(120))
    probe_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    probe_version: Mapped[str | None] = mapped_column(sa.String(20))
    last_processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    quarantine_reason: Mapped[str | None] = mapped_column(sa.Text)

    transcripts: Mapped[list[Transcript]] = relationship(back_populates="asset")
    projects: Mapped[list[ContentProject]] = relationship(back_populates="source_asset")
    derivatives: Mapped[list[MediaDerivative]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class MediaDerivative(Base, IdMixin, TimestampMixin):
    """One generated file derived from an asset (thumbnail, preview, proxy, audio...).

    The unique key is ``(asset_id, kind, profile, version)``: asking twice for the same
    output is a cache hit, never a second file. ``version`` is the processing-profile
    version, so changing an FFmpeg recipe invalidates old outputs instead of silently
    mixing recipes.
    """

    __tablename__ = "media_derivative"
    __table_args__ = (
        sa.UniqueConstraint("asset_id", "kind", "profile", "version", name="uq_derivative_key"),
        sa.Index("ix_media_derivative_asset", "asset_id", "kind"),
    )

    asset_id: Mapped[str] = mapped_column(
        sa.ForeignKey("media_asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[DerivativeKind] = mapped_column(
        enum_column(DerivativeKind, "derivative_kind"), nullable=False
    )
    profile: Mapped[str] = mapped_column(sa.String(60), default="default", nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    state: Mapped[DerivativeState] = mapped_column(
        enum_column(DerivativeState, "derivative_state"), default=DerivativeState.QUEUED, nullable=False
    )
    rel_path: Mapped[str | None] = mapped_column(sa.String(1024))
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, default=0, nullable=False)
    sha256: Mapped[str | None] = mapped_column(sa.String(64))
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    duration_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    mime_type: Mapped[str | None] = mapped_column(sa.String(120))
    job_id: Mapped[str | None] = mapped_column(sa.String(26), index=True)
    error_code: Mapped[str | None] = mapped_column(sa.String(60))
    error_detail: Mapped[str | None] = mapped_column(sa.Text)
    meta_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)

    asset: Mapped[MediaAsset] = relationship(back_populates="derivatives")


class Transcript(Base, IdMixin, TimestampMixin):
    __tablename__ = "transcript"

    asset_id: Mapped[str] = mapped_column(sa.ForeignKey("media_asset.id", ondelete="CASCADE"), nullable=False, index=True)
    language: Mapped[str] = mapped_column(sa.String(16), default="en", nullable=False)
    model: Mapped[str | None] = mapped_column(sa.String(120))
    text: Mapped[str] = mapped_column(sa.Text, default="", nullable=False)
    segments_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    srt_path: Mapped[str | None] = mapped_column(sa.String(1024))
    vtt_path: Mapped[str | None] = mapped_column(sa.String(1024))
    edited_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    asset: Mapped[MediaAsset] = relationship(back_populates="transcripts")


class ContentProject(Base, IdMixin, TimestampMixin):
    __tablename__ = "content_project"

    title: Mapped[str] = mapped_column(sa.String(300), nullable=False)
    brief: Mapped[str | None] = mapped_column(sa.Text)
    campaign_id: Mapped[str | None] = mapped_column(sa.ForeignKey("campaign.id", ondelete="SET NULL"), index=True)
    source_asset_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("media_asset.id", ondelete="SET NULL"), index=True
    )
    brand_profile_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("brand_profile.id", ondelete="SET NULL"), index=True
    )
    transcript_id: Mapped[str | None] = mapped_column(sa.ForeignKey("transcript.id", ondelete="SET NULL"))
    status: Mapped[ProjectStatus] = mapped_column(
        enum_column(ProjectStatus, "project_status"), default=ProjectStatus.DRAFT, nullable=False
    )

    campaign: Mapped[Campaign | None] = relationship(back_populates="projects")
    source_asset: Mapped[MediaAsset | None] = relationship(back_populates="projects")
    variants: Mapped[list[PlatformVariant]] = relationship(back_populates="project", cascade="all, delete-orphan")


class PlatformVariant(Base, IdMixin, TimestampMixin):
    __tablename__ = "platform_variant"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "platform", "target_account_id", name="uq_variant_target"),
    )

    project_id: Mapped[str] = mapped_column(
        sa.ForeignKey("content_project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[Platform] = mapped_column(enum_column(Platform, "platform"), nullable=False)
    target_account_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("social_account.id", ondelete="SET NULL"), index=True
    )
    output_path: Mapped[str | None] = mapped_column(sa.String(1024))
    aspect_ratio: Mapped[str] = mapped_column(sa.String(16), default="9:16", nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(sa.BigInteger)
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    caption: Mapped[str] = mapped_column(sa.Text, default="", nullable=False)
    hashtags_json: Mapped[list] = mapped_column(sa.JSON, default=list, nullable=False)
    cta: Mapped[str | None] = mapped_column(sa.String(500))
    cover_path: Mapped[str | None] = mapped_column(sa.String(1024))
    privacy_setting: Mapped[str] = mapped_column(sa.String(64), default="public", nullable=False)
    validation_report_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus, "approval_status"), default=ApprovalStatus.DRAFT, nullable=False
    )
    # --- Phase 7: the composer's per-destination content --------------------------
    title: Mapped[str | None] = mapped_column(sa.String(500))
    metadata_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    options_json: Mapped[dict] = mapped_column(sa.JSON, default=dict, nullable=False)
    # Which derivative this variant was validated against; the original is never touched.
    source_derivative_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("media_derivative.id", ondelete="SET NULL")
    )

    project: Mapped[ContentProject] = relationship(back_populates="variants")
