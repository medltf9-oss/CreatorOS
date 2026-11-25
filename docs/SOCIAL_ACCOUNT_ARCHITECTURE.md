# Social account architecture

Phase 6 gives CreatorOS the ability to **hold** social accounts: register a developer app, run an
OAuth round trip in a browser, store the credential in the DPAPI vault, discover what the provider
actually granted, and keep the token alive. It does not publish anything. Phase 7 does that, on
top of the facts this phase records.

```
┌──────────────────────────── renderer (Chromium) ───────────────────────────┐
│ 6 GUI pages: connected accounts · connect wizard · account detail ·        │
│              permissions · vault status · app settings                     │
│ Never holds a credential. No decrypt handle exists in preload.cjs.          │
└───────────┬─────────────────────────────────────────────┬──────────────────┘
            │ fetch + Bearer <per-launch token>            │ IPC (contextBridge)
            ▼                                              ▼
┌───────────────────────────────┐              ┌──────────────────────────────┐
│ backend (FastAPI, loopback)   │              │ Electron main process        │
│ /social/*  — 30 routes        │              │ vault.cjs  (safeStorage)     │
│ services/social/*             │              │ vault-broker.cjs (127.0.0.1) │
│ integrations/social/*         │              └──────────┬───────────────────┘
│ SQLite  (single DB, WAL)      │◄────────────────────────┘
└───────────┬───────────────────┘   one secret per scoped,   │ DPAPI ciphertext
            │                       audited, rate-limited    ▼
            │                       round trip             <DATA_ROOT>\config\secrets\*.bin
            ▼
      providers (Graph v23.0 · graph.instagram.com · open.tiktokapis.com/v2)
            ▲
            │ redirect: http://127.0.0.1:47821/oauth/callback/<route>
            │
      separate stdlib listener (ThreadingHTTPServer, GET only, fixed HTML page)
```

## Why the OAuth callback is its own server

Providers compare the redirect URI **byte for byte** against the value registered in the developer
console. The API port is ephemeral (chosen at launch), so a redirect pointing at it could never be
registered. The callback therefore lives on a fixed loopback address:

```
http://127.0.0.1:47821/oauth/callback/facebook_login
http://127.0.0.1:47821/oauth/callback/instagram_login
http://127.0.0.1:47821/oauth/callback/tiktok_login
```

The listener is a `ThreadingHTTPServer` bound to `127.0.0.1`, it answers `GET` on exactly one path,
it never echoes the authorization code into the page (which would put a credential in browser
history and in any screenshot), and it hands the query to the engine on the app's event loop.
`localhost` is rejected on purpose: `localhost` and `127.0.0.1` are different strings to a
provider's exact-match check.

## Layers

| Layer | Location | Responsibility |
| --- | --- | --- |
| Provider adapters | `integrations/social/{facebook,instagram,tiktok}.py` | HTTP shapes, scope names, error mapping. No database access. |
| Pure rules | `integrations/social/capability_rules.py` | Stored facts → capability verdicts. No I/O, fully unit-testable. |
| Account discovery | `integrations/social/account_discovery.py` | Claimable / unsupported / duplicate planning. Pure. |
| OAuth engine | `services/social/oauth_engine.py` | State minting and consumption, code exchange, account claim transaction. |
| Secret store | `services/social/secrets.py` | The only module that asks the vault for a value. |
| Token service | `services/social/tokens.py` | Status computation and the journalled refresh. |
| Accounts | `services/social/accounts.py` | Claim, rename, disconnect, token-problem marking. |
| Capabilities | `services/social/capabilities.py` | Records verdicts and their snapshot history. |
| Provider apps | `services/social/apps.py` | Client id/secret registration, redirect and scope validation. |
| API | `api/routes/social.py` | 30 authenticated routes. No response contains a secret. |

## Data model

Five tables carry Phase 6 state, all added by migration `f6a7b8c9d0e1` (ALTER-only for existing
tables, plus one table rebuild):

* `provider_app_config` — one row per (provider, route): client id, redirect URI, scopes, Graph
  version, app mode, and `client_secret_ref_id` pointing at the vault metadata row.
* `social_account` — one row per provider account, with `route`, `internal_label`, `group_label`,
  `token_state`, `api_version`, `scope_source` and `meta_json`.
* `account_capability` — one row per (account, capability): state, reason, required/granted/missing
  scopes, verification time, API version.
* `capability_snapshot` — immutable history of each evaluation, with its trigger.
* `oauth_session` — pending/completed attempts, with `state_hash` (unique) and
  `code_verifier_ref_id`; no plaintext state or verifier.
* `credential_rotation` — the refresh journal.

`secret_ref` gained `purpose`, `schema_version`, `status`, `byte_size`, `unavailable_reason`,
`last_verified_at` and `last_used_at`. It has never held a value and still does not.

## The rule this phase is built on

> A control works, or it is disabled with a stated reason. Nothing in between.

Every capability cell, every token state, every disabled button in the GUI comes from stored facts
and carries a reason string. `/version` reports the honest state of the subsystem:

```json
{"oauth": "...", "social_accounts": "...", "credential_vault": "vault_unavailable",
 "oauth_callback_listener": "callback_listener_stopped", "publishing": "not_implemented"}
```

`publishing` is `not_implemented` and stays that way until Phase 7.

## What is deliberately absent

* No publishing, uploading, posting or messaging code path exists.
* No social password is ever requested; no browser session is automated; nothing is scraped.
* No manual "create account" endpoint: an account exists only after a provider returned a token
  (`POST /accounts` is 405 for that reason).
* No plaintext fallback for the vault. Without the desktop shell, `/social/vault/*` is unavailable,
  connecting is refused with `VAULT_UNAVAILABLE`, and the GUI says so.
* No second database, queue, auth system or secret manager.

## Related documents

* `CREDENTIAL_VAULT.md` — the vault and its broker.
* `OAUTH_ENGINE.md` — state, PKCE, callback handling.
* `ACCOUNT_CAPABILITIES.md` — the vocabulary and why it is canonical.
* `TOKEN_LIFECYCLE.md` — expiry, refresh, the rotation journal.
* `FACEBOOK_INTEGRATION.md`, `INSTAGRAM_INTEGRATION.md`, `TIKTOK_INTEGRATION.md` — per provider.
* `SOCIAL_SECURITY.md` — the threat model and the leak tests that enforce it.
* `DEVELOPER_APP_SETUP.md` — what to create in each console.
* `OAUTH_TROUBLESHOOTING.md` — what each error code means and what to do.
* `SOCIAL_ACCOUNTS_GUI.md` — the six pages.
* `ADR/ADR-22-Credential-Vault.md`, `ADR-23-OAuth-State.md`, `ADR-24-Capability-Vocabulary.md`.
