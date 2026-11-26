# ADR-32 — Diagnostics report facts, and say when a fact is missing

**Status:** Accepted, **implemented + tested** (P8.24 — `GET /analytics/diagnostics`, `/version`
capability flags, 12 route tests).

## Context

Acceptance criterion 15 requires `/version` and `/diagnostics` to report analytics capability per
platform *from facts*. The failure mode for a diagnostics endpoint is well known: it grows a
`"healthy": true` that nobody can reproduce, or it reports `configured: true` for a platform that has
no entitlement and then the first collection run returns 403 to a surprised user.

## Decision

**Every diagnostics field is either a measured count, a stored fact, or an explicit gap.**

`GET /analytics/diagnostics` returns, per platform and overall:

| Field | Source | Never |
| --- | --- | --- |
| `accounts`, `connected_accounts` | `social_account` count by platform/status | inferred from a token's presence |
| `capabilities` | the scopes stored on the connected account, mapped through the capability vocabulary (ADR-24) | a boolean "enabled" |
| `collection` | last run, last success, next due, failing objects, budget remaining | a synthetic "ok" |
| `reports` | count by format/status, bytes on disk, oldest/newest | a report count after a failed write |
| `retention` | last execution time, rows deleted, next due, policy | "pruned" without an audit id |
| `version_watch` | pinned version, observed version, observation age, warnings | "up to date" from a freshness that was never observed |
| `availability` | counts per `MetricAvailability` state over the window | a total that hides the `not_observed` rows |

`GET /version` carries the honest per-platform shape the plan asked for, and each value is a state, not
a promise:

```json
{
  "analytics_reports": "available",
  "analytics_reports_formats": "json,csv,pdf",
  "analytics_retention": "never_executed",
  "analytics_version_watch": "never_checked",
  "analytics_facebook_insights": "no_connected_account",
  "analytics_instagram_insights": "no_connected_account",
  "analytics_tiktok": "no_connected_account",
  "analytics_version_watch_fetch": "recorded_table_only"
}
```

(`/version` on this machine today, abbreviated to the analytics keys. With accounts connected the three
platform values become `available`, `needs_user_consent`, `requires_additional_permission`,
`requires_scope_user_info_stats` or `requires_scope_video_list` depending on the scopes actually
granted — never a guess from the platform name.)

The `api` field moved from `"phase-8"` to `"phase-8.24"`; two older tests asserted the literal
`"phase-8"` and were updated to the new value, because the field exists to identify the running build.

## Consequences

* A missing fact is reported as missing (`observed: false`, `reason: "never_run"`), never as a zero and
  never as a green tick. A diagnostics screen that cannot be wrong is a diagnostics screen nobody
  reads.
* The April 2026 category-B reality — no connected Meta Page, no Instagram professional account, no
  approved TikTok app — shows up in `/diagnostics` as three permission states with reasons, not as a
  failure of the software.
* Nothing in this endpoint mutates state except `POST /versions/check`, which only appends an
  observation.
