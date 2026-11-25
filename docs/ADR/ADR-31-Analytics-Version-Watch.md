# ADR-31 — Version watch observes; it never upgrades

**Status:** Accepted, **implemented + tested** (P8.24 — `services/analytics/version_watch.py`,
`GET /analytics/versions`, `POST /analytics/versions/check`, 7-day scheduled check).

## Context

P8.4 pinned the provider API versions (Graph `v23.0`, Instagram `v23.0`, TikTok `v2`) and stored the
authority in `provider_app_config.api_version`. `version_monitor.py` compares a *requested* version
with the configured one and refuses a retired version **before any HTTP call** (requirement 2).

What was missing was the other half: knowing when the pinned version is about to stop working. Meta
retires Graph versions on a published calendar; on 2026-11-23 the researched calendar puts reach and
impression metric retirement at June 2026 across *all* versions. A monitor that only compares
configured-vs-requested values cannot see that coming.

## Decision

**A version watch that observes and reports, with no write path to the pin.**

* `services/analytics/version_watch.py` holds the documented facts as data, each with the date it was
  read: `SOURCES` (doc URLs + `documented_at = 2026-11-23`), `ENDPOINT_CHANGES`, `SUNSETS` (including
  `deprecation_scope = all_versions` vs `api_version`).
* `observe()` records an observation (timestamp, pinned version, observed latest documented version,
  staleness) into the settings store under `WATCH_KEY`. `load()` reads it back. `warnings()` derives
  the advisory list from the stored observation and the current pin — **not** from a live network
  call at read time, so a page load never depends on reaching Meta.
* `STALE_AFTER_DAYS = 90`: an observation older than that is reported `stale` rather than presented as
  current. The evidence date is always shown next to the claim.
* A live refresh runs only when `CREATOROS_ANALYTICS_VERSION_FETCH=1`. Default off: the desktop app
  must not make outbound calls to provider documentation as a side effect of opening a page, and the
  response is *documentation*, not an API — parsing it is best-effort, so it can only ever raise a
  warning.
* **The module has no code that writes `provider_app_config.api_version`.** A version bump is a code
  change with a migration and compatibility tests, and stays that way.
* `GET /analytics/versions` reports, per platform: the pinned version, the latest documented version,
  the observation age, metrics whose `api_version_max` is below the pin, and the sunset dates.
  `POST /analytics/versions/check` records a fresh observation (respecting the env gate).
* A scheduled `version_check` job runs every 7 days on the `analytics` lane, so a machine that is left
  running accumulates observations automatically.

## Consequences

* Requirements 2 and 15 (version monitoring; `/version` and `/diagnostics` reporting analytics
  capability from facts) are satisfied by observation + refusal, not by auto-upgrade.
* A stale observation is visible as stale; the alternative — presenting a three-month-old reading as
  "latest" — would be exactly the kind of invented currency the brief forbids.
* If Meta publishes a version whose retirement affects a metric in the catalogue, the warning names the
  metric key and the sunset date; the operator still has to change the pin deliberately.
