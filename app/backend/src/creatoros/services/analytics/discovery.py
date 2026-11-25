"""Discovery: which published objects exist for an account, and which ones we did not publish.

Two sources, deliberately kept distinct, because they answer different questions:

* **Our own publications** (`publish_job`): we know the project, campaign, asset and publish time.
  Linking is exact — `publish_job.remote_post_id` / `provider_media_id` / `remote_container_id` is
  the provider's own id — so performance can be attributed without guessing.
* **The provider's listing** (TikTok `video/list`, later Meta `/{page-id}/posts`): objects we did not
  publish through CreatorOS, or published before Phase 7 existed. They are collected and stored
  **unlinked**: no project, no campaign, and the UI says "not published by CreatorOS" rather than
  inventing an attribution.

Discovery never collects metrics itself. It returns candidates; the collector decides whether a
window is due and whether the account is allowed to ask.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import (
    AnalyticsErrorClass,
    AnalyticsObjectType,
    AnalyticsRunOutcome,
    AnalyticsSyncStatus,
    Platform,
)
from ...db.models import (
    AnalyticsCollectionRun,
    AnalyticsSnapshot,
    AnalyticsSyncState,
    PublishJob,
    SocialAccount,
)
from . import sync_state as sync

#: How many published objects one discovery sweep considers.
DISCOVERY_LIMIT = 100
#: Discovery looks back this far by default: further back is backfill, not discovery.
DISCOVERY_LOOKBACK_DAYS = 90

SOURCE_PUBLISH_JOB = "publish_job"
SOURCE_PROVIDER_LIST = "provider_list"


@dataclass(frozen=True, slots=True)
class DiscoveredObject:
    provider_object_id: str
    object_type: AnalyticsObjectType
    publication_id: str | None
    published_at: datetime | None
    product_type: str | None = None
    title: str | None = None
    source: str = SOURCE_PUBLISH_JOB

    @property
    def linked(self) -> bool:
        return self.publication_id is not None

    def as_dict(self) -> dict:
        return {
            "provider_object_id": self.provider_object_id,
            "object_type": self.object_type.value,
            "publication_id": self.publication_id,
            "linked": self.linked,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "product_type": self.product_type,
            "source": self.source,
        }


def object_type_for(platform: Platform, publish_mode: str | None) -> AnalyticsObjectType:
    """Which analytics surface a published object has, per provider."""
    mode = (publish_mode or "").lower()
    if platform is Platform.TIKTOK:
        return AnalyticsObjectType.VIDEO
    if platform is Platform.INSTAGRAM:
        return AnalyticsObjectType.REEL if mode in {"reels", "reel"} else AnalyticsObjectType.POST
    return AnalyticsObjectType.REEL if mode in {"reels", "reel"} else AnalyticsObjectType.VIDEO


def discover_published_objects(
    session: Session,
    account: SocialAccount,
    *,
    since: datetime | None = None,
    limit: int = DISCOVERY_LIMIT,
) -> list[DiscoveredObject]:
    """Objects Phase 7 published, newest first, with their attribution available by id."""
    platform = _platform(account)
    moment = datetime.now(UTC)
    floor = since or moment - timedelta(days=DISCOVERY_LOOKBACK_DAYS)
    jobs = session.scalars(
        sa.select(PublishJob)
        .where(
            PublishJob.account_id == account.id,
            sa.or_(
                PublishJob.remote_post_id.is_not(None),
                PublishJob.provider_media_id.is_not(None),
                PublishJob.remote_container_id.is_not(None),
            ),
            sa.or_(
                PublishJob.provider_published_at.is_(None),
                PublishJob.provider_published_at >= floor,
            ),
        )
        .order_by(PublishJob.provider_published_at.desc().nulls_last())
        .limit(limit)
    )
    discovered: list[DiscoveredObject] = []
    for job in jobs:
        provider_object_id = job.remote_post_id or job.provider_media_id or job.remote_container_id
        if not provider_object_id:
            continue
        discovered.append(
            DiscoveredObject(
                provider_object_id=provider_object_id,
                object_type=object_type_for(platform, str(getattr(job.publish_mode, "value", job.publish_mode))),
                publication_id=job.id,
                published_at=job.provider_published_at or job.created_at,
                title=job.title,
                source=SOURCE_PUBLISH_JOB,
            )
        )
    return discovered


def discover_provider_objects(
    videos: list[dict],
    *,
    session: Session | None = None,
    account: SocialAccount | None = None,
) -> list[DiscoveredObject]:
    """TikTok `video/list` output as candidates, linked when we published that id ourselves."""
    discovered: list[DiscoveredObject] = []
    for video in videos:
        video_id = str(video.get("id") or "")
        if not video_id:
            continue
        publication_id = None
        if session is not None and account is not None:
            publication_id = linked_publication_id(session, account=account, provider_object_id=video_id)
        created = video.get("create_time")
        published_at = (
            datetime.fromtimestamp(int(created), tz=UTC) if isinstance(created, (int, float)) else None
        )
        discovered.append(
            DiscoveredObject(
                provider_object_id=video_id,
                object_type=AnalyticsObjectType.VIDEO,
                publication_id=publication_id,
                published_at=published_at,
                title=video.get("title"),
                source=SOURCE_PROVIDER_LIST,
            )
        )
    return discovered


def linked_publication_id(
    session: Session,
    *,
    account: SocialAccount,
    provider_object_id: str,
) -> str | None:
    """The publication we own for this provider id, if any. Never guesses."""
    return session.scalar(
        sa.select(PublishJob.id).where(
            PublishJob.account_id == account.id,
            sa.or_(
                PublishJob.remote_post_id == provider_object_id,
                PublishJob.provider_media_id == provider_object_id,
                PublishJob.remote_container_id == provider_object_id,
            ),
        )
    )


def unlinked_objects(session: Session, account: SocialAccount, *, limit: int = DISCOVERY_LIMIT) -> list[str]:
    """Provider object ids we observe but did not publish: shown, never attributed."""
    linked = {
        row[0]
        for row in session.execute(
            sa.select(PublishJob.remote_post_id).where(
                PublishJob.account_id == account.id, PublishJob.remote_post_id.is_not(None)
            )
        )
    }
    observed = session.scalars(
        sa.select(AnalyticsSnapshot.provider_object_id)
        .where(
            AnalyticsSnapshot.account_id == account.id,
            AnalyticsSnapshot.object_type != AnalyticsObjectType.ACCOUNT,
        )
        .distinct()
        .limit(limit * 4)
    )
    return [object_id for object_id in observed if object_id not in linked][:limit]


def backfill_candidates(
    session: Session,
    account: SocialAccount,
    *,
    now: datetime | None = None,
    limit: int = DISCOVERY_LIMIT,
) -> list[tuple[DiscoveredObject, list[sync.DueWindow]]]:
    """Objects whose performance windows have passed and are not yet collected.

    The retention floor is applied here, so an old object is reported as "expired" by the caller
    rather than requested and answered with an empty set.
    """
    moment = now or datetime.now(UTC)
    candidates: list[tuple[DiscoveredObject, list[sync.DueWindow]]] = []
    for discovered in discover_published_objects(session, account):
        state = sync.state_for(session, account, discovered.object_type)
        floor = sync.retention_floor(state, now=moment)
        if floor is not None and discovered.published_at is not None and discovered.published_at < floor:
            continue
        collected = collected_windows(session, account=account, provider_object_id=discovered.provider_object_id)
        due = sync.windows_due(discovered.published_at, now=moment, already_collected=collected)
        if due:
            candidates.append((discovered, due))
        if len(candidates) >= limit:
            break
    return candidates


def collected_windows(
    session: Session,
    *,
    account: SocialAccount,
    provider_object_id: str,
) -> set:
    """Which performance windows already ran for this object.

    Read from the run ledger, not from the snapshots: a window with zero observed metrics still
    counts as collected, and re-asking for it every sweep would burn the allowance for nothing.
    """
    rows = session.execute(
        sa.select(AnalyticsCollectionRun.window_kind).where(
            AnalyticsCollectionRun.account_id == account.id,
            AnalyticsCollectionRun.target_object_id == provider_object_id,
            AnalyticsCollectionRun.window_kind.is_not(None),
            AnalyticsCollectionRun.finished_at.is_not(None),
            AnalyticsCollectionRun.outcome != AnalyticsRunOutcome.FAILED,
        )
    )
    return {window_kind for (window_kind,) in rows if window_kind is not None}


def note_discovery(
    state: AnalyticsSyncState,
    objects: list[DiscoveredObject],
    *,
    complete: bool = True,
) -> None:
    """Discovery finished; the state records how much it saw without inventing a cursor."""
    sync.note_discovery_cursor(state, cursor=objects[-1].provider_object_id if objects else None, complete=complete)


def blocked_summary(states: list[AnalyticsSyncState]) -> list[dict]:
    """What a human has to fix, with the error that stopped collection."""
    return [
        {
            "account_id": state.account_id,
            "object_type": state.object_type.value,
            "state": state.state.value,
            "consecutive_failures": state.consecutive_failures,
            "error_code": state.last_error_code,
            "error_class": state.last_error_class.value if state.last_error_class else None,
            "notes": state.notes,
        }
        for state in states
        if state.state is AnalyticsSyncStatus.BLOCKED
    ]


def classify_discovery_error(error_code: str | None) -> AnalyticsErrorClass:
    code = (error_code or "").upper()
    if code in {"TOKEN_EXPIRED", "TOKEN_REVOKED", "REAUTHORIZATION_REQUIRED"}:
        return AnalyticsErrorClass.AUTHORIZATION
    if code in {"SCOPE_NOT_GRANTED", "APP_REVIEW_REQUIRED", "CREDENTIAL_UNAVAILABLE"}:
        return AnalyticsErrorClass.PERMISSION
    if code in {"RATE_LIMITED", "ANALYTICS_BUDGET_EXCEEDED"}:
        return AnalyticsErrorClass.RATE_LIMIT
    return AnalyticsErrorClass.TRANSIENT


def _platform(account: SocialAccount) -> Platform:
    return account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform))
