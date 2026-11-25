"""The composer: campaigns, publishing intents and destinations.

Vocabulary, because the spec and this codebase use different words for the same things (D1/D2):

* **campaign** — a grouping the user creates for their own reasons. `campaign` table.
* **intent** — "publish this piece of content". One intent is one `content_project`. Publishing
  the same video again on purpose is a *new* intent, which is why republication duplicates the
  project instead of resetting the old one.
* **destination** — "this intent, on this account". One `platform_variant` plus exactly one
  `publish_job`, which is the ledger the queue and the history read (the spec calls that table
  `publishing_jobs`; D2 keeps the Phase 2 name).

Three rules this service enforces so no caller has to remember them:

1. A destination is only created for an account that can actually publish on that surface, and the
   refusal carries the capability state, the reason and the missing scopes.
2. Editing a destination after it was approved invalidates that approval. The hash lives on the
   job, so the composer clears it and puts the destination back to `draft`; an approval for text
   that no longer exists can never be spent.
3. Nothing here talks to a provider. This service never sets a state that means "something happened
   remotely" (`uploading` and beyond); that is the executor's job and only the executor's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...core.logging import get_logger
from ...db.enums import (
    CapabilityState,
    DerivativeKind,
    DerivativeState,
    Platform,
    ProjectStatus,
    PublishMode,
    PublishState,
    can_transition,
)
from ...db.models import (
    AccountCapability,
    Campaign,
    ContentProject,
    MediaAsset,
    MediaDerivative,
    PlatformVariant,
    PublishJob,
    SocialAccount,
)
from ...integrations.social.publishing import ValidationReport, rules_for, validate_variant
from ...integrations.social.publishing.metadata.limits import mode_for_platform
from ...integrations.social.publishing.metadata.validators import normalize_hashtags
from ...services.backup import AuditService
from .errors import PublishingError

log = get_logger(__name__)

#: Which capability proves an account can publish a given surface (ADR-24 vocabulary).
PUBLISH_CAPABILITY: Mapping[tuple[str, PublishMode], str] = {
    ("facebook", PublishMode.VIDEO): "facebook.page.publish_video",
    ("instagram", PublishMode.REELS): "instagram.publish_reels",
    ("tiktok", PublishMode.DIRECT_POST): "tiktok.direct_post",
    ("tiktok", PublishMode.INBOX): "tiktok.upload_draft",
}

#: States in which a destination may still be edited. `ready` is deliberately absent: the publish
#: worker may already have claimed the job, so editing it would race the upload.
EDITABLE_JOB_STATES: frozenset[PublishState] = frozenset(
    {
        PublishState.DRAFT,
        PublishState.PREPARING,
        PublishState.VALIDATION_PENDING,
        PublishState.AWAITING_APPROVAL,
        PublishState.APPROVED,
        PublishState.SCHEDULED,
        PublishState.BLOCKED,
        PublishState.REQUIRES_USER_ACTION,
    }
)

#: Editing any of these changes what the provider would receive, so the approval hash no longer
#: describes the destination and must be thrown away.
CONTENT_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "caption",
        "hashtags_json",
        "options_json",
        "privacy_setting",
        "scheduled_at_utc",
        "display_timezone",
        "target_account_id",
        "source_asset_id",
        "source_derivative_id",
    }
)

#: Derivative profiles this service will accept as a publishing source, best first. `normalized`
#: is the Phase 5 profile; the two `publish_*` profiles exist for files the platform rejects.
PUBLISH_PROFILES: tuple[str, ...] = ("publish_reels_1080", "publish_tiktok_1080", "normalized")

FIELD_SOURCE_PREFIXES = ("human", "template", "imported", "agent:")


@dataclass(frozen=True, slots=True)
class SourceMedia:
    """The bytes a destination would send, and whether they need re-encoding first."""

    asset_id: str
    sha256: str
    size_bytes: int
    probe: Mapping[str, Any]
    derivative_id: str | None = None
    derivative_profile: str | None = None
    derivative_version: int | None = None
    needs_transcode: bool = False
    suggested_profile: str | None = None
    media_report: ValidationReport | None = None


@dataclass(frozen=True, slots=True)
class Destination:
    """A variant and the job that will carry it to the provider."""

    variant: PlatformVariant
    job: PublishJob


def _tz(name: str | None) -> str:
    """Validate an IANA zone name. Returns the canonical name."""
    candidate = (name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PublishingError(
            "INVALID_TIMEZONE",
            f"{candidate!r} is not an IANA time zone; use a name like Europe/Paris.",
            http_status=422,
            detail={"timezone": candidate},
        ) from exc
    return candidate


def _parse_platform(value: str | Platform) -> Platform:
    try:
        return value if isinstance(value, Platform) else Platform(str(value).lower())
    except ValueError as exc:
        raise PublishingError(
            "UNKNOWN_PLATFORM",
            f"{value!r} is not a publishing platform.",
            http_status=404,
            detail={"platform": str(value)},
        ) from exc


def _field_sources(raw: Mapping[str, str] | None) -> dict[str, str]:
    """Normalise provenance. An unknown source is refused rather than stored as a lie."""
    if not raw:
        return {}
    cleaned: dict[str, str] = {}
    for field, source in raw.items():
        value = str(source).strip()
        if not value.startswith(FIELD_SOURCE_PREFIXES):
            raise PublishingError(
                "INVALID_FIELD_SOURCE",
                (
                    f"{field!r} claims source {value!r}; use human, template, imported or "
                    "agent:<name>."
                ),
                http_status=422,
                detail={"field": str(field), "source": value},
            )
        cleaned[str(field)] = value[:120]
    return cleaned


class ComposerService:
    """Campaign, intent and destination management. One transaction, no provider calls."""

    def __init__(self, session: Session, audit: AuditService | None = None) -> None:
        self.session = session
        self.audit = audit

    # ------------------------------------------------------------------ campaigns
    def create_campaign(
        self,
        *,
        name: str,
        goal: str | None = None,
        audience: Mapping[str, Any] | None = None,
        pillars: Sequence[str] | None = None,
        starts_on: datetime | None = None,
        ends_on: datetime | None = None,
    ) -> Campaign:
        clean_name = (name or "").strip()
        if not clean_name:
            raise PublishingError("VALIDATION_ERROR", "A campaign needs a name.", http_status=422)
        if starts_on and ends_on and ends_on < starts_on:
            raise PublishingError(
                "VALIDATION_ERROR", "The campaign ends before it starts.", http_status=422
            )
        campaign = Campaign(
            name=clean_name[:200],
            goal=goal,
            audience_json=dict(audience or {}),
            pillars_json=[str(item)[:120] for item in (pillars or [])],
            starts_on=starts_on,
            ends_on=ends_on,
        )
        self.session.add(campaign)
        self.session.flush()
        self._record("publish.campaign.create", campaign.id, {"name": campaign.name})
        return campaign

    def update_campaign(self, campaign_id: str, **fields: Any) -> Campaign:
        campaign = self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise PublishingError(
                "CAMPAIGN_NOT_FOUND", "campaign not found", http_status=404, detail={"id": campaign_id}
            )
        allowed = {"name", "goal", "audience_json", "pillars_json", "starts_on", "ends_on"}
        changed = [key for key in fields if key in allowed and fields[key] is not None]
        for key in changed:
            setattr(campaign, key, fields[key])
        if "name" in changed and not str(campaign.name).strip():
            raise PublishingError("VALIDATION_ERROR", "A campaign needs a name.", http_status=422)
        self._record("publish.campaign.update", campaign.id, {"fields": changed})
        return campaign

    # -------------------------------------------------------------------- intents
    def create_intent(
        self,
        *,
        title: str,
        campaign_id: str | None = None,
        source_asset_id: str | None = None,
        brief: str | None = None,
        brand_profile_id: str | None = None,
    ) -> ContentProject:
        clean_title = (title or "").strip()
        if not clean_title:
            raise PublishingError("VALIDATION_ERROR", "An intent needs a title.", http_status=422)
        if campaign_id and self.session.get(Campaign, campaign_id) is None:
            raise PublishingError(
                "CAMPAIGN_NOT_FOUND", "campaign not found", http_status=404, detail={"id": campaign_id}
            )
        if source_asset_id and self.session.get(MediaAsset, source_asset_id) is None:
            raise PublishingError(
                "ASSET_NOT_FOUND",
                "media asset not found",
                http_status=404,
                detail={"id": source_asset_id},
            )
        project = ContentProject(
            title=clean_title[:300],
            brief=brief,
            campaign_id=campaign_id,
            source_asset_id=source_asset_id,
            brand_profile_id=brand_profile_id,
            status=ProjectStatus.DRAFT,
        )
        self.session.add(project)
        self.session.flush()
        self._record("publish.intent.create", project.id, {"campaign_id": campaign_id})
        return project

    def update_intent(self, project_id: str, **fields: Any) -> ContentProject:
        project = self._project(project_id)
        allowed = {"title", "brief", "campaign_id", "source_asset_id", "brand_profile_id", "status"}
        changed = [key for key in fields if key in allowed and fields[key] is not None]
        if "source_asset_id" in changed:
            if self.session.get(MediaAsset, fields["source_asset_id"]) is None:
                raise PublishingError(
                    "ASSET_NOT_FOUND",
                    "media asset not found",
                    http_status=404,
                    detail={"id": fields["source_asset_id"]},
                )
            # Changing the source changes what every destination would send, so every approval on
            # this intent is invalidated rather than silently pointing at different bytes.
            for job in self._jobs_for_project(project_id):
                self._invalidate_approval(job, reason="source_asset_changed")
        for key in changed:
            setattr(project, key, fields[key])
        self._record("publish.intent.update", project.id, {"fields": changed})
        return project

    def delete_intent(self, project_id: str) -> None:
        project = self._project(project_id)
        in_flight = [
            job
            for job in self._jobs_for_project(project_id)
            if job.job_state not in EDITABLE_JOB_STATES and job.job_state not in {
                PublishState.PUBLISHED,
                PublishState.CANCELLED,
            }
        ]
        if in_flight:
            raise PublishingError(
                "INTENT_HAS_IN_FLIGHT_DESTINATION",
                "This intent has a destination that is being published right now.",
                http_status=409,
                detail={"job_ids": [job.id for job in in_flight]},
            )
        published = [
            job for job in self._jobs_for_project(project_id) if job.job_state is PublishState.PUBLISHED
        ]
        if published:
            raise PublishingError(
                "DESTINATION_TERMINAL",
                "This intent is already published; archive it instead of deleting the record.",
                http_status=409,
                detail={"job_ids": [job.id for job in published]},
            )
        self.session.delete(project)
        self.session.flush()
        self._record("publish.intent.delete", project_id, {})

    def duplicate_intent(self, project_id: str, *, title: str | None = None) -> ContentProject:
        """Republish on purpose: a new intent, with the old one left exactly as history."""
        source = self._project(project_id)
        copy = ContentProject(
            title=(title or f"{source.title} (again)")[:300],
            brief=source.brief,
            campaign_id=source.campaign_id,
            source_asset_id=source.source_asset_id,
            brand_profile_id=source.brand_profile_id,
            status=ProjectStatus.DRAFT,
        )
        self.session.add(copy)
        self.session.flush()
        for variant in self._variants_for_project(project_id):
            if variant.target_account_id is None:
                continue
            self.add_destination(
                copy.id,
                platform=str(variant.platform),
                account_id=variant.target_account_id,
                mode=self._mode_for_variant(variant),
                title=variant.title,
                caption=variant.caption,
                hashtags=list(variant.hashtags_json or []),
                options=dict(variant.options_json or {}),
                privacy_setting=variant.privacy_setting,
                field_sources={"caption": "template", "title": "template"},
            )
        self._record("publish.intent.duplicate", copy.id, {"source_project_id": project_id})
        return copy

    # --------------------------------------------------------------- destinations
    def add_destination(
        self,
        project_id: str,
        *,
        platform: str,
        account_id: str,
        mode: PublishMode | str | None = None,
        title: str | None = None,
        caption: str | None = None,
        hashtags: Sequence[str] | None = None,
        options: Mapping[str, Any] | None = None,
        privacy_setting: str | None = None,
        scheduled_at_utc: datetime | None = None,
        display_timezone: str | None = None,
        asset_id: str | None = None,
        field_sources: Mapping[str, str] | None = None,
        creator_info: Mapping[str, Any] | None = None,
    ) -> Destination:
        project = self._project(project_id)
        resolved_platform = _parse_platform(platform)
        resolved_mode = self._mode(platform=resolved_platform, mode=mode)
        account = self._account(account_id)
        self._assert_account_matches(account, resolved_platform, resolved_mode)

        existing = self.session.scalar(
            sa.select(PlatformVariant).where(
                PlatformVariant.project_id == project.id,
                PlatformVariant.platform == resolved_platform,
                PlatformVariant.target_account_id == account.id,
            )
        )
        if existing is not None:
            raise PublishingError(
                "DESTINATION_EXISTS",
                f"This intent already has a {resolved_platform.value} destination for that account.",
                http_status=409,
                detail={"variant_id": existing.id},
            )

        source_asset_id = asset_id or project.source_asset_id
        source = self.select_source(source_asset_id, platform=resolved_platform, mode=resolved_mode)
        clean_hashtags = normalize_hashtags(hashtags)
        timezone = _tz(display_timezone)

        report = validate_variant(
            str(resolved_platform),
            resolved_mode,
            title=title,
            caption=caption,
            hashtags=clean_hashtags,
            options=options,
            probe=source.probe if source else None,
            size_bytes=source.size_bytes if source else None,
            creator_info=creator_info,
        )

        variant = PlatformVariant(
            project_id=project.id,
            platform=resolved_platform,
            target_account_id=account.id,
            aspect_ratio=self._aspect_label(source),
            duration_ms=_int_or_none(source.probe.get("duration_ms")) if source else None,
            width=_int_or_none(_video(source).get("display_width") or _video(source).get("width")),
            height=_int_or_none(_video(source).get("display_height") or _video(source).get("height")),
            caption=(caption or "")[:20_000],
            hashtags_json=clean_hashtags,
            privacy_setting=(privacy_setting or variant_privacy(resolved_platform, resolved_mode))[:64],
            validation_report_json=report.as_dict(),
            title=title,
            options_json=dict(options or {}),
            metadata_json={"publish_mode": resolved_mode.value, "platform": str(resolved_platform)},
            source_derivative_id=source.derivative_id if source else None,
        )
        self.session.add(variant)
        try:
            self.session.flush()
        except IntegrityError as exc:  # the unique key is the guard, the pre-check is the message
            self.session.rollback()
            raise PublishingError(
                "DESTINATION_EXISTS",
                "This intent already has a destination for that account.",
                http_status=409,
            ) from exc

        job = PublishJob(
            variant_id=variant.id,
            platform=str(resolved_platform),
            account_id=account.id,
            scheduled_at_utc=scheduled_at_utc,
            display_timezone=timezone,
            job_state=PublishState.DRAFT,
            attempts=0,
            idempotency_key=self.idempotency_key(project.id, resolved_platform, account.id),
            intent_key=project.id,
            asset_id=source.asset_id if source else None,
            asset_sha256=source.sha256 if source else None,
            derivative_id=source.derivative_id if source else None,
            derivative_sha256=source.sha256 if source and source.derivative_id else None,
            derivative_profile=source.derivative_profile if source else None,
            derivative_version=source.derivative_version if source else None,
            publish_mode=resolved_mode,
            title=title,
            caption=variant.caption,
            hashtags_json=clean_hashtags,
            metadata_json=dict(variant.metadata_json),
            options_json=dict(options or {}),
            field_provenance_json=_field_sources(field_sources),
            remote_status_json={},
        )
        self.session.add(job)
        self.session.flush()
        self._record(
            "publish.destination.create",
            job.id,
            {
                "project_id": project.id,
                "platform": str(resolved_platform),
                "account_id": account.id,
                "mode": resolved_mode.value,
                "validation": report.outcome.value,
            },
        )
        return Destination(variant=variant, job=job)

    def update_destination(
        self,
        variant_id: str,
        *,
        fields: Mapping[str, Any],
        field_sources: Mapping[str, str] | None = None,
    ) -> Destination:
        variant, job = self._destination(variant_id)
        if job.job_state not in EDITABLE_JOB_STATES:
            raise PublishingError(
                "DESTINATION_LOCKED",
                "This destination can no longer be edited; its remote state is in play.",
                http_status=409,
                detail={"state": str(job.job_state)},
            )

        allowed = {
            "title",
            "caption",
            "hashtags",
            "options",
            "privacy_setting",
            "scheduled_at_utc",
            "display_timezone",
            "target_account_id",
        }
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise PublishingError(
                "UNKNOWN_FIELD",
                f"{', '.join(unknown)} cannot be edited here.",
                http_status=422,
                detail={"fields": unknown},
            )

        previous: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "hashtags":
                value = normalize_hashtags(value)
                previous[key] = list(variant.hashtags_json or [])
                variant.hashtags_json = value
            elif key == "options":
                previous[key] = dict(variant.options_json or {})
                variant.options_json = dict(value or {})
            elif key == "scheduled_at_utc":
                previous[key] = job.scheduled_at_utc
                job.scheduled_at_utc = value
            elif key == "display_timezone":
                previous[key] = job.display_timezone
                job.display_timezone = _tz(value)
            elif key == "target_account_id":
                previous[key] = variant.target_account_id
                self._retarget(variant, job, str(value))
            elif key == "title":
                previous[key] = variant.title
                variant.title = (str(value).strip() or None)[:500] if value else None
                job.title = variant.title
            elif key == "caption":
                previous[key] = variant.caption
                variant.caption = str(value or "")[:20_000]
                job.caption = variant.caption
            else:  # privacy_setting
                previous[key] = variant.privacy_setting
                variant.privacy_setting = str(value)[:64]

        if field_sources:
            merged = {**dict(job.field_provenance_json or {}), **_field_sources(field_sources)}
            job.field_provenance_json = merged

        # Anything that changes the payload invalidates any approval bound to the old payload.
        if set(fields) & CONTENT_FIELDS:
            self._invalidate_approval(job, reason="destination_edited")

        report = self.revalidate(variant.id)
        job.metadata_json = dict(variant.metadata_json or {})
        job.hashtags_json = list(variant.hashtags_json or [])
        job.options_json = dict(variant.options_json or {})

        self._record(
            "publish.destination.update",
            job.id,
            {
                "fields": sorted(fields),
                "validation": report.outcome.value,
                "previous": {key: _jsonable(value) for key, value in previous.items()},
            },
        )
        return Destination(variant=variant, job=job)

    def delete_destination(self, variant_id: str) -> None:
        variant, job = self._destination(variant_id)
        if job.job_state not in EDITABLE_JOB_STATES:
            raise PublishingError(
                "DESTINATION_LOCKED",
                "This destination cannot be deleted; a remote publication may exist for it.",
                http_status=409,
                detail={"state": str(job.job_state)},
            )
        project_id = variant.project_id
        self.session.delete(variant)
        self.session.flush()
        self._record("publish.destination.delete", job.id, {"project_id": project_id})

    def revalidate(
        self,
        variant_id: str,
        *,
        creator_info: Mapping[str, Any] | None = None,
    ) -> ValidationReport:
        variant, job = self._destination(variant_id)
        mode = self._mode_for_variant(variant)
        source = self.select_source(job.asset_id, platform=variant.platform, mode=mode)
        report = validate_variant(
            str(variant.platform),
            mode,
            title=variant.title,
            caption=variant.caption,
            hashtags=list(variant.hashtags_json or []),
            options=dict(variant.options_json or {}),
            probe=source.probe if source else None,
            size_bytes=source.size_bytes if source else None,
            creator_info=creator_info,
        )
        variant.validation_report_json = report.as_dict()
        return report

    # ---------------------------------------------------------------- source media
    def select_source(
        self,
        asset_id: str | None,
        *,
        platform: Platform,
        mode: PublishMode,
    ) -> SourceMedia | None:
        """Pick the bytes to send: the original when the platform accepts it, else a transcode.

        Never re-encodes a file the platform already accepts - that is the difference between a
        publishing engine and a re-compression loop. When nothing usable exists, the result says
        which profile to generate instead of pretending the destination is ready.
        """
        if not asset_id:
            return None
        asset = self.session.get(MediaAsset, asset_id)
        if asset is None:
            raise PublishingError(
                "ASSET_NOT_FOUND", "media asset not found", http_status=404, detail={"id": asset_id}
            )
        probe = _asset_probe(asset)
        original_report = validate_variant(
            str(platform), mode, caption=None, probe=probe, size_bytes=asset.size_bytes
        )
        if not original_report.blocking and probe.get("video"):
            return SourceMedia(
                asset_id=asset.id,
                sha256=asset.sha256,
                size_bytes=int(asset.size_bytes or 0),
                probe=probe,
                media_report=original_report,
            )

        for derivative in self._candidate_derivatives(asset.id):
            derivative_probe = _derivative_probe(asset, derivative)
            report = validate_variant(
                str(platform), mode, caption=None, probe=derivative_probe, size_bytes=derivative.size_bytes
            )
            if report.blocking:
                continue
            return SourceMedia(
                asset_id=asset.id,
                sha256=derivative.sha256 or asset.sha256,
                size_bytes=int(derivative.size_bytes or 0),
                probe=derivative_probe,
                derivative_id=derivative.id,
                derivative_profile=derivative.profile,
                derivative_version=derivative.version,
                media_report=report,
            )

        return SourceMedia(
            asset_id=asset.id,
            sha256=asset.sha256,
            size_bytes=int(asset.size_bytes or 0),
            probe=probe,
            needs_transcode=True,
            suggested_profile="publish_reels_1080" if platform is Platform.INSTAGRAM else "publish_tiktok_1080",
            media_report=original_report,
        )

    # ------------------------------------------------------------------- helpers
    def idempotency_key(self, project_id: str, platform: Platform, account_id: str) -> str:
        """Deterministic: the same intent, surface and account can never produce two jobs."""
        key = f"{project_id}:{platform.value}:{account_id}"
        return key[:64]

    def _candidate_derivatives(self, asset_id: str) -> list[MediaDerivative]:
        rows = list(
            self.session.scalars(
                sa.select(MediaDerivative).where(
                    MediaDerivative.asset_id == asset_id,
                    MediaDerivative.kind == DerivativeKind.TRANSCODE,
                    MediaDerivative.state == DerivativeState.READY,
                )
            )
        )
        return sorted(rows, key=lambda item: _profile_rank(item.profile))

    def _project(self, project_id: str) -> ContentProject:
        project = self.session.get(ContentProject, project_id)
        if project is None:
            raise PublishingError(
                "INTENT_NOT_FOUND", "intent not found", http_status=404, detail={"id": project_id}
            )
        return project

    def _account(self, account_id: str) -> SocialAccount:
        account = self.session.get(SocialAccount, account_id)
        if account is None:
            raise PublishingError(
                "ACCOUNT_NOT_FOUND", "social account not found", http_status=404, detail={"id": account_id}
            )
        return account

    def _destination(self, variant_id: str) -> tuple[PlatformVariant, PublishJob]:
        variant = self.session.get(PlatformVariant, variant_id)
        if variant is None:
            raise PublishingError(
                "DESTINATION_NOT_FOUND",
                "destination not found",
                http_status=404,
                detail={"id": variant_id},
            )
        job = self.session.scalar(sa.select(PublishJob).where(PublishJob.variant_id == variant.id))
        if job is None:
            raise PublishingError(
                "DESTINATION_HAS_NO_JOB",
                "this destination has no publishing journal entry; recreate it",
                http_status=409,
                detail={"id": variant_id},
            )
        return variant, job

    def _variants_for_project(self, project_id: str) -> list[PlatformVariant]:
        return list(
            self.session.scalars(
                sa.select(PlatformVariant)
                .where(PlatformVariant.project_id == project_id)
                .order_by(PlatformVariant.platform)
            )
        )

    def _jobs_for_project(self, project_id: str) -> list[PublishJob]:
        return list(
            self.session.scalars(
                sa.select(PublishJob)
                .join(PlatformVariant, PublishJob.variant_id == PlatformVariant.id)
                .where(PlatformVariant.project_id == project_id)
            )
        )

    def _mode(self, *, platform: Platform, mode: PublishMode | str | None) -> PublishMode:
        if mode is None:
            return mode_for_platform(str(platform))
        try:
            resolved = mode if isinstance(mode, PublishMode) else PublishMode(str(mode))
        except ValueError as exc:
            raise PublishingError(
                "UNKNOWN_PUBLISH_MODE",
                f"{mode!r} is not a publishing mode.",
                http_status=422,
                detail={"mode": str(mode)},
            ) from exc
        try:
            rules_for(str(platform), resolved)
        except KeyError as exc:
            raise PublishingError(
                "UNKNOWN_PUBLISH_MODE",
                f"{platform} has no {resolved.value} publishing path.",
                http_status=422,
                detail={"platform": str(platform), "mode": resolved.value},
            ) from exc
        return resolved

    def _mode_for_variant(self, variant: PlatformVariant) -> PublishMode:
        stored = (variant.metadata_json or {}).get("publish_mode")
        return self._mode(platform=variant.platform, mode=stored)

    def _assert_account_matches(
        self, account: SocialAccount, platform: Platform, mode: PublishMode
    ) -> None:
        if account.platform is not platform:
            raise PublishingError(
                "DESTINATION_ACCOUNT_MISMATCH",
                f"That account belongs to {account.platform.value}, not {platform.value}.",
                http_status=422,
                detail={"account_platform": str(account.platform)},
            )
        capability_key = PUBLISH_CAPABILITY[(str(platform), mode)]
        capability = self.session.scalar(
            sa.select(AccountCapability).where(
                AccountCapability.account_id == account.id,
                AccountCapability.capability == capability_key,
            )
        )
        if capability is None:
            raise PublishingError(
                "ACCOUNT_NOT_PUBLISH_CAPABLE",
                f"{account.display_name} has no stored verdict for {capability_key}.",
                http_status=409,
                detail={
                    "capability": capability_key,
                    "state": CapabilityState.UNKNOWN.value,
                    "reason": "capabilities have not been verified for this account yet",
                },
            )
        if capability.state is not CapabilityState.AVAILABLE:
            raise PublishingError(
                "ACCOUNT_NOT_PUBLISH_CAPABLE",
                capability.reason or f"{account.display_name} cannot use {capability_key}.",
                http_status=409,
                detail={
                    "capability": capability_key,
                    "state": str(capability.state),
                    "missing_scopes": list(capability.missing_json or []),
                    "docs_reference": capability.docs_reference,
                },
            )

    def _retarget(self, variant: PlatformVariant, job: PublishJob, account_id: str) -> None:
        account = self._account(account_id)
        mode = self._mode_for_variant(variant)
        self._assert_account_matches(account, variant.platform, mode)
        if account_id == variant.target_account_id:
            return
        variant.target_account_id = account.id
        job.account_id = account.id
        # The key names the account, so a re-targeted destination gets a new key: the old key
        # described a different account and must not be reusable for this one.
        job.idempotency_key = self.idempotency_key(variant.project_id, variant.platform, account.id)

    def _invalidate_approval(self, job: PublishJob, *, reason: str) -> None:
        changed = job.payload_hash is not None or job.approval_request_id is not None
        job.payload_hash = None
        job.approval_request_id = None
        job.approval_payload_hash = None
        job.approval_record_id = None
        job.state_reason = reason[:300]
        if changed and job.job_state in {
            PublishState.AWAITING_APPROVAL,
            PublishState.APPROVED,
            PublishState.SCHEDULED,
        }:
            # Back through validation: the hash no longer describes the payload, so the next
            # approval has to be requested against the new one.
            self._set_state(job, PublishState.VALIDATION_PENDING, reason=reason)

    def _set_state(self, job: PublishJob, state: PublishState, *, reason: str) -> None:
        current = PublishState(job.job_state) if not isinstance(job.job_state, PublishState) else job.job_state
        if not can_transition(current, state) and current is not state:
            raise PublishingError(
                "ILLEGAL_STATE_TRANSITION",
                f"cannot move a destination from {current.value} to {state.value}",
                http_status=409,
                detail={"from": current.value, "to": state.value},
            )
        job.job_state = state
        job.state_reason = reason[:300]

    def _aspect_label(self, source: SourceMedia | None) -> str:
        if not source:
            return "9:16"
        video = source.probe.get("video") or {}
        width = int(video.get("display_width") or video.get("width") or 0)
        height = int(video.get("display_height") or video.get("height") or 0)
        if width and height:
            return f"{width}:{height}"
        return "9:16"

    def _record(self, action: str, resource_id: str, metadata: dict[str, Any]) -> None:
        if self.audit is not None:
            self.audit.record(action, resource_type="publish_job", resource_id=resource_id, metadata=metadata)


def variant_privacy(platform: Platform, mode: PublishMode) -> str:
    """The privacy a destination starts with: explicit for Direct Post, descriptive elsewhere."""
    if mode is PublishMode.INBOX:
        return "inbox"
    if platform is Platform.TIKTOK:
        # Direct Post refuses to guess, so the composer's default is the narrowest real option.
        return "SELF_ONLY"
    return "public"


def _profile_rank(profile: str | None) -> int:
    try:
        return PUBLISH_PROFILES.index(str(profile))
    except ValueError:
        return len(PUBLISH_PROFILES)


def _asset_probe(asset: MediaAsset) -> dict[str, Any]:
    probe = dict(asset.probe_json or {})
    probe.setdefault("kind", str(asset.kind) if asset.kind else None)
    probe.setdefault("size_bytes", asset.size_bytes)
    if not probe.get("duration_ms") and asset.duration_ms:
        probe["duration_ms"] = asset.duration_ms
    if not probe.get("has_audio") and asset.has_audio:
        probe["has_audio"] = True
    return probe


def _derivative_probe(asset: MediaAsset, derivative: MediaDerivative) -> dict[str, Any]:
    """A probe-shaped document built from the derivative's own recorded facts."""
    base = dict(asset.probe_json or {})
    video = dict(base.get("video") or {})
    video.update(
        {
            "codec": "h264",
            "width": derivative.width or video.get("width"),
            "height": derivative.height or video.get("height"),
            "display_width": derivative.width or video.get("display_width"),
            "display_height": derivative.height or video.get("display_height"),
            "pixel_format": video.get("pixel_format") or "yuv420p",
            "rotation": 0,
        }
    )
    audio = dict(base.get("audio") or {})
    if audio:
        audio["codec"] = "aac"
    return {
        "kind": "video",
        "format_name": "mp4",
        "mime_type": derivative.mime_type or "video/mp4",
        "duration_ms": derivative.duration_ms or base.get("duration_ms"),
        "size_bytes": derivative.size_bytes,
        "has_audio": bool(audio),
        "video": video,
        "audio": audio or None,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _video(source: SourceMedia | None) -> Mapping[str, Any]:
    return (source.probe.get("video") or {}) if source else {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
