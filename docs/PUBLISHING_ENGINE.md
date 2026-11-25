# The publishing engine

Phase 7 turns a content intent into a publication on Facebook Pages, Instagram Reels or TikTok. This
document is the map: what the parts are, which one may call which, and where the rules live.

## The one diagram

```
 campaign ─┐
           ├─> content_project (intent) ─> platform_variant ─> publish_job ─> provider
 media ────┘        (D1: one intent    (per platform +         (the destination
                    = one piece of      account; holds the      ledger: state,
                    content)            caption for it)         attempts, ids)
                                                                      │
                        approval_record (hash-bound consent) ─────────┤
                        publish_delivery (cloud copy) ────────────────┤
                        publish_attempt  (one request) ───────────────┤
                        publish_reconciliation (one look) ────────────┘
```

Nothing in this project publishes from a variant directly: a variant is what the user wrote, a
`publish_job` is one destination and its history.

## Modules

| Path | Role |
| --- | --- |
| `db/enums.py` | 18 canonical states, `PUBLISH_TRANSITIONS`, `parse_publish_state` (strict) vs `normalize_publish_state` (boundary), error categories |
| `integrations/social/publishing/metadata/limits.py` | per-(platform, mode) rules with a documentation URL on every rule |
| `integrations/social/publishing/metadata/validators.py` | `validate_variant` → `ValidationReport` with typed findings |
| `integrations/social/publishing/media_delivery/*` | `MediaDeliveryProvider`, S3 SigV4, the unconfigured state |
| `integrations/social/publishing/publishers/*` | one class per (platform, mode): Facebook video, Instagram Reels, TikTok direct + inbox |
| `integrations/social/publishing/security.py` | SSRF allowlist for media and upload URLs |
| `services/publishing/composer.py` | campaigns, intents, destinations, source selection, revalidation |
| `services/publishing/approvals.py` | canonical payload, hash, request, decide, verify, consume |
| `services/publishing/delivery.py` | bucket configuration, upload, signed URL, retention |
| `services/publishing/executor.py` | **the only path to a provider** |
| `services/publishing/scheduler.py` | due, future, missed, timezone changes |
| `services/publishing/reconciliation.py` | unknown remote state → evidence → decision |
| `services/publishing/actions.py` | publish now / retry / cancel / reschedule, with their rules |
| `services/publishing/history.py` | redacted read models for the API and the GUI |
| `services/publishing/agent_grants.py` | which agents may read the queue and draft (never publish) |

## The lifecycle in one paragraph

A destination starts as `draft`. Editing moves it through `validation_pending`; asking for approval
moves it to `awaiting_approval`; a human decision makes it `approved`. Publishing (now, or at the
scheduled time) moves it to `ready`, where `validation_pending`/`awaiting_approval`/`approved` may
still arrive. The executor then walks `uploading → uploaded → processing → published`, or stops at
`blocked` (something local is wrong), `requires_user_action` (a person must decide), `failed` (attempts
exhausted) or `unknown_remote_state` (the answer was lost). `cancelled` and `published` are final.

## Who may do what

* **The executor** may call a publisher. Nothing else may.
* **The scheduler** may move a destination to `ready` and enqueue it.
* **Reconciliation** may read a provider and move a destination according to what it saw.
* **`PublishActions`** is the API's four verbs, so the GUI, an agent tool and a test all hit the same
  rules.
* **Agents** may read the queue and create drafts. They cannot approve, schedule a run, retry, cancel
  or publish; `test_agent_publishing_tools.py` fails if a tool ever imports the executor.

## Where the honest limits are

* Exactly-once is impossible. The guarantees are: no accidental duplicate within an intent, no blind
  resubmission after an ambiguity, reconciliation before any retry.
* Instagram cannot publish without a configured bucket (ADR-28). It says so in preflight, in
  `/version` and in the GUI.
* TikTok Direct Post needs the Content Posting API add-on; without it the capability reads
  `requires_developer_approval` and the inbox path remains.
* Provider processing time is the provider's. The GUI shows a status string, never an invented
  percentage.

## Related

`docs/PUBLISHING_API.md` · `docs/PUBLISHING_GUI.md` · `docs/SCHEDULING.md` ·
`docs/RECONCILIATION.md` · `docs/MEDIA_DELIVERY.md` · `docs/PLATFORM_LIMITS.md` ·
`docs/PUBLISHING_SAFETY.md` · `docs/PUBLISHING_AGENT_TOOLS.md` · `docs/LIVE_PUBLISHING_TESTS.md` ·
`docs/PUBLISHING_TROUBLESHOOTING.md`
