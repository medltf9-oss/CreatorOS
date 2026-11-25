# The publishing API

49 routes under `/publishing`. All of them require the per-launch session token (ADR-06). Every error
uses the project envelope: `{ code, message, correlation_id, details? }`.

No response contains a token, an upload URL, a signed URL or a local file path; `test_publishing_api.py`
scans whole response bodies for them.

## Composer

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/publishing/overview` | counts by state, delivery state, schedule policy, accounts |
| GET | `/publishing/campaigns` | paged |
| POST | `/publishing/campaigns` | `name`, optional objective/description/dates |
| PATCH | `/publishing/campaigns/{id}` | |
| GET | `/publishing/intents` | filters: `campaign_id`, `state`, `platform`, `search` |
| POST | `/publishing/intents` | `title`, optional `campaign_id`, `source_asset_id`, `notes` |
| GET | `/publishing/intents/{id}` | project + its destinations |
| PATCH | `/publishing/intents/{id}` | |
| DELETE | `/publishing/intents/{id}` | refused if a destination is published or in flight |
| POST | `/publishing/intents/{id}/duplicate` | republication = new intent, old one kept |
| POST | `/publishing/intents/{id}/destinations` | creates a draft destination |

## Destinations

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/publishing/destinations/{variant_id}` | variant + job detail |
| GET | `/publishing/destinations/{variant_id}/actions` | each action with `enabled` and a `reason` |
| PATCH | `/publishing/destinations/{variant_id}` | `{fields: {...}}`; refused once `ready` |
| DELETE | `/publishing/destinations/{variant_id}` | |
| POST | `/publishing/destinations/{variant_id}/revalidate` | re-runs the validators, stores the report |

## Approval

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/publishing/destinations/{variant_id}/approval` | state, hashes, the exact payload |
| POST | `/publishing/destinations/{variant_id}/approval` | request (or refresh) consent |
| POST | `/publishing/destinations/{variant_id}/approval/decide` | `{approve, note}` |

An approval is bound to `sha256(canonical payload)`. Editing the caption, hashtags, options, schedule,
account or media invalidates it; the destination returns to `validation_pending` rather than keeping a
stale consent.

## Schedule and actions

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/publishing/destinations/{variant_id}/schedule` | aware timestamp + IANA zone; `null` clears |
| GET | `/publishing/schedule` | upcoming (14 days by default) + missed + policy |
| GET | `/publishing/schedule/policy` | `{policy, grace_seconds}` |
| PUT | `/publishing/schedule/policy` | `ask` \| `publish_now` \| `skip` |
| POST | `/publishing/schedule/tick` | one sweep, exactly what the thread does |
| POST | `/publishing/destinations/{variant_id}/publish-now` | preflight, then enqueue |
| POST | `/publishing/destinations/{variant_id}/retry` | needs a live approval; else back to draft |
| POST | `/publishing/destinations/{variant_id}/cancel` | refused once the post may exist remotely |
| POST | `/publishing/destinations/{variant_id}/publish-missed-now` | the answer to `ask` |

## Reconciliation

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/publishing/destinations/{variant_id}/reconcile` | ask the provider, decide, record |
| POST | `/publishing/destinations/{variant_id}/reconcile/resolve` | a person's decision + mandatory note |
| POST | `/publishing/reconcile/sweep` | check every unresolved destination (bounded) |
| GET | `/publishing/reconciliations` | open cases, with evidence |

## History

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/publishing/jobs` | filters: platform, account, campaign, project, state, mode, since, until, search |
| GET | `/publishing/jobs/{job_id}` | attempt, delivery, reconciliation and approval ledgers |
| GET | `/publishing/jobs/{job_id}/attempts` | |
| GET | `/publishing/jobs/{job_id}/deliveries` | |
| GET | `/publishing/jobs/{job_id}/reconciliations` | |
| GET | `/publishing/jobs/{job_id}/audit` | audit events for this destination |
| GET | `/publishing/history` | published only, with permalinks |
| GET | `/publishing/attention` | blocked / failed / needs-you / unknown |
| GET | `/publishing/queue` | in flight (with leases) and unknown, separately |

## Delivery

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/publishing/delivery` | configuration, health and the object ledger |
| PUT | `/publishing/delivery` | `credentials_secret_id`, never a key |
| DELETE | `/publishing/delivery` | removes configuration and revokes the sealed credential |
| POST | `/publishing/delivery/check` | asks the bucket whether it answers |
| POST | `/publishing/delivery/cleanup` | deletes expired objects, keeps ledger rows |

## Meta

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/publishing/limits?platform=&mode=` | the published limits, including `null` = "provider sets no cap" |
| POST | `/publishing/validate` | validate an unsaved draft; nothing is stored |
| GET | `/publishing/capabilities` | account capabilities from the capability engine |

## Error codes worth knowing

`NOT_PUBLISHABLE_NOW` · `NOT_RETRYABLE` · `REMOTE_STATE_IN_PLAY` · `NOTHING_TO_RECONCILE` ·
`UNDETERMINED_IS_NOT_A_RESOLUTION` · `APPROVAL_MISSING` · `APPROVAL_HASH_MISMATCH` ·
`APPROVAL_EXPIRED` · `DESTINATION_LOCKED` · `DESTINATION_EXISTS` · `INVALID_SCHEDULE` ·
`INVALID_TIMEZONE` · `INVALID_LATE_POLICY` · `DELIVERY_CREDENTIALS_REQUIRED` · `DELIVERY_BAD_CONFIG` ·
`ILLEGAL_STATE_TRANSITION` · `UNKNOWN_STATE`
