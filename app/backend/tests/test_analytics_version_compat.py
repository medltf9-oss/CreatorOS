"""Requirement 2 — pinned, monitored, tested versions. Six tests, one per row of plan §4.5.

The point of these tests is not that a version string exists. It is that changing it cannot happen
by accident: not by a code edit (no literal in an adapter), not by a metric that silently starts
working on a newer version, not by sending a request for a version Meta already retired, and not by
losing track of which version produced a number.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import creatoros
from creatoros.db.enums import AnalyticsObjectType, MetricAvailability, OAuthRoute, Platform
from creatoros.db.models import ProviderAppConfig
from creatoros.integrations.social import config as social_config
from creatoros.services.analytics import registry, version_monitor


def _app_config(session, platform: Platform, version: str) -> ProviderAppConfig:
    row = ProviderAppConfig(
        provider=platform,
        route=OAuthRoute.FACEBOOK_LOGIN if platform is Platform.FACEBOOK else OAuthRoute.INSTAGRAM_LOGIN,
        client_id="1234567890",
        redirect_uri="http://localhost:47821/oauth/callback/facebook_login",
        scopes_json=[],
        api_version=version,
        app_mode="development",
    )
    session.add(row)
    session.flush()
    return row


def test_no_adapter_hardcodes_a_graph_version():
    """A version literal in an adapter is how a silent upgrade happens. There are none."""
    root = Path(creatoros.__file__).parent
    versions = {"v21.0", "v22.0", "v23.0", "v24.0", "v25.0", "v26.0", "v27.0"}
    # Modules that build requests. `config.py` holds the deliberate first-boot defaults, and
    # `catalog.py` holds documented *availability windows* (`api_version_max`), which are data.
    request_builders = [
        *(root / "integrations" / "social").glob("*.py"),
        *(root / "integrations" / "analytics").glob("*.py"),
        *(root / "services" / "analytics").glob("*.py"),
    ]
    offenders: list[str] = []
    for path in request_builders:
        if path.name in {"config.py", "catalog.py", "version_monitor.py"}:
            # version_monitor *is* the monitoring code: its `LATEST_DOCUMENTED` map is an
            # observation to display, and `RETIRED_ON` is a refusal to send anything.
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if any(f'"{version}"' in stripped for version in versions) or any(
                f"/{version}/" in stripped for version in versions
            ):
                offenders.append(f"{path.relative_to(root)}:{number}: {stripped}")
    assert offenders == [], offenders


def test_pinned_version_is_the_app_record_version(session):
    """v23.0 as configured *is* v23.0 on the wire, and the record beats the code default."""
    assert social_config.DEFAULT_GRAPH_VERSION == "v23.0"
    _app_config(session, Platform.FACEBOOK, "v24.0")
    session.commit()
    assert version_monitor.configured_version(session, Platform.FACEBOOK) == "v24.0"
    # No app record yet: fall back to the documented default rather than to nothing.
    assert version_monitor.configured_version(session, Platform.TIKTOK) == social_config.TIKTOK_API_VERSION
    assert version_monitor.configured_version(session, Platform.INSTAGRAM) == social_config.DEFAULT_IG_VERSION


def test_metric_above_api_version_max_is_not_requested(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    granted = {"read_insights", "pages_read_engagement"}
    at_v23 = {
        item.metric_key: item
        for item in registry.plan(
            session,
            platform=Platform.FACEBOOK,
            object_type=AnalyticsObjectType.POST,
            granted_scopes=granted,
            api_version="v23.0",
        )
    }
    at_v21 = {
        item.metric_key: item
        for item in registry.plan(
            session,
            platform=Platform.FACEBOOK,
            object_type=AnalyticsObjectType.POST,
            granted_scopes=granted,
            api_version="v21.0",
        )
    }
    retired_metric = at_v23["fb.post.impressions"]
    assert retired_metric.allowed is False
    assert retired_metric.reason == registry.REASON_VERSION_ABOVE
    assert retired_metric.availability_if_skipped is MetricAvailability.NOT_SUPPORTED
    # The same metric is still served below its documented ceiling, so the pin is what decides.
    assert at_v21["fb.post.impressions"].allowed is True
    # And the replacement is unaffected either way.
    assert at_v23["fb.post.media_views"].allowed is True


def test_version_change_requires_explicit_setting(session):
    """Only an app-record edit moves the version, and a malformed version is refused outright."""
    registry.ensure_metric_catalog(session)
    _app_config(session, Platform.FACEBOOK, "v23.0")
    session.commit()
    assert version_monitor.configured_version(session, Platform.FACEBOOK) == "v23.0"

    with pytest.raises(ValueError):
        version_monitor.parse_version("vNext")
    with pytest.raises(ValueError):
        version_monitor.assert_version_usable(Platform.FACEBOOK, "latest")

    row = session.query(ProviderAppConfig).one()
    row.api_version = "v21.0"
    session.commit()
    assert version_monitor.configured_version(session, Platform.FACEBOOK) == "v21.0"
    # Nothing in the analytics services writes the column back: only a human edit does.
    import re

    root = Path(creatoros.__file__).parent
    writers = [
        path.name
        for path in (root / "services" / "analytics").rglob("*.py")
        if re.search(r"\.[a-z_]*api_version\s*=[^=]", path.read_text(encoding="utf-8"))
    ]
    assert writers == []


def test_retired_version_is_refused_before_http(session, monkeypatch):
    """A retired version never reaches a request builder, let alone a socket."""
    assert version_monitor.retirement_date(Platform.FACEBOOK, "v19.0") == date(2026, 5, 21)
    sent: list[str] = []

    def _spy(*args, **kwargs):  # pragma: no cover - only fires if the guard is bypassed
        sent.append(str(args))
        raise AssertionError("an HTTP call was attempted for a retired API version")

    monkeypatch.setattr("httpx.Client.request", _spy)
    monkeypatch.setattr("httpx.AsyncClient.request", _spy)

    with pytest.raises(version_monitor.VersionUnsupported) as refused:
        version_monitor.assert_version_usable(Platform.FACEBOOK, "v19.0", today=date(2026, 12, 1))
    assert refused.value.code == "GRAPH_VERSION_UNSUPPORTED"
    # Not retired *on* its own retirement date before it passes, and not before the date either.
    version_monitor.assert_version_usable(Platform.FACEBOOK, "v19.0", today=date(2026, 5, 20))

    _app_config(session, Platform.FACEBOOK, "v19.0")
    session.commit()
    report = version_monitor.report(session, Platform.FACEBOOK, today=date(2026, 12, 1))
    assert report.retired is True
    assert report.retirement_date == date(2026, 5, 21)
    assert "retired" in report.note
    assert sent == []


def test_recorded_api_version_is_returned_with_every_metric(session):
    """The version travels with the observation, and the metric plan never guesses it.

    The API half of this assertion (``GET /analytics/insights`` echoing ``api_version``) is
    completed in P8.19; here the invariant is established where the value is produced.
    """
    registry.ensure_metric_catalog(session)
    _app_config(session, Platform.FACEBOOK, "v23.0")
    session.commit()

    planned = registry.plan(
        session,
        platform=Platform.FACEBOOK,
        object_type=AnalyticsObjectType.POST,
        granted_scopes={"read_insights", "pages_read_engagement"},
        api_version=version_monitor.configured_version(session, Platform.FACEBOOK),
    )
    assert planned
    report = version_monitor.report(session, Platform.FACEBOOK)
    assert report.configured == "v23.0"
    assert report.latest_documented == "v27.0"
    # Recorded, not applied: the monitor observes the newer version and changes nothing.
    assert version_monitor.configured_version(session, Platform.FACEBOOK) == "v23.0"

    from creatoros.db.models import AnalyticsSnapshot

    assert "api_version" in AnalyticsSnapshot.__table__.columns


def test_report_lists_metrics_outside_the_configured_version(session):
    registry.ensure_metric_catalog(session)
    _app_config(session, Platform.INSTAGRAM, "v21.0")
    session.commit()
    report = version_monitor.report(session, Platform.INSTAGRAM)
    assert "ig.media.impressions" not in report.metrics_above_version
    assert report.metrics_above_version == ()

    row = session.query(ProviderAppConfig).one()
    row.api_version = "v22.0"
    session.commit()
    report = version_monitor.report(session, Platform.INSTAGRAM)
    assert "ig.media.impressions" in report.metrics_above_version
    assert "ig.account.impressions" in report.metrics_above_version
    assert report.retired is False


def test_version_report_covers_every_platform(session):
    registry.ensure_metric_catalog(session)
    session.commit()
    reports = {entry["platform"]: entry for entry in version_monitor.report_all(session)}
    assert set(reports) == {"facebook", "instagram", "tiktok"}
    for entry in reports.values():
        assert entry["configured"]
        assert entry["latest_documented"]
        assert isinstance(entry["metrics_above_version"], list)
