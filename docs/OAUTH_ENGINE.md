# OAuth engine

The engine turns "the user wants to connect an account" into exactly one exchangeable
authorization code, and refuses everything else.

## The two halves

`POST /social/oauth/start` → authorization URL, `oauth_session` row, state hash.
`GET  http://127.0.0.1:47821/oauth/callback/<route>` → consumed session, code exchange, accounts.

Both halves live in `services/social/oauth_engine.py`. The callback is *not* a route on the API
port: it arrives on the dedicated listener (`services/social/callback_server.py`) which forwards the
query to `engine.handle_callback(route, params)` on the app event loop.

## State

| Property | Value | Why |
| --- | --- | --- |
| Source | 32 bytes from `secrets.token_bytes` | 256 bits, no predictable pattern |
| Encoding | base64url, `=` stripped | URL-safe, 43 characters |
| Stored | `sha256(state)` hex in `oauth_session.state_hash` (UNIQUE) | a stolen database read cannot forge a callback |
| Lifetime | 600 s (`social_oauth_state_ttl_s`) | short enough that a leaked URL ages out |
| Uses | one | consumed before the code exchange |
| Bound to | provider + route | a TikTok state cannot complete a Facebook session |

Consumption is a conditional `UPDATE ... WHERE consumed_at IS NULL AND status = 'pending'`. Two
concurrent callbacks race on one row; exactly one wins, the loser gets `STATE_CONSUMED`. The
exchange then happens *after* the row is marked, so a crash mid-exchange leaves a session that
cannot be replayed — the user starts again, which is the safe failure.

The authorization URL's `state` value is also a **boolean** that the GUI shows: the reconnect
wizard prints the URL only inside a collapsed `<details>`, and every transaction is auditable by
session id afterwards.

## PKCE

| Provider | PKCE |
| --- | --- |
| Facebook Login | not used — Facebook's desktop flow does not document PKCE; sending a challenge it ignores would be theatre |
| Instagram Login | not used — the flow has no PKCE parameter |
| TikTok Login Kit | **mandatory**, S256 |

For TikTok the verifier is 96 characters (`secrets.token_urlsafe(64)[:96]`, within RFC 7636's
43–128), stored as a secret with purpose `oauth_pkce_verifier`, and the provider receives only the
S256 challenge. The verifier never appears in the authorization URL, in a log, or in an API
response. A TikTok start without PKCE raises `PKCE_REQUIRED` rather than silently downgrading.

## Redirect URI

Registered per route and validated three times:

1. on app registration (`apps.validate_redirect_uri`): scheme `http`, host `127.0.0.1` exactly,
   explicit port 1024–65535, path `/oauth/callback/<route>`, no query, no fragment;
2. on start: the stored value is what the provider receives, byte for byte;
3. on callback: the returned `redirect_uri` (when a provider echoes it) is compared with
   `hmac.compare_digest`.

`localhost` is rejected at step 1 with an explicit message, because providers compare strings.

## Callback handling

```
GET /oauth/callback/<route>?code=…&state=…
 ├─ route must be a known OAuthRoute                     else STATE_ROUTE_MISMATCH
 ├─ error query param present?                           else oauth_callback_error(code, description)
 ├─ state shape (32–200 chars)                           else STATE_INVALID
 ├─ session lookup by sha256(state)                      else STATE_INVALID
 ├─ provider/route match                                 else STATE_ROUTE_MISMATCH
 ├─ expired?                                             else STATE_EXPIRED
 ├─ already consumed?                                    else STATE_CONSUMED
 ├─ conditional UPDATE → consumed                        (the race is here)
 ├─ read client id + secret + PKCE verifier from the vault
 ├─ exchange the code at the provider
 ├─ normalise the token bundle (provider-specific long-lived exchange)
 ├─ discover accounts (Pages / IG professional account / TikTok creator info)
 ├─ plan: claimable · unsupported · duplicates            (account_discovery.plan_discovery)
 ├─ per claimable account: store tokens in the vault, write `social_account`,
 │                        evaluate capabilities, write snapshot, audit
 └─ session → succeeded (or failed with the provider's code)
```

The listener answers the browser with a fixed HTML page: "Connected" or "Not connected", the
reason in words, and a line telling the user to return to CreatorOS. The authorization code is
never interpolated into that page.

Accounts that the provider returned but that cannot be published to (a personal Facebook profile,
a Page-less Instagram account) are recorded in `transaction.unsupported` with a reason instead of
being silently dropped or optimistically claimed.

## Transactions over the API

`GET /social/oauth/transactions` and `GET /social/oauth/transactions/{id}` expose the same session
the callback mutates, so the wizard can poll while a browser tab is open:

```json
{"session_id": "…", "status": "pending|succeeded|failed|expired|cancelled",
 "accounts": [{"id": "…", "label": "…", "capabilities": {"facebook.page.publish_text": "available"}}],
 "error_code": null, "unsupported": [...]}
```

`POST /social/oauth/transactions/{id}/cancel` marks a pending session cancelled; the state can never
be used afterwards. Abandoned sessions expire on their own and are swept when a new one is created.

## Permission upgrade (P8.20)

The same engine and the same listener run a **re-consent**, bound to one existing account:

1. `POST /analytics/consent/{account_id}` builds a plan (`services/social/consent.py`) and calls
   `engine.start(..., account_id=..., purpose="analytics_consent", context={...})`.
2. `start` refuses when the account's platform or login route differs from the requested one
   (`PLATFORM_MISMATCH` / `ROUTE_MISMATCH`) and refuses an unknown account (`ACCOUNT_NOT_FOUND`).
   The transaction row records `account_id` and, in `result_json.upgrade`, the provider account id,
   login path, existing credential refs, existing scopes, required/missing/requested scopes.
3. The callback branch `OAuthEngine._complete_upgrade` runs instead of the connect discovery:
   * the provider identity is checked **first** — a different account is reported
     `ACCOUNT_IDENTITY_MISMATCH` and nothing is changed;
   * the new credential is stored and the account updated through `AccountService.claim`, which
     writes the new secret *before* revoking the old one;
   * the stored scopes are the union of the account's, the provider's answer and the account's own
     reported scopes, so a grant is never forgotten;
   * capabilities are re-evaluated from the verified facts and a snapshot is written
     (`trigger="analytics_consent"`), and the outcome is `capability_available` or
     `capability_still_blocked` — a successful OAuth exchange alone never marks analytics available.

A denial (`AUTHORIZATION_DENIED`) never disconnects: `_fail` records the session and
`analytics.consent.denied` is audited against the account, whose credential and scopes are untouched.
A vault failure during adoption is reported and also leaves the old credential in place, so the user
can try again. State, PKCE, single-use consumption and the loopback callback are unchanged.

## Failure codes

| Code | Meaning | Who must act |
| --- | --- | --- |
| `STATE_INVALID` | unknown or malformed state | user restarts the flow |
| `STATE_EXPIRED` | past `social_oauth_state_ttl_s` | user restarts |
| `STATE_CONSUMED` | replay of a used callback | nothing; the first one already worked |
| `STATE_ROUTE_MISMATCH` | state belongs to another provider/route | developer: wrong callback URL |
| `REDIRECT_MISMATCH` | provider returned a different redirect | developer: console value mismatch |
| `AUTHORIZATION_DENIED` | user pressed cancel at the provider | user |
| `APP_NOT_CONFIGURED` | no client id/secret for this route | user: App settings |
| `VAULT_UNAVAILABLE` | no DPAPI vault | shell not running; nothing is stored |
| `SCOPE_NOT_GRANTED` | provider withheld a requested scope | user: re-consent |
| `RATE_LIMITED` | provider throttled the exchange | retry later |
| `NETWORK_ERROR` | the request never reached the provider | retry |

Full provider mapping lives in `integrations/social/errors.py`; `FACEBOOK_ERROR_MAP` covers
Graph's numeric codes (4/17/32/613 → `RATE_LIMITED`, 10/200 → `SCOPE_NOT_GRANTED`, 190/102/463/467 →
`TOKEN_EXPIRED`, 458/459 → `APP_REVIEW_REQUIRED`) and `TIKTOK_ERROR_MAP` covers TikTok's string
codes. An unrecognised body becomes `PROVIDER_ERROR` with the raw code in `detail` — guessing
"probably rate limiting" is how retry storms start.

## What is not here

* No implicit flow, no password grant, no embedded webview, no cookie harvesting.
* No refresh-token exchange on the callback path; refreshes are their own journalled operation
  (`TOKEN_LIFECYCLE.md`).
* No automatic retry inside the callback. The browser is waiting; a slow retry is worse than a
  clear error the user can act on.
