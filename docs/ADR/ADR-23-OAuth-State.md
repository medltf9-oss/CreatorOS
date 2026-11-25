# ADR-23 — Hashed, single-use, provider-bound OAuth state consumed before exchange

**Status:** Accepted, implemented and tested (Phase 6)
**Date:** 2026-11-23

## Context

The OAuth callback arrives on a loopback HTTP listener. Anything on the machine can reach that
listener, and the authorization code is a bearer credential for the user's account. The state
parameter is the only thing that ties a callback to a flow CreatorOS actually started, so its
handling decides whether a redirect can be forged, replayed, or borrowed from another provider.

The naive implementations all fail in ways that are invisible until they are exploited:

* a counter or a timestamp-based state is guessable;
* storing the state in plaintext means a database read (or a backup) yields a working state;
* "delete the row after exchange" loses the race: two callbacks can both pass the check before
  either deletes;
* a state valid for any provider lets a TikTok callback complete a Facebook session.

## Decision

1. **State value**: 32 bytes from `secrets.token_bytes`, base64url-encoded without padding (43
   characters).
2. **At rest**: only `sha256(state)` is stored, in `oauth_session.state_hash` with a **UNIQUE**
   constraint. The plaintext state is never written to the database, a log or an audit row.
3. **Binding**: the row carries `platform` and `route`. A callback whose state resolves to a session
   with a different provider or route is refused with `STATE_ROUTE_MISMATCH`.
4. **Lifetime**: `social_oauth_state_ttl_s` = 600 seconds, checked on every callback.
5. **Single use, raced correctly**: consumption is a conditional
   `UPDATE … SET consumed_at=…, status='consumed' WHERE id=… AND consumed_at IS NULL AND status='pending'`.
   Exactly one concurrent callback wins the row update; the others get `STATE_CONSUMED`. The exchange
   happens *after* the row is marked, so a crash mid-exchange leaves an unusable state rather than a
   replayable one.
6. **Exact redirect comparison**: the redirect URI is validated at registration (loopback,
   `127.0.0.1` — not `localhost` — explicit port, exact path, no query/fragment), sent byte for byte,
   and compared with `hmac.compare_digest` when the provider echoes it back.
7. **PKCE where the provider requires it**: TikTok gets an S256 challenge from a 96-character
   verifier stored in the vault with purpose `oauth_pkce_verifier`; the verifier never leaves the
   vault. Facebook and Instagram Login do not document PKCE, so no challenge is sent — sending one
   they ignore would be theatre.
8. **Cancellation**: a pending session can be cancelled explicitly; its state can never be used
   afterwards.

## Consequences

**Good**

* A database read, a backup, or a log dump cannot produce a working callback. An attacker needs the
  live state value from the authorization URL.
* Replay fails deterministically with a code the GUI can explain, instead of silently re-exchanging a
  code the provider will reject anyway.
* Cross-provider confusion is impossible: the state names its own platform and route.
* Abandoned flows clean themselves up after 10 minutes.

**Costs**

* A sha256 lookup means the row cannot be found by scanning for a value — intentional, but it makes
  manual debugging slightly harder; the API exposes the session id instead.
* Two callbacks for one flow (a double-click in the browser) produce one success and one
  `STATE_CONSUMED`, which the troubleshooting doc has to explain.
* One more secret purpose (`oauth_pkce_verifier`) to reason about in the vault.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Store state in plaintext | a DB read or backup yields a credential-equivalent value |
| HMAC-signed state with no row | signature alone cannot give single use or an auditable session |
| Random state, delete-then-exchange | delete-then-exchange is racy in the wrong order; conditional UPDATE is exact |
| Accept `localhost` as well as `127.0.0.1` | providers compare strings exactly; a mismatch is a support ticket, not a feature |
| Send PKCE to every provider | undocumented parameters are noise; the TikTok route is the one that requires it |

## Evidence

* `tests/test_social_oauth.py` — 24 tests: state shape and hashing, duplicate-state rejection,
  expiry, consumption, concurrent replay (one winner), route mismatch, redirect exact match, PKCE
  challenge for TikTok, callback error passthrough, cancel.
* `tests/test_social_tiktok.py` — 13 tests including "no challenge → `PKCE_REQUIRED`" and "no verifier
  → `PKCE_REQUIRED`".
* The generated authorization URL was inspected against a live-format provider URL (`client_id`,
  `redirect_uri`, `state`, `response_type`, `scope`) — recorded in `docs/evidence/P6.md`.
