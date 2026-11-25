# Reconciliation: what actually happened at the provider

## Why this exists

Three providers, three ways to lose an answer:

* **Facebook** — the last chunk is accepted and the connection dies before the `finish` response, so
  `video_id` may or may not exist.
* **Instagram** — `media_publish` is called and the reply times out; the container may be published.
* **TikTok** — `publish_id` is created, then the status call fails.

At that moment the only honest state is `unknown_remote_state`. Retrying may double-post; marking it
failed is a lie; marking it published is also a lie.

## The three rules

`ReconciliationService.check()` asks the provider through the same publisher the executor used, records
what it saw, and applies exactly one rule:

| What the provider says | Decision | Destination |
| --- | --- | --- |
| the object exists (`ready`, `published`, `FINISHED`, `PUBLISH_COMPLETE`, `inbox`, …) | `remote_exists` | `published`, permalink taken from the provider |
| it does not exist (`missing`, `expired`, `failed`, `deleted`) | `no_remote_object` | `requires_user_action` (`reconciled_remote_absent`) — **not** resubmitted |
| anything else, including "still processing" | `undetermined` | stays `unknown_remote_state`, case left open |

A resubmit is refused on purpose: it re-spends an approval and can create a second post. The retry is a
human action, and it needs a live approval (`PublishActions.retry`).

## One place reads provider vocabularies

`observe()` in `services/publishing/reconciliation.py` is the only mapping from provider strings to
decisions. An unrecognised state is `undetermined`, never a guess, so a new Facebook status cannot
silently look like a success.

## Every look is recorded

`publish_reconciliation` rows carry `checked_at`, `provider_state`, `decision`, redacted `evidence_json`
and `resolved_by`. An undetermined look is still a row: "we looked and it was still processing" is
information, and its absence would be indistinguishable from never having looked.

A case is *open* only when the newest row for that destination has no `resolved_by` **and** the
destination is still `unknown_remote_state` or `needs_reconciliation`.

## Resolving by hand

```
POST /publishing/destinations/{id}/reconcile/resolve
{ "decision": "remote_exists" | "no_remote_object", "note": "I can see it on the Page" }
```

The note is mandatory, the row records `resolved_by`, and `undetermined` is rejected with
`UNDETERMINED_IS_NOT_A_RESOLUTION` — a person cannot resolve a case by saying "no idea".

The reason this exit exists: the provider API may be gone (a revoked app), or the post may have been
deleted in the provider's own app, which no API here can see.

## Unreachable is not absent

A transport error or an authorization failure during the check is recorded as `undetermined` with its
error code. "We could not ask" is different from "it is not there", and the GUI keeps them apart.

## Tests

`tests/test_publishing_reconciliation.py` (22 tests): the three-way state mapping across all three
providers' vocabularies, the absent case queueing nothing, still-processing keeping the state, an
unreachable provider, a destination with no provider id (`NOTHING_TO_RECONCILE`), manual resolution with
its note, open-case bookkeeping and idempotent re-checks.
