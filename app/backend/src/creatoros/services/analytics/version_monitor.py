"""Version pinning, monitoring and compatibility.

The Graph version is a *fact about the app*, not a constant in the adapter:

1. ``provider_app_config.api_version`` is the authority (a human edit in App settings),
2. ``integrations/social/config.py::DEFAULT_GRAPH_VERSION`` is only the first-boot default,
3. every observation records the version that answered it.

Nothing here upgrades a version. These functions answer three questions and refuse one action:

* is the configured version still supported, or past its published retirement date? (refuse)
* which registry metrics are above the configured version? (never requested)
* what is the newest documented version, so the UI can say "v23.0 configured, v27.0 documented"?
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import Platform
from ...db.models import AnalyticsMetricDefinition, ProviderAppConfig
from ...integrations.analytics.base import parse_api_version, version_within
from ...integrations.social import config as social_config

#: Newest version each provider documents (read 2026-11-23). Recorded, never auto-applied.
LATEST_DOCUMENTED: dict[Platform, str] = {
    Platform.FACEBOOK: "v27.0",
    Platform.INSTAGRAM: "v27.0",
    Platform.TIKTOK: "v2",
}

#: Published retirement dates. A version here is refused before any HTTP call is attempted.
RETIRED_ON: dict[Platform, dict[str, date]] = {
    Platform.FACEBOOK: {
        "v19.0": date(2026, 5, 21),
        "v20.0": date(2026, 9, 24),
    },
    Platform.INSTAGRAM: {
        "v19.0": date(2026, 5, 21),
        "v20.0": date(2026, 9, 24),
    },
    Platform.TIKTOK: {},
}

class VersionUnsupported(RuntimeError):
    """The configured provider version cannot be used (retired or malformed)."""

    code = "GRAPH_VERSION_UNSUPPORTED"

    def __init__(self, platform: Platform, version: str, reason: str) -> None:
        super().__init__(f"{platform.value} {version}: {reason}")
        self.platform = platform
        self.version = version
        self.reason = reason


@dataclass(frozen=True, slots=True)
class VersionReport:
    platform: Platform
    configured: str
    latest_documented: str
    retired: bool
    retirement_date: date | None
    metrics_above_version: tuple[str, ...]
    note: str

    def as_dict(self) -> dict:
        return {
            "platform": self.platform.value,
            "configured": self.configured,
            "latest_documented": self.latest_documented,
            "retired": self.retired,
            "retirement_date": self.retirement_date.isoformat() if self.retirement_date else None,
            "metrics_above_version": list(self.metrics_above_version),
            "note": self.note,
        }


def parse_version(version: str | None) -> tuple[int, int]:
    """``v23.0`` -> ``(23, 0)``; ``v2`` -> ``(2, 0)``. Raises on nonsense instead of guessing."""
    if not version:
        raise VersionUnsupported(Platform.FACEBOOK, str(version), "no version configured")
    return parse_api_version(str(version))


def version_at_least(candidate: str, floor: str) -> bool:
    return parse_version(candidate) >= parse_version(floor)


def version_at_most(candidate: str, ceiling: str) -> bool:
    return parse_version(candidate) <= parse_version(ceiling)


def retirement_date(platform: Platform, version: str) -> date | None:
    return RETIRED_ON.get(platform, {}).get(str(version))


def is_retired(platform: Platform, version: str, *, today: date | None = None) -> bool:
    retired = retirement_date(platform, version)
    if retired is None:
        return False
    return (today or datetime.now(UTC).date()) >= retired


def assert_version_usable(platform: Platform, version: str, *, today: date | None = None) -> None:
    """Refuse a retired version *before* an adapter builds a URL."""
    parse_version(version)  # malformed versions fail here
    retired = retirement_date(platform, version)
    if retired is not None and is_retired(platform, version, today=today):
        raise VersionUnsupported(
            platform,
            version,
            f"retired on {retired.isoformat()}; update the app's API version explicitly before collecting",
        )


def metric_allowed_on_version(definition: AnalyticsMetricDefinition, version: str) -> bool:
    """A metric is requestable only inside its documented version window.

    Delegates to the adapter helper so the registry and the HTTP layer cannot disagree about what
    "v23.0 <= v24.0" means.
    """
    return version_within(definition.api_version_min, definition.api_version_max, version)


def configured_version(session: Session, platform: Platform) -> str:
    """The app record wins; the code default is only the first-boot fallback."""
    row = session.scalar(
        sa.select(ProviderAppConfig)
        .where(ProviderAppConfig.provider == platform)
        .order_by(ProviderAppConfig.created_at.asc())
    )
    if row is not None and row.api_version:
        return str(row.api_version)
    if platform is Platform.TIKTOK:
        return social_config.TIKTOK_API_VERSION
    if platform is Platform.INSTAGRAM:
        return social_config.DEFAULT_IG_VERSION
    return social_config.DEFAULT_GRAPH_VERSION


def report(session: Session, platform: Platform, *, today: date | None = None) -> VersionReport:
    version = configured_version(session, platform)
    above: list[str] = []
    for definition in session.scalars(
        sa.select(AnalyticsMetricDefinition).where(AnalyticsMetricDefinition.platform == platform)
    ):
        if not metric_allowed_on_version(definition, version):
            above.append(definition.metric_key)
    retired = retirement_date(platform, version)
    latest = LATEST_DOCUMENTED.get(platform, version)
    note = (
        "configured version is retired; collection is refused until an operator updates it"
        if retired is not None and is_retired(platform, version, today=today)
        else f"{len(above)} metric(s) are outside the configured version and will not be requested"
    )
    return VersionReport(
        platform=platform,
        configured=version,
        latest_documented=latest,
        retired=bool(retired is not None and is_retired(platform, version, today=today)),
        retirement_date=retired,
        metrics_above_version=tuple(sorted(above)),
        note=note,
    )


def report_all(session: Session, *, today: date | None = None) -> list[dict]:
    return [report(session, platform, today=today).as_dict() for platform in Platform]
