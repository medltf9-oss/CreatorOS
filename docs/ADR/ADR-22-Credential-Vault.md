# ADR-22 — Credential vault in the Electron main process, behind a scoped broker

**Status:** Accepted, implemented and tested (Phase 6)
**Supersedes:** nothing. **Implements:** ADR-07 (until now "accepted, not implemented").
**Date:** 2026-11-23

## Context

Phase 6 must store provider app secrets and OAuth tokens on disk. The options were:

1. a key in the Python backend and AES-GCM with a key file;
2. `keyring` / Windows Credential Manager from Python;
3. DPAPI through Electron's `safeStorage`, in the Electron main process.

Options 1 and 2 both mean the *backend* can decrypt with a key it can read, which makes the
"backend never holds a key" property false and puts a usable key on disk next to the ciphertext.
Option 3 keeps the key inside the OS protection tied to the user profile and lets the process that
already runs as the app's UI own it.

The remaining design question was how a Python process gets a value when it needs one.

## Decision

* Encryption and decryption happen **only** in the Electron main process (`electron/vault.cjs`),
  using the **asynchronous** `safeStorage` API (`encryptStringAsync` / `decryptStringAsync`). The
  deprecated synchronous methods are never called; the vault refuses to initialise without the async
  API.
* Records are files under `<DATA_ROOT>\config\secrets\<ULID>.bin`, with a `CRVAULT1` header and a
  JSON envelope carrying `alg`, `bytes`, `purpose`, `provider`, `alias` and a `sha256` of the
  ciphertext. The digest is checked before decryption so corruption is reported, not decoded.
* Writes are atomic: temp file in the same directory, then `renameSync`.
* The backend reaches secrets through a **loopback broker** (`electron/vault-broker.cjs`) on an
  ephemeral port, authenticated by a **per-launch random token** passed only to the backend child
  process. Every request names one id and one purpose; there is no list, no enumeration, no bulk
  read. Purpose must match the envelope. 120 requests/minute, 16 KiB body cap, single-use grants with
  a 5–300 s TTL, a 200-entry audit ring with no values, and `no-store` on every response.
* The renderer has **no decrypt handle**. `preload.cjs` exposes `vaultHealth`, `vaultStore`,
  `vaultDelete` and `openAuthUrl`; `vaultStore` returns metadata only.
* **No plaintext fallback.** If the vault is unavailable, the API reports `VAULT_UNAVAILABLE`,
  nothing is written, the account stays unconnected, and the GUI says so. `InMemoryVault` exists in
  the test suite and writes no file.
* How a value gets in: the renderer reserves an id (metadata only), asks the shell to seal it, then
  the backend **adopts** the record (verifying it exists and matches the purpose) and **links** it to
  the app registration. The value therefore never appears in a request body to Python on that path.
  A script-facing path (`POST /provider-apps/{p}/{r}/secret`) still exists, and the documentation
  says plainly that it hands the value to the backend once — the backend has to hold the plaintext in
  memory to compute `appsecret_proof` and to exchange codes, so claiming otherwise would be false.

## Consequences

**Good**

* The backend has no key material, now or later: `services/social/vault.py` speaks HTTP, nothing more.
* A compromised renderer cannot read a credential; the only bridge calls write or delete.
* Purpose binding means possessing an id does not give a value: an access token cannot be read as an
  app secret.
* Corruption, truncation and a changed DPAPI backend are detected, quarantined and reported instead
  of producing garbage or a silent re-auth loop.
* The audit ring makes "who read this secret, when, for what purpose" answerable without recording
  anything sensitive.

**Costs and limits**

* CreatorOS cannot run headless: no shell, no vault, no connections. That is the intended trade.
* DPAPI is **user**-scoped. It protects against other Windows users and offline disk access, and it
  does **not** protect against malware running as this user. The GUI states this verbatim; claiming
  otherwise would change what the user is willing to do on the machine.
* A user who loses their Windows profile loses the credentials and must reconnect.
* Two processes must both be alive for a connection to complete (shell + backend), which is one more
  thing to explain in `OAUTH_TROUBLESHOOTING.md`.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| AES-GCM with a key file in `config/` | the key sits next to the ciphertext; "encrypted" becomes decorative |
| `keyring`/Credential Manager from Python | the backend can decrypt at will; no scoped, audited round trip |
| Store secrets in SQLite | contradicts "no secret in the database" and defeats the leak scan |
| Pass the vault token to the renderer too | one XSS away from reading every token |
| Plaintext file "for development" | the first thing that ships to a user by accident |

## Evidence

* `app/desktop/electron/vault.test.cjs` — 18 tests, `node --test`: round trip, tamper, truncation,
  decrypt failure preserving the record, rotation, deletion, corruption → quarantine, plaintext scan,
  broker auth/purpose/rate-limit/grant/audit, no enumeration.
* `app/backend/tests/test_social_vault_client.py` — 12 tests: no broker → `UnavailableVault` → refused
  write; metadata-only `secret_ref`; lost record → `unavailable` + `CREDENTIAL_UNAVAILABLE`.
* `app/backend/tests/test_social_secret_adoption.py` — 16 tests: reserve → seal → adopt → link, purpose
  mismatch, unreadable id, idempotent adoption, previous secret revoked only after the new one is
  linked, app deletion revoking the blob.
* `docs/CREDENTIAL_VAULT.md`, `docs/SOCIAL_SECURITY.md`.
