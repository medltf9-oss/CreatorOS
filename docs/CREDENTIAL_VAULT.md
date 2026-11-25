# Credential vault

Windows DPAPI, user scope, called only from the Electron main process. The backend holds no key.
The renderer has no decrypt handle. One secret per scoped, rate-limited, audited round trip.

## Components

| File | Runs in | Job |
| --- | --- | --- |
| `app/desktop/electron/vault.cjs` | Electron main | encrypt/decrypt, files, rotation, quarantine, health |
| `app/desktop/electron/vault-broker.cjs` | Electron main | loopback HTTP surface the backend uses |
| `app/desktop/electron/main.cjs` | Electron main | starts both, injects `CREATOROS_VAULT_URL/TOKEN/ROOT` into the backend child only |
| `services/social/vault.py` | backend | `BrokerVault` HTTP client; `UnavailableVault` when the shell is absent |
| `services/social/secrets.py` | backend | `secret_ref` metadata, the only place a value is read |

## Record format

```
<DATA_ROOT>\config\secrets\<26-char ULID>.bin
"CRVAULT1\n" + {"v":1,"id":"…","alg":"dpapi-safeStorage","created_at":"…","rotated_at":null,
                 "bytes":142,"sha256":"<hex of the ciphertext>","purpose":"access_token",
                 "provider":"facebook","alias":"account:facebook:01J…","data":"<base64 ciphertext>"}
```

* Only the **asynchronous** `safeStorage` API is used (`encryptStringAsync` /
  `decryptStringAsync` / `isAsyncEncryptionAvailable`). The deprecated synchronous methods are
  never called; the vault refuses to initialise without the async API.
* The `sha256` covers the ciphertext and is verified **before** a decrypt is attempted, so a
  truncated or edited file is reported as `VAULT_CORRUPT` instead of producing garbage.
* Writes go to `<id>.bin.tmp` and are moved into place with `renameSync` — the same volume, so the
  rename is atomic. A crash leaves either the old record or the new one, never half of each.
* Allowed purposes: `provider_app_secret`, `access_token`, `refresh_token`,
  `oauth_pkce_verifier`, `rotation_journal`, `token_metadata`. A ULID-shaped id is required; no
  path separators can reach the filesystem.
* Maximum value size: 64 KiB.

## Corruption handling

A record that fails to decode is moved to `config/secrets/quarantine/<id>.<timestamp>.corrupt` and
reported as `present: true, ok: false`. The backend then marks the `secret_ref` `unavailable` with
the vault's reason, sets the account's token state to `credential_unavailable`, and the GUI tells the
user to reconnect. The record is never silently deleted — the fact that a credential *was* lost is
information the user needs.

## Rotation

`shouldReEncrypt` detects a record written under an older schema version or by a different DPAPI
backend (for example after a Windows profile migration). The next `get` transparently decrypts and
re-encrypts it, bumps `rotated_at` and counts a `reencrypted` operation. No user action, no window
in which the value is written in plaintext.

## The broker

```
POST http://127.0.0.1:<ephemeral>/vault/{put|get|rotate|delete|verify|health|grant|audit}
headers: x-creatoros-vault: <per-launch token>   x-creatoros-vault-proto: 1
```

* Bound to `127.0.0.1` on an ephemeral port; the port and token are generated per launch and passed
  only to the backend child process. The renderer never sees the token, and no IPC handler exposes it.
* Token comparison is timing-safe. A missing or wrong token is `401 VAULT_UNAUTHORIZED`.
* Rate limit: 120 requests/minute per process.
* Request bodies are capped at 16 KiB; larger bodies are destroyed mid-stream.
* **No enumeration, no list, no wildcard, no bulk read.** Every read names one id *and* one purpose;
  the purpose in the request must match the purpose stored in the envelope (`VAULT_PURPOSE_MISMATCH`
  otherwise). Possession of an id is not enough to get a value.
* `POST /vault/grant` mints a single-use grant (TTL clamped to 5–300 s) that is consumed by the next
  matching read — used where a caller needs to read a value slightly later than it discovers the id.
* Audit ring of 200 entries holding `{operation, outcome, purpose, id, timestamp}` — never a value,
  never a prefix of a value. `GET /vault/audit` returns the last 50.
* Every response is `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `Cross-Origin-Resource-Policy: same-origin`.

## How a secret gets in

Two paths exist and both are used:

**A. Renderer seals it (default for provider app secrets).** The value never crosses the API.

```
renderer ──POST /social/vault/secrets/reserve──▶ backend     (metadata only: a reserved ULID)
renderer ──IPC creatoros:vaultStore────────────▶ Electron main ──safeStorage──▶ .bin
renderer ──POST /social/vault/secrets/{id}/adopt──▶ backend   (verifies the record exists,
                                                               records purpose/fingerprint/bytes)
renderer ──PUT /social/provider-apps/{p}/{r}/secret-ref──▶ backend (links it to the app)
```

`adopt` asks the broker `verify` for the id first: if the record is missing the call is refused with
`VAULT_NOT_FOUND`, so a typo cannot produce an app that looks configured. The fingerprint stored in
`secret_ref` is the first 32 hex characters of the ciphertext digest — it identifies a record without
revealing anything a decrypt would.

**B. Backend stores it** (`POST /provider-apps/{p}/{r}/secret`). Used by scripts and the API tests.
The value transits the authenticated loopback API once and is written through the broker
immediately. This is the honest description: the backend must hold the plaintext *in memory* anyway
to compute `appsecret_proof` and to exchange codes, so claiming it never sees the value would be
false. Path A exists so a GUI user's paste does not need to travel over HTTP at all.

## What DPAPI actually protects

Verbatim from `vault.health()`, and shown in the GUI:

> Windows DPAPI, current user. Protects against other Windows users and offline disk access; it does
> not isolate credentials from software running as this user.

Concretely:

* another Windows user on this machine cannot decrypt these files;
* copying `config/secrets` to another machine yields unreadable blobs;
* **malware running as this user can ask DPAPI to decrypt**, exactly as CreatorOS does — this is not
  isolation from your own session;
* losing the Windows profile loses the credentials; reconnect the accounts.

There is no plaintext fallback. If the vault is unavailable, connecting is refused
(`VAULT_UNAVAILABLE`), nothing is written, and the account stays unconnected. `InMemoryVault` exists
only in the test suite.

## Lifecycle rules

| Event | Vault action |
| --- | --- |
| connect succeeds | new `access_token` (+ `refresh_token`, + `oauth_pkce_verifier` for TikTok) written; old credential revoked **after** the new one is stored |
| token refresh | new values written, old refs revoked, `credential_rotation` row records the intent |
| app secret replaced | `attach_secret_ref` links the new id first, then revokes the previous one |
| app registration deleted | `ProviderAppService.delete` revokes the sealed secret before removing the row |
| account disconnected | provider revoke attempted (where an endpoint exists), then both refs revoked |
| account row deleted | explicit deletes: capabilities, snapshots, rotations — the `secret_ref` rows were already revoked at disconnect time |

A `secret_ref` row survives revocation on purpose: `status=revoked` plus a reason is the honest
record, and it is what makes "this credential existed and was destroyed" auditable.

## Verified by tests

`app/desktop/electron/vault.test.cjs` (18 tests, `node --test`):

* round trip, UTF-8 values, 64 KiB boundary enforced;
* tamper detection (single byte flipped → `VAULT_CORRUPT`), truncation;
* a decrypt failure preserves the encrypted record;
* rotation keeps the id and updates `rotated_at`;
* deletion removes the file and is idempotent;
* corruption moves the file to `quarantine/`;
* a plaintext scan of the vault directory finds no value;
* broker: missing/wrong token, wrong purpose, rate limit, oversized body, single-use grants,
  audit ring contents, no enumeration route.

`app/backend/tests/test_social_vault_client.py` (12) and `test_social_secret_adoption.py` (16) cover
the client side: no broker → `UnavailableVault` → refused operation; metadata-only `secret_ref`
rows; lost record → `unavailable` + `CREDENTIAL_UNAVAILABLE`; adoption and linking; and deletion
removing the blob.
