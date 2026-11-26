"""The collector: plan, fetch, write, and record the run — in that order, once.

Every collection goes through the same five steps, and each step has one job:

1. **Version guard** — a retired Graph version is refused before anything is planned.
2. **Plan** — `registry.plan()` decides, per metric, whether it may be requested and why
   (`not_authorized`, `below_threshold`, `version_above`, `not_supported`). Metrics the plan refuses
   are written as observations with that availability, so the UI can explain the gap; they are never
   requested and never turned into zeros.
3. **Run** — `runs.open_run()` gives the logical attempt an id. A run that already succeeded is not
   executed again; a crashed run is resumed.
4. **Fetch + write** — the provider adapter returns drafts; `SnapshotWriter` persists them under the
   run id, with attribution captured at that moment.
5. **Finish** — counters, error class and the redacted error detail go on the run.

The collector owns no HTTP knowledge (adapters do), no SQL for individual rows (the writer does) and
no policy about what is collectable (the registry does).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ...db.enums import (
    AnalyticsErrorClass,
    AnalyticsObjectType,
    AnalyticsRunOutcome,
    AnalyticsRunTarget,
    AnalyticsWindowKind,
    MetricAvailability,
    OAuthRoute,
    Platform,
)
from ...db.models import SocialAccount
from ...integrations.analytics.base import AnalyticsAdapter, CollectRequest, CollectResult
from ...integrations.analytics.catalog import MetricSpec, catalogue_by_key
from ...integrations.analytics.errors import AnalyticsError
from ...integrations.analytics.facebook import FacebookInsightsAdapter
from ...integrations.analytics.instagram import (
    LOGIN_FACEBOOK,
    LOGIN_INSTAGRAM,
    InstagramInsightsAdapter,
)
from ...integrations.analytics.tiktok import SCOPE_STATS, TikTokInsightsAdapter
from ...integrations.social.errors import SocialError
from . import registry, runs, version_monitor
from .budget import BudgetExceeded, BudgetGovernor
from .snapshots import SnapshotWriter, attribute


#: The account itself is a sync; anything else is a metric collection for one object.
def _target_for(object_type: AnalyticsObjectType) -> AnalyticsRunTarget:
    return AnalyticsRunTarget.ACCOUNT_SYNC if object_type is AnalyticsObjectType.ACCOUNT else AnalyticsRunTarget.METRICS

#: Which API host answered, recorded per observation.
_HOST_BY_ROUTE = {
    (Platform.FACEBOOK, None): "graph.facebook.com",
    (Platform.INSTAGRAM, LOGIN_INSTAGRAM): "graph.instagram.com",
    (Platform.INSTAGRAM, LOGIN_FACEBOOK): "graph.facebook.com",
    (Platform.TIKTOK, None): "open.tiktokapis.com",
}


@dataclass(slots=True)
class CollectionOutcome:
    account_id: str
    platform: Platform
    runs: list[dict] = field(default_factory=list)
    observed: int = 0
    unavailable: int = 0
    written: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped_reason is None

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "platform": self.platform.value,
            "runs": self.runs,
            "metrics_observed": self.observed,
            "metrics_unavailable": self.unavailable,
            "metrics_written": self.written,
            "warnings": self.warnings,
            "skipped_reason": self.skipped_reason,
        }


class AnalyticsCollector:
    def __init__(
        self,
        session: Session,
        *,
        adapters: dict[Platform, AnalyticsAdapter] | None = None,
        budget: BudgetGovernor | None = None,
        token_resolver: Any | None = None,
        now: datetime | None = None,
    ) -> None:
        self.session = session
        self.budget = budget or BudgetGovernor()
        self.writer = SnapshotWriter(session)
        self._adapters = adapters or {}
        self._token_resolver = token_resolver
        self._now = now

    # ------------------------------------------------------------------ entry points
    async def collect_account(
        self,
        account: SocialAccount,
        *,
        object_type: AnalyticsObjectType = AnalyticsObjectType.ACCOUNT,
        window_kind: AnalyticsWindowKind | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        product_type: str | None = None,
        object_id: str | None = None,
        publication_id: str | None = None,
        job_id: str | None = None,
        token: str | None = None,
    ) -> CollectionOutcome:
        """Collect one object (the account itself, or one post/video) for one account."""
        outcome = CollectionOutcome(account_id=account.id, platform=_platform(account))
        now = self._now or datetime.now(UTC)
        api_version = account.api_version or version_monitor.configured_version(self.session, outcome.platform)

        try:
            version_monitor.assert_version_usable(outcome.platform, api_version, today=now.date())
        except version_monitor.VersionUnsupported as exc:
            outcome.skipped_reason = f"GRAPH_VERSION_UNSUPPORTED: {exc.reason}"
            return outcome

        provider_object_id = object_id or account.provider_account_id
        target_kind = _target_for(object_type)
        granted = frozenset(str(scope) for scope in (account.scopes_json or []))

        plan = registry.plan(
            self.session,
            platform=outcome.platform,
            object_type=object_type,
            granted_scopes=granted,
            api_version=api_version,
            account_signals=_account_signals(account, outcome.platform),
        )
        specs, refused = _split_plan(plan)

        context = runs.open_run(
            self.session,
            account=account,
            target_kind=target_kind,
            target_object_id=provider_object_id if object_type is not AnalyticsObjectType.ACCOUNT else None,
            window_kind=window_kind,
            api_version=api_version,
            job_id=job_id,
            now=now,
        )
        if context.closed:
            outcome.runs.append({**context.as_dict(), "reason": "already_finished"})
            return outcome

        counters = runs.RunCounters(metrics_attempted=len(specs))
        attribution = (
            attribute(self.session, platform=outcome.platform, provider_object_id=provider_object_id, account_id=account.id)
            if object_type is not AnalyticsObjectType.ACCOUNT
            else _attribution_for_publication(self.session, outcome.platform, publication_id)
        )

        drafts: list = []
        try:
            self.budget.acquire(outcome.platform, account.id)
        except BudgetExceeded as exc:
            outcome.skipped_reason = exc.code
            outcome.warnings.append(str(exc))
            runs.finish_run(
                self.session,
                context,
                outcome=AnalyticsRunOutcome.SKIPPED,
                counters=counters,
                error_class=AnalyticsErrorClass.RATE_LIMIT,
                error_code=exc.code,
                error_detail=str(exc),
                now=now,
            )
            outcome.runs.append({**context.as_dict(), "reason": exc.code})
            return outcome

        try:
            if token is None:
                token = await self._access_token(account)
            adapter = self._adapter(account, outcome.platform)
            request = CollectRequest(
                object_id=provider_object_id,
                object_type=object_type,
                specs=tuple(specs),
                since=since,
                until=until,
                period_kind=None,
                product_type=product_type,
                page_id=account.parent_page_id,
                granted_scopes=granted,
            )
            fetched: CollectResult = await adapter.collect(request, token=token, api_version=api_version)
            drafts = list(fetched.drafts)
            counters.http_calls = fetched.calls + (1 if token else 0)
            outcome.warnings.extend(fetched.warnings)
        except (AnalyticsError, SocialError, BudgetExceeded) as error:
            code = getattr(error, "code", "ANALYTICS_ERROR")
            runs.finish_run(
                self.session,
                context,
                outcome=AnalyticsRunOutcome.FAILED,
                counters=counters,
                error_class=_error_class(code),
                error_code=str(code),
                error_detail=getattr(error, "message", None) or str(error),
                now=now,
            )
            outcome.runs.append({**context.as_dict(), "reason": str(code)})
            outcome.warnings.append(f"{code}: {getattr(error, 'message', str(error))}")
            return outcome

        drafts.extend(refused)
        write = self.writer.write(
            run_id=context.run_id,
            account_id=account.id,
            platform=outcome.platform,
            object_type=object_type,
            provider_object_id=provider_object_id,
            drafts=drafts,
            api_version=api_version,
            api_host=_api_host(outcome.platform, account),
            source_ref=f"{target_kind.value}:{provider_object_id}",
            attribution=attribution,
            collected_at=now,
        )
        counters.metrics_written = write.inserted + write.updated
        counters.metrics_observed = sum(1 for draft in drafts if draft.availability is MetricAvailability.OBSERVED)
        counters.metrics_unavailable = len(drafts) - counters.metrics_observed
        runs.finish_run(
            self.session,
            context,
            outcome=AnalyticsRunOutcome.SUCCEEDED,
            counters=counters,
            rate_limit_remaining=self.budget.remaining(outcome.platform, account.id),
            now=now,
        )
        outcome.observed = counters.metrics_observed
        outcome.unavailable = counters.metrics_unavailable
        outcome.written = counters.metrics_written
        outcome.runs.append({**context.as_dict(), "write": write.as_dict()})
        return outcome

    async def collect_many(
        self,
        accounts: list[SocialAccount],
        **kwargs: Any,
    ) -> list[CollectionOutcome]:
        results: list[CollectionOutcome] = []
        for account in accounts:
            results.append(await self.collect_account(account, **kwargs))
        return results

    # ------------------------------------------------------------------ internals
    def _adapter(self, account: SocialAccount, platform: Platform) -> AnalyticsAdapter:
        injected = self._adapters.get(platform)
        if injected is not None:
            return injected
        if platform is Platform.FACEBOOK:
            return FacebookInsightsAdapter()
        if platform is Platform.TIKTOK:
            return TikTokInsightsAdapter()
        route = getattr(account, "route", None)
        login_path = LOGIN_FACEBOOK if route is OAuthRoute.FACEBOOK_LOGIN else LOGIN_INSTAGRAM
        return InstagramInsightsAdapter(login_path=login_path)

    async def _access_token(self, account: SocialAccount) -> str:
        if self._token_resolver is not None:
            return await self._token_resolver(account)
        from ..social.tokens import TokenService

        try:
            from ...api.routes.social import get_vault
        except ImportError as error:  # pragma: no cover - package layout guard
            raise AnalyticsError("CREDENTIAL_UNAVAILABLE", "no vault is available for analytics") from error
        return await TokenService(self.session, get_vault()).access_token(account)


def _platform(account: SocialAccount) -> Platform:
    return account.platform if isinstance(account.platform, Platform) else Platform(str(account.platform))


def _api_host(platform: Platform, account: SocialAccount) -> str:
    if platform is not Platform.INSTAGRAM:
        return _HOST_BY_ROUTE[(platform, None)]
    route = getattr(account, "route", None)
    login = LOGIN_FACEBOOK if route is OAuthRoute.FACEBOOK_LOGIN else LOGIN_INSTAGRAM
    return _HOST_BY_ROUTE[(platform, login)]


def _account_signals(account: SocialAccount, platform: Platform) -> dict | None:
    """Facts the plan needs for threshold rules, when the account record knows them."""
    signals: dict[str, Any] = {}
    meta = getattr(account, "meta_json", None) or {}
    followers = meta.get("followers") or meta.get("follower_count") or getattr(account, "followers", None)
    if isinstance(followers, (int, float)):
        signals["min_followers"] = int(followers)
    likes = meta.get("fan_count") or meta.get("page_likes")
    if isinstance(likes, (int, float)):
        signals["min_page_likes"] = int(likes)
    return signals or None


def _split_plan(plan: list[registry.PlannedMetric]) -> tuple[list[MetricSpec], list]:
    from ...integrations.analytics.base import skipped as skipped_draft

    by_key = catalogue_by_key()
    specs: list[MetricSpec] = []
    refused = []
    for item in plan:
        spec = by_key.get(item.metric_key)
        if spec is None:
            continue
        if item.allowed:
            specs.append(spec)
        elif item.availability_if_skipped is not None:
            refused.append(skipped_draft(spec, item.availability_if_skipped, f"{item.reason}: {item.detail}"))
    return specs, refused


def _attribution_for_publication(session: Session, platform: Platform, publication_id: str | None):
    from ...db.models import PublishJob
    from .snapshots import Attribution, _attribution_from_job

    if not publication_id:
        return Attribution()
    job = session.get(PublishJob, publication_id)
    return _attribution_from_job(session, job) if job is not None else Attribution()


def _error_class(code: str) -> AnalyticsErrorClass:
    mapping = {
        "RATE_LIMITED": AnalyticsErrorClass.RATE_LIMIT,
        "PROVIDER_UNAVAILABLE": AnalyticsErrorClass.TRANSIENT,
        "PROVIDER_TIMEOUT": AnalyticsErrorClass.TRANSIENT,
        "NETWORK_ERROR": AnalyticsErrorClass.TRANSIENT,
        "TOKEN_EXPIRED": AnalyticsErrorClass.AUTHORIZATION,
        "TOKEN_REVOKED": AnalyticsErrorClass.AUTHORIZATION,
        "REAUTHORIZATION_REQUIRED": AnalyticsErrorClass.AUTHORIZATION,
        "SCOPE_NOT_GRANTED": AnalyticsErrorClass.PERMISSION,
        "CREDENTIAL_UNAVAILABLE": AnalyticsErrorClass.PERMISSION,
        "APP_REVIEW_REQUIRED": AnalyticsErrorClass.PERMISSION,
        "GRAPH_VERSION_UNSUPPORTED": AnalyticsErrorClass.PERMANENT,
        "ANALYTICS_BUDGET_EXCEEDED": AnalyticsErrorClass.RATE_LIMIT,
    }
    return mapping.get(code, AnalyticsErrorClass.PROVIDER_PROCESSING)


def stats_scope_granted(account: SocialAccount) -> bool:
    """TikTok account counters need `user.info.stats`; the UI asks for it explicitly when missing."""
    return SCOPE_STATS in {str(scope) for scope in (account.scopes_json or [])}
