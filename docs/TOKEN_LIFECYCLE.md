# Token lifecycle

One vocabulary for nine very different provider behaviours, computed from stored facts, plus a
refresh that survives being interrupted halfway.

## States

`TokenState` (10 values, `db/enums.py`). `TOKEN_NEEDS_ATTENTION` marks the six that require a human.

| State | Meaning | Can use now | Can refresh |
| --- | --- | --- | --- |
| `valid` | stored token is live and not close to expiring | yes | yes (if a refresh token exists) |
| `expiring_soon` | expires within `social_token_expiry_warn_hours` (6 h) | yes | yes |
| `expired` | past expiry, and a refresh token exists | no | yes |
| `refresh_required` | the provider asked for a refresh, or a refresh was requested | no | yes |
| `refresh_in_progress` | a refresh holds the account lock | no | no (one at a time) |
| `refresh_failed` | the last refresh failed but the stored credential may still work | no | yes |
| `revoked` | revoked at the provider, or disconnected here | no | no |
| `reauthorization_required` | expired with no refresh token, or a refresh token that cannot be renewed | no | no |
| `credential_unavailable` | the vault record is missing or undecryptable | no | no |
| `unknown` | no facts recorded yet | no | no |

`compute_status(account)` is a pure function: expiry timestamps, the presence of a refresh
reference and the last recorded failure, nothing else. No network call happens to answer "what is
the state of this token?".

Two rules that exist because providers differ:

* **6-hour warning window.** TikTok access tokens live 24 hours. A 24-hour window would mark every
  healthy TikTok account `expiring_soon` permanently.
* **12-hour refresh dead zone.** A refresh token that itself expires within 12 hours cannot be
  used to rescue an expired access token (TikTok requires the refresh token to still be valid), so
  the account is moved to `reauthorization_required` *before* it silently breaks.

## Per-provider reality

| Provider | Access token | Refresh token | Renewal |
| --- | --- | --- | --- |
| Facebook Page | minted from a long-lived user token → no expiry | none | re-authorize the user token (long-lived extension) |
| Instagram Login (A) | long-lived ~60 days | none | `GET /refresh_access_token` extends the same token |
| Instagram via FB (B) | Page token | none | Page token from `/me/accounts` |
| TikTok | 24 h | 365 days, **rotates** | `POST /oauth/token/` `grant_type=refresh_token` |

Where a provider has no refresh token, `token.can_refresh` is `false` and the GUI disables the
button with "this account has no refresh token" as its title. A button that calls an endpoint that
cannot work is worse than a disabled one.

## The journalled refresh

`TokenService.refresh(account)`:

```
1. compute_status → refuse with REAUTHORIZATION_REQUIRED if the state cannot be refreshed
2. _begin(account):
     - reuse a live `credential_rotation` row, or insert one (pending, lock token, attempt count)
     - conditional UPDATE state='pending' → 'in_progress' WHERE lock matches
       (a second caller loses and gets REFRESH_IN_PROGRESS)
3. write `rotation_journal` secret: {rotation_id, account_id, old_ref_id, started_at, intent}
4. read the current refresh token from the vault
5. call adapter.refresh(...)
6. store the new access token (+ new refresh token when the provider rotates):
     - new secret_ref rows first
     - repoint account.token_ref_id / refresh_ref_id
     - revoke the old refs only after the new ones exist
7. rotation → completed, journal secret deleted, audit `token.refreshed`
   on failure: rotation → failed with the provider code; token state decided from what is stored
```

A stale lock (`social_refresh_lock_stale_s` = 300 s) is treated as abandoned, not as "in progress
forever". `resolve_interrupted(account)` is exposed as `POST /social/rotations/{account_id}/resolve`
and answers from stored facts:

* the account has a usable credential → the rotation is `abandoned`, the account keeps working;
* the account has no usable credential → `reauthorization_required`, and the GUI says so.

## What the API exposes

```
GET  /social/accounts/{id}/token          → {state, reason, expires_at, refresh_expires_at,
                                             has_refresh_token, can_refresh, can_use}
POST /social/accounts/{id}/token/refresh  → {account_id, token, rotated, message}
GET  /social/rotations                    → journal rows + pending count
POST /social/rotations/{account_id}/resolve
```

No response contains a token value. `rotated` says whether the provider issued a new refresh token
(TikTok: yes; Instagram: no, the same token was extended).

## Failure policy

| Situation | Result |
| --- | --- |
| provider says the refresh token is invalid | `reauthorization_required`, rotation `failed`, credential revoked locally |
| provider is unreachable | error propagated, rotation `failed`, token state unchanged (the stored token may still work) |
| the vault cannot decrypt the current token | `credential_unavailable`, `CREDENTIAL_UNAVAILABLE`, GUI tells the user to reconnect |
| a second refresh arrives while one runs | `REFRESH_IN_PROGRESS`, no second provider call |
| the process dies mid-refresh | rotation stays `in_progress`; resolved by `resolve_interrupted` after the stale window |

## Verified by tests

`tests/test_social_tokens.py` (15): state computation for every branch (including the 12-hour dead
zone), refresh success, refresh with rotation, refresh failure preserving the usable credential, the
conditional lock, stale-lock takeover, crash recovery through `resolve_interrupted`, and that no
token value appears in an audit row or API response.
