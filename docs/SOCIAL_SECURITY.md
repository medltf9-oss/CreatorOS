# Social security

What this phase protects, what it explicitly does not, and the tests that make the claims checkable.

## Threat model

| Threat | Mitigation | Residual risk |
| --- | --- | --- |
| Malware on the same machine reads the database | no credential is in the database; only `secret_ref` metadata and a ciphertext digest | malware can ask DPAPI to decrypt as this user |
| Another Windows user reads `config/secrets` | DPAPI user scope | none while the profile is intact |
| The disk is stolen / copied | DPAPI blobs are unreadable off this machine and this user | none for the blobs |
| A malicious webpage triggers a callback | state is 32 random bytes, single use, bound to provider+route, expires in 10 min | a user who completes a flow they did not start |
| Replayed callback | state consumed by conditional UPDATE before the exchange | none |
| Authorization code intercepted | code is never written to a page, a log, or the database | a local process sniffing loopback traffic |
| A forged vault request from another local process | per-launch token, timing-safe compare, 127.0.0.1 only, 120 req/min | a process that can read the backend's environment |
| Renderer compromise reads a token | preload exposes no decrypt handle; there is no IPC route that returns a value | renderer can ask the shell to *store* a value, which is not a read |
| A token appears in a log or audit row | audit rows store references and digests only; `test_social_leaks.py` scans the log and DB for needles | a future code path that prints a value |
| Publishing something accidentally | no publish/upload/post code exists; `publishing` reports `not_implemented` | a deliberate Phase 7 implementation |

## The three credential paths

1. **Renderer → vault (default for provider app secrets.)** The value is sealed by the Electron main
   process through `creatoros:vaultStore`; the backend only ever receives an id, via `adopt` and
   `secret-ref`. Nothing appears in an HTTP body to Python.
2. **Backend → API → vault (`POST /provider-apps/{p}/{r}/secret`).** Used by scripts and tests. The
   value transits the authenticated loopback API once; the backend must hold it in memory anyway to
   compute `appsecret_proof` and exchange codes.
3. **Vault → backend (one value at a time).** `SecretStore.read` asks the broker for exactly one id
   and one purpose, uses the value for one adapter call, and never stores or returns it. The broker
   enforces purpose matching, single-use grants, a rate limit and a 16 KiB body cap, and keeps an
   audit ring with no values in it.

## Non-negotiables encoded in the code

* **No social password is ever requested.** No form asks for one; no endpoint accepts one.
* **No browser session automation, no scraping.** The only browser interaction is
  `shell.openExternal` on an allowlist (`facebook.com`, `www.facebook.com`, `instagram.com`,
  `api.instagram.com`, `www.tiktok.com`), with `https` enforced and a 4000-character cap.
* **Official APIs only.** Every HTTP call goes to a documented endpoint declared in
  `integrations/social/config.py`.
* **No plaintext fallback.** Without the vault: `VAULT_UNAVAILABLE`, nothing stored, account stays
  unconnected. `InMemoryVault` exists in tests only and never writes a file.
* **Agents never see secrets.** `social.accounts.list` and `social.capabilities.read` are the only
  social tools, both `read`-class, and their schemas contain no token/secret/code/verifier field.

## Renderer surface

`preload.cjs` exposes exactly four social-related calls:

| Call | Returns |
| --- | --- |
| `vaultHealth()` | availability, backend, record count, protection text |
| `vaultStore({id, secret, purpose, provider, alias})` | metadata only (`id`, `alg`, `bytes`, `fingerprint`, `version`) |
| `vaultDelete(id)` | `{ok, removed}` |
| `openAuthUrl(url)` | `{ok}` or `{ok:false, error}` |

No `decrypt`, no `read`, no `list`, no token of any kind. A page that wanted a credential would have
to ask the backend, and the backend has no route that returns one.

## Loopback listener

* binds `127.0.0.1:47821` — not `0.0.0.0`, not a UNIX socket, not a named pipe;
* answers `GET` on `/oauth/callback/<route>` only; every other path is a 404 with a plain body;
* never echoes the authorization code into the HTML;
* fixed, minimal page with `Cache-Control: no-store` and `Content-Security-Policy: default-src 'none'`;
  the page contains no script, so nothing reflected can execute;
* query parameters are length-capped before they reach the engine.

## Error and audit discipline

* Every failure is a `SocialError` with a stable code, a human message and optional `detail`. No
  message interpolates a token, code or verifier (`test_social_leaks.py::
  test_a_vault_failure_does_not_print_the_value_into_the_error`).
* Audit events store `{provider, route, account_id, capability, fingerprint, reason, outcome}` —
  never a value, never a prefix of a value.
* The API's error envelope is `{code, message, detail?, correlation_id}`; the GUI shows the code and
  the message, and the docs map every code to an action.

## Leak tests

`tests/test_social_leaks.py` (7 tests) uses unmistakable synthetic values
(`APP-SECRET-4f1c9a-NEEDLE`, `EAAB-PAGE-4f1c9a-NEEDLE`, `TT-REFRESH-4f1c9a-NEEDLE`, …), pushes them
through the real paths (app registration, a full OAuth claim, a TikTok refresh, a disconnect) and
then scans:

* every API response body of the social surface;
* the SQLite file, byte for byte;
* the JSON application log and the `audit_event` table;
* the backup directory and the whole data root — for a plaintext file containing a value;
* the vault directory for plaintext (in the Electron test, `vault.test.cjs`).

`tests/test_agent_social_tools.py` (13) asserts that the two agent tools have no secret-bearing
field, that their implementations contain no vault or network call, and that a secret-shaped value in
a tool payload is rejected.

## What a user should be told

The GUI says this, verbatim, on the Vault status page:

> Windows DPAPI, current user. Protects against other Windows users and offline disk access; it does
> not isolate credentials from software running as this user.

A vault that overclaims is worse than no vault, because it changes what the user is willing to do on
the machine.
