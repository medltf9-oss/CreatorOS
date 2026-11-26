"""Provider API version watch: recording *when we looked*, not guessing what changed.

``version_monitor`` already answers "is the configured version usable, and which metrics are outside
it". It cannot answer the question an operator actually asks before a bump — *how old is this claim?* —
because its table of documented versions is a constant in the source. This module adds the missing
half: an observation record, with the date it was made, the official source it came from, and a
staleness verdict.

The rules:

* **Nothing bumps a version.** ``observe()`` writes an *observation*, never ``provider_app_config``.
* **A failed or disabled check is reported, not invented.** With the network fetch off (the default)
  or failing, the observation says so and is marked ``stale``/``unavailable``. It never silently
  republishes the old table as fresh.
* **Official sources only.** The URLs are the provider's own changelog/version pages; nothing here
  scrapes a blog, a forum or a third-party tracker.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ...db.enums import Platform
from ...db.models import AppSetting, ProviderAppConfig
from . import version_monitor

WATCH_KEY = "analytics.version_watch"
#: An observation older than this is reported stale rather than treated as current knowledge.
STALE_AFTER_DAYS = 90
#: The documented table in ``version_monitor`` was read on this date (see LATEST_DOCUMENTED).
DOCUMENTED_AT = date(2026, 11, 23)

#: The provider's own published version/changelog pages. Recorded for the operator to open, and used
#: only when ``CREATOROS_ANALYTICS_VERSION_FETCH=1`` explicitly opts into a live check.
SOURCES: dict[Platform, str] = {
    Platform.FACEBOOK: "https://developers.facebook.com/docs/graph-api/changelog",
    Platform.INSTAGRAM: "https://developers.facebook.com/docs/instagram-platform/changelog",
    Platform.TIKTOK: "https://developers.tiktok.com/doc/tiktok-api-v2-get-user-info/",
}

#: Endpoint changes that affect what this application collects, with the date they take effect.
#: Recorded from the provider documentation read on DOCUMENTED_AT; a bump decision is left to an
#: operator because it changes which metrics exist (plan §20.1.6).
ENDPOINT_CHANGES: dict[Platform, tuple[dict[str, str], ...]] = {
    Platform.FACEBOOK: (
        {
            "endpoint": "/{page-id}/insights (reach, impressions)",
            "change": "reach and impression metrics are being retired for all Graph versions",
            "effective": "2026-06",
            "impact": "the catalogue already prefers the *_media_view family; no reach-based total is computed",
        },
    ),
    Platform.INSTAGRAM: (
        {
            "endpoint": "/{ig-user-id}/insights",
            "change": "performance metrics require instagram_manage_insights on a professional account",
            "effective": "current",
            "impact": "accounts without the permission report needs_user_consent, not zero values",
        },
    ),
    Platform.TIKTOK: (
        {
            "endpoint": "/v2/user/info/",
            "change": "follower_count and likes_count require the user.info.stats scope",
            "effective": "current",
            "impact": "requested as an extra scope; the TikTok Research API stays explicitly excluded",
        },
    ),
}

#: Dates on which the configured version stops working. Duplicated from ``version_monitor`` so the
#: report can print them next to the observation without importing internals.
SUNSETS: dict[Platform, dict[str, str]] = {
    platform: {version: when.isoformat() for version, when in table.items()}
    for platform, table in version_monitor.RETIRED_ON.items()
}


class WatchUnavailable(RuntimeError):
    code = "VERSION_WATCH_UNAVAILABLE"


@dataclass(slots=True)
class Observation:
    """One recorded look at the documented versions."""

    checked_at: datetime
    status: str  # current | stale | unavailable
    source: str
    method: str  # recorded_table | live_fetch
    documented: dict[str, str]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "status": self.status,
            "source": self.source,
            "method": self.method,
            "documented": dict(self.documented),
            "note": self.note,
        }


def _fetch_enabled() -> bool:
    return os.environ.get("CREATOROS_ANALYTICS_VERSION_FETCH", "").strip() == "1"


def _age_days(moment: datetime, *, now: datetime) -> int:
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return max(0, (now - moment).days)


def _documented_table() -> dict[str, str]:
    return {platform.value: version for platform, version in version_monitor.LATEST_DOCUMENTED.items()}


def _fetch_source(url: str, *, timeout: float = 5.0) -> str:
    """Fetch an official page, returning its first 200 chars on success and raising otherwise.

    Deliberately tiny: the result is used only to prove the source answered, never to parse a version
    out of marketing HTML. Parsing a page for a version string would be exactly the "fabricated
    update" the brief forbids when the page changes shape.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "CreatorOS-version-watch/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(400).decode("utf-8", "ignore")
    return body[:200]


def observe(session: Session, *, now: datetime | None = None, fetch: bool | None = None) -> Observation:
    """Record what the documented versions are, and how old that knowledge is.

    With the live fetch disabled or failing, the result is honest about it: ``stale`` when the
    recorded table is older than :data:`STALE_AFTER_DAYS`, ``unavailable`` when a requested live check
    could not reach any provider page.
    """
    now = now or datetime.now(UTC)
    fetch = _fetch_enabled() if fetch is None else fetch
    table_age = (now.date() - DOCUMENTED_AT).days

    method = "recorded_table"
    status = "current" if table_age <= STALE_AFTER_DAYS else "stale"
    notes: list[str] = [
        f"documented versions read from the provider documentation on {DOCUMENTED_AT.isoformat()}",
    ]

    if fetch:
        method = "live_fetch"
        reachable: list[str] = []
        failures: list[str] = []
        for platform, url in SOURCES.items():
            try:
                _fetch_source(url)
                reachable.append(platform.value)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failures.append(f"{platform.value}: {exc}")
        if not reachable:
            status = "unavailable"
            notes.append("live version check could not reach any official source: " + "; ".join(failures[:3]))
        else:
            status = "current"
            notes.append(
                "official sources answered for "
                + ", ".join(reachable)
                + (f"; unreachable: {', '.join(failures)}" if failures else "")
            )
            notes.append(
                "a reachable source proves the documentation is online, not that the version changed; "
                "no version was modified by this check"
            )
    elif table_age > STALE_AFTER_DAYS:
        notes.append(f"the recorded table is {table_age} days old; enable CREATOROS_ANALYTICS_VERSION_FETCH=1 to refresh")

    observation = Observation(
        checked_at=now,
        status=status,
        source=", ".join(sorted({url for url in SOURCES.values()})),
        method=method,
        documented=_documented_table(),
        note=" ".join(notes),
    )
    _store(session, observation)
    return observation


def _store(session: Session, observation: Observation) -> None:
    payload = observation.as_dict()
    row = session.get(AppSetting, WATCH_KEY)
    if row is None:
        session.add(AppSetting(key=WATCH_KEY, value_json=payload, scope="app"))
    else:
        row.value_json = payload
    session.flush()


def load(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """The stored observation, with its age and a staleness verdict computed now.

    When nothing has ever been recorded, the answer is a *derived* observation from the code table,
    marked ``never_checked`` so a caller cannot mistake it for a live verification.
    """
    now = now or datetime.now(UTC)
    row = session.get(AppSetting, WATCH_KEY)
    if row is None or not isinstance(row.value_json, dict) or not row.value_json.get("checked_at"):
        age = (now.date() - DOCUMENTED_AT).days
        return {
            "checked_at": None,
            "age_days": age,
            "status": "never_checked" if age <= STALE_AFTER_DAYS else "stale",
            "method": "recorded_table",
            "source": ", ".join(sorted(SOURCES.values())),
            "documented": _documented_table(),
            "stale_after_days": STALE_AFTER_DAYS,
            "note": (
                f"no version check has been recorded; the table was read on {DOCUMENTED_AT.isoformat()} "
                "and is reported as knowledge of that date, not as a live verification"
            ),
        }
    stored = dict(row.value_json)
    try:
        checked_at = datetime.fromisoformat(str(stored["checked_at"]))
    except ValueError:
        checked_at = now
    stored["checked_at"] = checked_at.isoformat()
    stored["age_days"] = _age_days(checked_at, now=now)
    stored["stale_after_days"] = STALE_AFTER_DAYS
    if stored.get("status") == "current" and stored["age_days"] > STALE_AFTER_DAYS:
        stored["status"] = "stale"
        stored["note"] = (
            f"{stored.get('note', '')} — the last check is {stored['age_days']} days old and is now stale"
        ).strip()
    return stored


def platforms(session: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """The per-platform version report, with the observation and endpoint notes attached."""
    observation = load(session, now=now)
    rows = version_monitor.report_all(session, today=(now or datetime.now(UTC)).date())
    for row in rows:
        platform = Platform(row["platform"])
        row["documented"] = observation["documented"].get(row["platform"])
        row["endpoint_changes"] = list(ENDPOINT_CHANGES.get(platform, ()))
        row["sunset_dates"] = SUNSETS.get(platform, {})
        row["observation_status"] = observation["status"]
        row["observation_age_days"] = observation["age_days"]
        row["observation_checked_at"] = observation["checked_at"]
        row["observation_note"] = observation["note"]
        configured = _configured(session, platform)
        row["configured_source"] = configured
        if row["retired"]:
            row["note"] = row["note"] + "; collection is refused until an operator changes the version"
    return rows


def _configured(session: Session, platform: Platform) -> str:
    row = session.scalar(
        sa.select(ProviderAppConfig)
        .where(ProviderAppConfig.provider == platform)
        .order_by(ProviderAppConfig.created_at.asc())
    )
    return "provider_app_config" if row is not None and row.api_version else "code_default"


def warnings(session: Session, *, now: datetime | None = None) -> list[str]:
    """The lines diagnostics and `/version` show. Each one is a fact with a date attached."""
    observation = load(session, now=now)
    rows = version_monitor.report_all(session, today=(now or datetime.now(UTC)).date())
    out: list[str] = []
    retired = [row["platform"] for row in rows if row["retired"]]
    if retired:
        out.append(f"retired API version configured for: {', '.join(retired)} — collection is refused")
    behind = {row["platform"]: len(row["metrics_above_version"]) for row in rows if row["metrics_above_version"]}
    if behind:
        out.append(
            "metrics outside the configured version (never requested): "
            + "; ".join(f"{platform}: {count}" for platform, count in behind.items())
        )
    if observation["status"] in {"stale", "never_checked", "unavailable"}:
        out.append(f"version watch is {observation['status']} ({observation['age_days']} days old)")
    return out


__all__ = [
    "DOCUMENTED_AT",
    "ENDPOINT_CHANGES",
    "SOURCES",
    "STALE_AFTER_DAYS",
    "SUNSETS",
    "WATCH_KEY",
    "WatchUnavailable",
    "load",
    "observe",
    "platforms",
    "warnings",
]

