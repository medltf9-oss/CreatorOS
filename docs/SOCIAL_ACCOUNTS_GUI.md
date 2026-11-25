# Social accounts GUI

Six pages under **Social accounts** in the left navigation. Every value on them comes from the API;
no page contains a fixture, a placeholder count or a button that pretends.

| Page | Nav id | Purpose |
| --- | --- | --- |
| Connected accounts | `soc-overview` | the fleet: totals, vault and listener state, per-account rows |
| Connect account | `soc-connect` | the wizard: pick a login path, watch the browser round trip |
| Account details | `soc-account` | one account: connection, token lifecycle, credentials, capabilities, diagnostics, disconnect |
| Permissions | `soc-permissions` | capability × account matrix and the catalogue |
| Vault status | `soc-vault` | DPAPI facts, secret counts, refresh journal, listener |
| App settings | `soc-apps` | developer app registration per login path |

## Connected accounts

* Four summary cards: account total (with disconnected / needs-attention counts), credential vault
  (backend + record count + the plain-language DPAPI limits), callback listener (port or "not
  running"), Phase 7 readiness (publish-capable count and the blocker list).
* Platform filter: *All platforms · Facebook · Instagram · TikTok*.
* One row per account: label, platform, login path, account type, `@username`, credential state and
  token state. Clicking opens Account details.
* With no account connected, the empty state says *why*: register a developer app first. It does not
  offer a "create account" form, because an account exists only after a provider returned a token.
* Below: the four login paths with their configured state, and the last connection attempts with
  their error codes.

## Connect account

Per login path: what the path is for (Facebook Pages only; two Instagram paths; TikTok PKCE +
Direct Post), whether an app is registered, app mode, PKCE, scope count, and a **Connect** button.

The button is **disabled when the app is not registered**, with a title explaining it and a link to
App settings. That is the whole anti-fake rule in one control.

While a flow is open, the page shows: requested scopes, the exact redirect URI, expiry time
(10 minutes), PKCE status, app mode, and the authorization URL behind a collapsed *details* (it
contains the one-time state). **Cancel this attempt** invalidates it.

When the browser comes back, the transaction result lists each connected account with its capability
chips, and any accounts the provider returned but that cannot be published to are summarised as
skipped.

## Account details

* **Connection**: internal id, provider account id, login path, API version, last verified, last
  refresh, disconnected timestamp.
* **Token lifecycle**: state, expiry, refresh expiry, `can_refresh`; **Refresh token** is disabled
  when the provider has no refresh token (title says so). **Verify now** and **Re-evaluate
  capabilities** are separate buttons because they do different things: the first talks to the
  provider, the second only re-reads stored facts.
* **Credentials in the vault**: one row per `secret_ref` — kind, fingerprint, byte size, status. No
  value, and no way to ask for one.
* **Capabilities**: one card per capability with its state, its reason, missing scopes and when it
  was verified.
* **Label and group**: free text, saved per account; a reconnect never overwrites them.
* **Permissions granted**: the scope list plus `scope_source`, with the difference between "granted"
  and "requested as consent" spelled out.
* **Diagnostics**: on demand, redacted, never cached.
* **Disconnect**: *Disconnect and revoke*, *Delete local credential only*, and — once disconnected —
  *Reconnect* and *Delete the row*.

## Permissions

* Totals per capability state.
* A matrix: one row per capability, one column per account, each cell a state badge with the
  capability's tooltip; account headers link to Account details.
* A filter to show only capabilities at least one account has evaluated.
* The catalogue below it: label, key, platform, routes, required scopes, notes, whether it is
  `manual_only`, and a documentation link.
* With no account evaluated yet, the empty state explains that verdicts come from real granted
  scopes.

## Vault status

* Availability + the verbatim protection text; backend, algorithm, record schema, records, directory,
  corrupt count; calls served, failures, re-encryptions, last operation.
* "What DPAPI actually protects" — the four honest bullets (other users, offline disk, malware as
  you, profile loss).
* Secret metadata counts by status. No values, no fingerprints in the summary.
* **Refresh journal**: every rotation with state, attempts, error code, and a **Resolve** button for
  interrupted ones.
* Callback listener state and its base URL.

## App settings

* The four login paths with `not configured` / app mode / `secret stored` badges.
* Selected path form: client id, client secret, redirect URI, app mode, API version, notes.
  The client secret goes to the Electron main process (DPAPI), never into the JSON body sent to the
  backend; after saving, the field is cleared and the placeholder becomes *stored — leave empty to
  keep*.
* **Save app** → saves the registration, then seals and links the secret if one was typed, then
  re-reads the saved record so the redirect URI the backend filled in is visible.
* **Verify configuration** → the five checks, with `provider_reachability` explicitly `unknown`.
* **Delete app** → removes the registration **and** revokes its sealed secret (a connected account
  blocks deletion with `APP_IN_USE`).
* Footer states the policy: no social password, no browser automation, no scraping, no workarounds
  for missing permissions.

## Wiring

`src/apiSocial.ts` is the typed client (`SocialApi`, 30 routes). Two conventions:

* no method returns a credential, because no endpoint does;
* `storeProviderSecret(platform, route, secret)` is the only path that hands a value anywhere, and it
  hands it to `window.creatoros.vaultStore` (Electron), then adopts and links the metadata.

`App.tsx` creates one `SocialApi` from the bootstrap `apiBase` + per-launch token and renders the six
pages. `window.creatoros` is typed with the four bridge calls the pages use.

## Verified how

* `pnpm typecheck` (strict, `verbatimModuleSyntax`), `pnpm build`.
* Screenshots of each page against the live backend (`scripts/ui_drive.py`), checked against real API
  output — see `docs/evidence/P6.md`.
* The full secret flow was exercised end to end in the running app (App settings → save with a
  secret → vault record count 1 → *Verify configuration* → start OAuth → cancel → delete app → count
  0).
