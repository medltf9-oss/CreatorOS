# Publishing troubleshooting

Find the destination in **Publishing → Needs you** (or `GET /publishing/attention`). Every row carries
the backend's own `state_reason`, and `GET /publishing/jobs/{id}` has the attempt, delivery and
reconciliation ledgers. Work from the state, not from a guess.

## The state says blocked

Something local is wrong and nothing was sent. `detail` in the preflight issue names it:

| `state_reason` | Fix |
| --- | --- |
| `ACCOUNT_NOT_PUBLISH_CAPABLE` | reconnect the account, or grant the missing scope (`docs/ACCOUNT_CAPABILITIES.md`) |
| `NO_MEDIA` | attach an asset to the intent, or add one to the destination |
| `MEDIA_NEEDS_TRANSCODE` | run the suggested profile in the Transcode studio |
| `DELIVERY_NOT_CONFIGURED` (Instagram) | configure the bucket (`docs/MEDIA_DELIVERY.md`) |
| `VAULT_UNAVAILABLE` | the desktop shell's vault broker is not answering; restart the app |

## It needs me

| `state_reason` | Meaning | What to do |
| --- | --- | --- |
| `missed_schedule` | the time passed while the app was closed | publish now, or reschedule |
| `missed_schedule_skipped` | same, under the `skip` policy | reschedule |
| `approval_revoked` / `approval_invalidated` | the content changed after approval | request approval again |
| `reconciled_remote_absent` | the provider says it has no such object | decide whether to publish again |
| `retry_requires_approval:*` | a retry needs fresh consent | approve, then retry |

## It says unknown remote state

Do **not** retry. Press **Check provider** (`POST /publishing/destinations/{id}/reconcile`). Three
outcomes:

* it exists → the destination becomes `published` with the permalink;
* it does not exist → `requires_user_action`, and you decide whether to publish again;
* it still cannot say → the case stays open; check again later, or resolve it by hand with a note.

## It failed

`error_category` on the attempt tells you what kind of failure it was:

| Category | Meaning |
| --- | --- |
| `transient` | the attempt budget (3) was spent on timeouts/5xx/429; retry is reasonable |
| `permission` | a scope or app permission is missing |
| `authorization` | the token was rejected; reauthorize (`docs/TOKEN_LIFECYCLE.md`) |
| `validation` | the provider rejected the content itself; fix the content |
| `permanent` | the provider will not accept this; read the response excerpt |
| `ambiguous` | see "unknown remote state" above |

Retry re-checks the approval first. A consumed or stale approval cannot be reused, so the destination
goes back to `draft` and you approve the current content — that is not a bug, it is consent.

## The upload stops at a percentage

Check the lease (`GET /publishing/jobs/{id}` → `lease`). If a worker died, the lease expires and the
queue recovers the job; a resumed upload continues from the provider's offset, not from zero. If a
TikTok upload URL expired (401/403), that is classified as transient and the next attempt starts a fresh
session — publishing nothing by itself.

## Instagram is blocked

That is the honest state when no bucket is configured (ADR-28). There is no localhost workaround in this
codebase, by design. Configure the bucket, then revalidate the destination.

## Where to look next

* `GET /publishing/jobs/{id}/audit` — every action on this destination, with actor and outcome.
* `<DATA_ROOT>\logs\backend\creatoros.log` — the queue's own errors, with correlation ids.
* `python scripts/publish_readiness.py` — why each platform can or cannot publish right now.
