# Phase 6 — Social accounts, OAuth and the credential vault

**Goal:** connect Facebook Pages, Instagram professional accounts (two login paths) and TikTok
accounts through official OAuth, store every secret encrypted with Windows DPAPI in the
Electron main process, and know exactly what each account can and cannot do — with evidence.
**No publishing, no uploads, no messages.** Publish paths in this phase return typed
`NOT_IMPLEMENTED` / `NOT_ELIGIBLE`.

Status legend: ☐ todo · ◐ in progress · ☑ done

---

## 1. What already exists (inspected, not rebuilt)

| Thing | Where | Reuse decision |
|---|---|---|
| Session token, origin allowlist, headers, envelopes | `core/security.py`, `main.py` | reused unchanged; OAuth callback is a separate loopback listener with its own rule |
| IPC signature (`x-creatoros-ipc`) for main→backend calls | `api/ipc_auth.py` | pattern reused for the **reverse** direction (vault broker) |
| Tables `secret_ref`, `social_account`, `oauth_session`, `capability_snapshot`, `audit_event` | `db/models/platform.py` | extended by one migration; no duplicates |
| `ConnectionStatus`, `AccountType`, `CapabilityState` enums | `db/enums.py` | `CapabilityState` gains the Phase 6 normalized states |
| Durable queue, audit, backups, approval service | `services/*` | untouched |
| Electron main/preload boundary, `CREATOROS_*` env hand-off | `app/desktop/electron/*` | extended with the vault module, broker and 3 new IPC handles |
| 111-agent workforce + tool grants | `agents/catalog.py`, `services/registry.py` | 2 new **read-only** tools, granted to 4 departments |

Nothing in Phase 1–5 is rewritten. `/version` moves to `phase-6` / `0.6.0`.

## 2. Architecture

```
Renderer (sandboxed, no Node)
   │  window.creatoros.saveProviderApp(...)   ← narrow IPC, secrets go one way only
   ▼
Electron main
   ├── vault.cjs        safeStorage async (DPAPI) → config/secrets/<id>.bin, versioned records
   ├── vault-broker.cjs 127.0.0.1:<ephemeral> + per-launch token → put/get/delete/rotate/health
   └── spawns backend with CREATOROS_VAULT_URL / CREATOROS_VAULT_TOKEN
   ▼
Python backend (holds no key, never writes plaintext to disk)
   ├── integrations/social/{base,oauth,facebook,instagram,tiktok,capabilities,account_discovery}
   ├── services/social/{accounts,tokens,vault_client,app_config,rotation}
   ├── api/routes/social.py        (accounts, oauth, capabilities, tokens, vault, app config)
   └── oauth callback listener     127.0.0.1:<fixed port>/oauth/callback/<provider>
```

**Why the broker runs in main:** ADR-07 says encryption/decryption happens in the Electron main
process. The backend cannot decrypt, so token use requires a deliberate round trip that main
can scope, rate-limit and audit. The renderer has no decrypt handle at all.

## 3. Tasks

| # | Task | Definition of done | Tests |
|---|---|---|---|
| 6.1 | `docs/PHASE6_PLAN.md` (this file) | committed before code | — |
| 6.2 | Vault (`electron/vault.cjs`) | async safeStorage only; versioned record `{v,schema,id,alg,created_at,rotated_at,bytes,sha256,data}`; atomic write; integrity check; corrupt record quarantined; `shouldReEncrypt` rotation; no plaintext ever written | `vault.test.cjs`: round trip, tamper, key rotation, missing vault, deletion, plaintext scan |
| 6.3 | Vault broker (`electron/vault-broker.cjs`) | loopback only, per-launch token, purpose allowlist, per-call scoping, 1 secret per response, TTL on grants, rate limit, audit without values | `vault-broker.test.cjs`: no token, wrong token, bad purpose, unknown id, audit |
| 6.4 | Main wiring + preload | `vaultHealth`, `saveProviderApp`, `clearProviderApp`, `openAuthUrl` (https + host allowlist); secrets never returned | manual + node test |
| 6.5 | `VaultClient` + doubles (Python) | broker client; `VAULT_UNAVAILABLE` when no broker; `InMemoryVault` only under `CREATOROS_ALLOW_TEST_VAULT=1`; never plaintext at rest | `test_social_vault_client.py` |
| 6.6 | OAuth engine | 256-bit state stored **hashed**, single use, 10 min TTL, provider+route binding, PKCE S256 (TikTok required, others per docs), callback listener on 127.0.0.1 with exact path, code never logged, denial/cancel/expiry/replay paths | `test_social_oauth.py` (14+) |
| 6.7 | Facebook adapter | authorize, code exchange, long-lived exchange, `debug_token`, `/me/accounts` with page tasks + token, page picture, revoke, error classification | `test_social_facebook.py` |
| 6.8 | Instagram adapters | `instagram_login` and `facebook_login` routes, profile, professional-account detection, linked Page, scopes | `test_social_instagram.py` |
| 6.9 | TikTok adapter | Login Kit + PKCE, token exchange, user info, refresh **with rotation**, revoke, creator-info probe (read-only) | `test_social_tiktok.py` |
| 6.10 | Capability engine | 8 normalized states; per-capability records with reason, required/granted scopes, api version, evidence; snapshot history | `test_social_capabilities.py` (table-driven) |
| 6.11 | Token lifecycle | states valid/expiring_soon/expired/refresh_required/refresh_in_progress/refresh_failed/revoked/reauthorization_required/credential_unavailable; per-account refresh lock; rotation journal; crash recovery | `test_social_tokens.py` |
| 6.12 | Accounts service | multi-account, dedupe by (platform, provider id, route), rename, group, disconnect with revoke, reauthorize, verify | `test_social_accounts.py` |
| 6.13 | Migration | extend 4 tables, add `provider_app_config`, `account_capability`, `credential_rotation`; real-DB copy test, FKs, integrity, downgrade | `test_social_migration.py` |
| 6.14 | API | accounts/oauth/capabilities/tokens/vault/app-config; no secret in any response; callback accepts only pending transactions | `test_social_api.py` |
| 6.15 | Agents | `social.accounts.list`, `social.capabilities.read` (read-only, no secrets); grants to platform/publishing/quality/strategy | `test_agent_social_tools.py` |
| 6.16 | GUI | 6 pages under **Social accounts**: Connected accounts, Connect wizard, Account details, Permissions, Vault status, App settings | screenshots |
| 6.17 | Leak scan | DB file, logs, API bodies, error strings, backups — zero matches for synthetic secrets | `test_social_leaks.py` |
| 6.18 | Docs | 12 documents + ADR-22/23/24 + `docs/evidence/P6.md` | reviewed |
| 6.19 | Acceptance | full suite, ruff, tsc, build, diagnose, migration on real DB copy, live checks | `docs/evidence/P6.md` |

## 4. Honest limits stated up front

* **No live provider account is connected** unless the user has created the developer apps and
  supplied credentials. Automated tests use `httpx.MockTransport` fixtures that are clearly
  marked `test_only`. Live provider checks are opt-in per account, inside the app: *Verify now* and
  *Run diagnostics* on the account detail page are the only paths that contact a provider with a
  real credential, so nothing in CI can pretend to have done it. No automated live test exists and
  none is claimed.
* **DPAPI is user-scoped.** It protects against other Windows users, not against malware running
  as the same user. Documented in `CREDENTIAL_VAULT.md`; the vault is not described as malware
  protection.
* **TikTok Direct Post** is capability-gated: login succeeding never implies posting approval.
* **No publish call exists** in this phase; the platform adapters expose read/introspect/refresh
  only, and a test asserts the adapter modules contain no publish endpoints.
