"""Fixture factories for the publishing tests.

Every factory writes through the real models and the real session, so a test that passes here is
passing against the schema a user has. No factory invents a state the application cannot reach: a
"publish-capable account" is an account row plus the capability row the capability engine would
have written for it.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from creatoros.db.base import utcnow
from creatoros.db.enums import (
    AccountType,
    CapabilityState,
    ConnectionStatus,
    MediaKind,
    Platform,
    TokenState,
)
from creatoros.db.models import AccountCapability, MediaAsset, MediaDerivative, SocialAccount

GIB = 1024**3


def probe_document(
    *,
    width: int = 1080,
    height: int = 1920,
    duration_ms: int = 42_000,
    size_bytes: int = 7_000_000,
    video_codec: str = "h264",
    audio_codec: str | None = "aac",
    pixel_format: str | None = "yuv420p",
    format_name: str = "mov,mp4,m4a,3gp,3g2,mj2",
    rotation: int = 0,
) -> dict[str, Any]:
    """A probe document shaped like `ProbeResult.as_dict()`."""
    return {
        "probe_version": "p5.1",
        "path": "<DATA_ROOT>\\data\\media\\ab\\cd\\0000000000000000000000000000000000000000000000000000000000000000.mp4",
        "size_bytes": size_bytes,
        "kind": "video",
        "format_name": format_name,
        "format_long_name": "QuickTime / MOV",
        "mime_type": "video/mp4",
        "duration_ms": duration_ms,
        "bitrate": 1_400_000,
        "has_audio": audio_codec is not None,
        "rotation": rotation,
        "video": {
            "codec": video_codec,
            "width": width,
            "height": height,
            "display_width": width,
            "display_height": height,
            "pixel_format": pixel_format,
            "fps": 30.0,
            "rotation": rotation,
            "duration_ms": duration_ms,
        },
        "audio": None if audio_codec is None else {"codec": audio_codec, "sample_rate": 44_100, "channels": 2},
    }


def make_asset(
    session: Session,
    *,
    probe: Mapping[str, Any] | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    kind: MediaKind = MediaKind.VIDEO,
) -> MediaAsset:
    document = dict(probe or probe_document())
    # A distinct hash per asset by default: `media_asset.sha256` is unique, and a test that creates
    # two assets would otherwise fail on a constraint instead of testing what it meant to.
    sha256 = sha256 or hashlib.sha256(f"{uuid4()}".encode()).hexdigest()
    # Write a real file inside this run's media root: `MediaStorage.resolve_stored` refuses to
    # return a path outside it, and the executor must hit that guard if a row points elsewhere.
    from creatoros.core.settings import get_settings

    media_root = get_settings().path("media_originals").parent
    stored = media_root / "originals" / sha256[:2] / f"{sha256}.mp4"
    stored.parent.mkdir(parents=True, exist_ok=True)
    if not stored.exists():
        stored.write_bytes(b"v" * int(document.get("size_bytes") or 0))
    asset = MediaAsset(
        sha256=sha256,
        source_path=str(stored),
        original_filename="asset.mp4",
        size_bytes=size_bytes if size_bytes is not None else int(document.get("size_bytes") or 0),
        duration_ms=document.get("duration_ms"),
        width=(document.get("video") or {}).get("width"),
        height=(document.get("video") or {}).get("height"),
        video_codec=(document.get("video") or {}).get("codec"),
        audio_codec=(document.get("audio") or {}).get("codec"),
        has_audio=bool(document.get("has_audio")),
        kind=kind,
        format_name=document.get("format_name"),
        mime_type=document.get("mime_type"),
        probe_json=document,
        probe_version="p5.1",
    )
    session.add(asset)
    session.flush()
    return asset


def make_derivative(
    session: Session,
    asset: MediaAsset,
    *,
    profile: str = "normalized",
    width: int = 608,
    height: int = 1080,
    duration_ms: int = 42_000,
    size_bytes: int = 5_000_000,
    state: str = "ready",
) -> MediaDerivative:
    from creatoros.db.enums import DerivativeKind, DerivativeState

    derivative = MediaDerivative(
        asset_id=asset.id,
        kind=DerivativeKind.TRANSCODE,
        profile=profile,
        version=1,
        state=DerivativeState(state),
        rel_path=f"transcodes/{asset.id}/{profile}.mp4",
        size_bytes=size_bytes,
        sha256="b" * 64,
        width=width,
        height=height,
        duration_ms=duration_ms,
        mime_type="video/mp4",
    )
    session.add(derivative)
    session.flush()
    return derivative


def make_account(
    session: Session,
    *,
    platform: Platform = Platform.FACEBOOK,
    account_type: AccountType | None = None,
    display_name: str | None = None,
    provider_account_id: str | None = None,
    capability: str | None = None,
    capability_state: CapabilityState = CapabilityState.AVAILABLE,
    missing_scopes: list[str] | None = None,
) -> SocialAccount:
    from creatoros.db.enums import PublishMode
    from creatoros.services.publishing.composer import PUBLISH_CAPABILITY

    platform = platform if isinstance(platform, Platform) else Platform(str(platform))
    resolved_type = account_type or {
        Platform.FACEBOOK: AccountType.FACEBOOK_PAGE,
        Platform.INSTAGRAM: AccountType.INSTAGRAM_BUSINESS,
        Platform.TIKTOK: AccountType.TIKTOK_CREATOR,
    }[platform]
    account = SocialAccount(
        platform=platform,
        # Unique per account by default: `(platform, provider_account_id)` is unique in the schema.
        provider_account_id=provider_account_id or f"{platform.value}-{uuid4().hex[:12]}",
        display_name=display_name or f"{platform.value.title()} account",
        account_type=resolved_type,
        scopes_json=["pages_manage_posts", "instagram_business_content_publish", "video.publish"],
        capabilities_json={},
        connection_status=ConnectionStatus.CONNECTED,
        token_state=TokenState.VALID,
    )
    session.add(account)
    session.flush()

    default_mode = {
        Platform.FACEBOOK: PublishMode.VIDEO,
        Platform.INSTAGRAM: PublishMode.REELS,
        Platform.TIKTOK: PublishMode.DIRECT_POST,
    }[platform]
    key = capability or PUBLISH_CAPABILITY[(platform.value, default_mode)]
    session.add(
        AccountCapability(
            account_id=account.id,
            capability=key,
            state=capability_state,
            reason=None if capability_state is CapabilityState.AVAILABLE else "not allowed for this app",
            required_json=["pages_manage_posts"],
            granted_json=[] if missing_scopes else ["pages_manage_posts"],
            missing_json=list(missing_scopes or []),
            verified_at=datetime.now(UTC),
            docs_reference="https://developers.facebook.com/docs/video-api/guides/publishing",
        )
    )
    session.flush()
    return account


def bind_token(
    session: Session,
    account: SocialAccount,
    vault,
    *,
    value: str = "test-access-token",
    expires_in_days: int = 30,
) -> str:
    """Give an account a usable access token: a vault record plus the reference the token service reads.

    Without this an account is 'connected' but has no credential, and every publish fails with
    REAUTHORIZATION_REQUIRED - which is the honest outcome, just not what most tests are about.
    """
    import asyncio
    from datetime import timedelta

    from creatoros.core.ids import new_ulid
    from creatoros.db.enums import SecretStatus
    from creatoros.db.models import SecretRef

    secret_id = new_ulid()
    asyncio.run(
        vault.put(
            secret_id=secret_id,
            value=value,
            purpose="access_token",
            provider=account.platform.value,
            alias=f"token:{account.id}",
        )
    )
    session.add(
        SecretRef(
            id=secret_id,
            provider=account.platform.value,
            key_alias=f"token:{account.id}"[:160],
            fingerprint=hashlib.sha256(value.encode()).hexdigest()[:64],
            cipher="dpapi",
            dpapi_scope="user",
            purpose="access_token",
            schema_version=1,
            status=SecretStatus.ACTIVE,
            byte_size=len(value),
        )
    )
    account.token_ref_id = secret_id
    account.token_state = TokenState.VALID
    account.token_expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)
    session.flush()
    return secret_id


def make_composer(session: Session):
    from creatoros.services.backup import AuditService
    from creatoros.services.publishing.composer import ComposerService

    return ComposerService(session, AuditService(session))


def intent_with_asset(session: Session, **asset_kwargs: Any):
    """A project with a clean 9:16 asset attached: the normal starting point."""
    composer = make_composer(session)
    asset = make_asset(session, **asset_kwargs)
    project = composer.create_intent(title="Morning routine", source_asset_id=asset.id)
    return composer, project, asset


def freeze_job(job, state: str) -> None:
    """Put a job in a state directly, for tests about editing and locking (not about transitions)."""
    job.job_state = state


def now() -> datetime:
    return utcnow()


def make_published_job(
    session: Session,
    *,
    platform: Platform = Platform.TIKTOK,
    account: SocialAccount | None = None,
    remote_post_id: str | None = None,
    provider_media_id: str | None = None,
    published_at: datetime | None = None,
):
    """A published job with attribution: project → variant → job → provider ids.

    Phase 8 reads performance for objects Phase 7 published, so the analytics tests need a real chain
    (campaign, project, variant, job, asset) rather than a hand-written job row with no parents.
    """
    from creatoros.core.ids import new_ulid
    from creatoros.db.enums import PublishMode, PublishState
    from creatoros.db.models import Campaign, PlatformVariant, PublishJob
    from creatoros.services.backup import AuditService
    from creatoros.services.publishing.composer import ComposerService

    platform = platform if isinstance(platform, Platform) else Platform(str(platform))
    account = account or make_account(session, platform=platform)
    asset = make_asset(session)

    campaign = Campaign(name=f"campaign-{uuid4().hex[:8]}")
    session.add(campaign)
    session.flush()

    composer = ComposerService(session, AuditService(session))
    project = composer.create_intent(
        title="Launch week", campaign_id=campaign.id, source_asset_id=asset.id
    )
    variant = PlatformVariant(
        project_id=project.id,
        platform=platform,
        target_account_id=account.id,
        caption="Launch week",
        hashtags_json=["#launch"],
    )
    session.add(variant)
    session.flush()

    job = PublishJob(
        id=new_ulid(),
        variant_id=variant.id,
        platform=platform,
        account_id=account.id,
        job_state=PublishState.PUBLISHED,
        idempotency_key=f"analytics-fixture-{uuid4().hex[:16]}",
        asset_id=asset.id,
        publish_mode=PublishMode.DIRECT_POST
        if platform is Platform.TIKTOK
        else PublishMode.REELS
        if platform is Platform.INSTAGRAM
        else PublishMode.VIDEO,
        remote_post_id=remote_post_id or f"{platform.value}-post-{uuid4().hex[:10]}",
        provider_media_id=provider_media_id,
        provider_published_at=published_at or datetime.now(UTC),
    )
    session.add(job)
    session.flush()

    class _Published:
        job_id = job.id
        remote_post_id = job.remote_post_id
        variant_id = variant.id
        project_id = project.id
        campaign_id = campaign.id
        asset_id = asset.id

    return _Published()
