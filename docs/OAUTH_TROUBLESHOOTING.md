# OAuth troubleshooting

Every failure has a code. Find the code, read the row, do the thing in the last column.

## Where to look first

1. **Connected accounts** page — a red bar with the code and message.
2. **Vault status** page — is the vault available, is the callback listener running?
3. **App settings** → *Verify configuration* — checks redirect, scopes, app mode locally.
4. **Account details** → *Run diagnostics* — a redacted report of what the provider answered.

## Codes

### Start / configuration

| Code | HTTP | Meaning | Fix |
| --- | --- | --- | --- |
| `APP_NOT_CONFIGURED` | 409 | no client id/secret for this provider+route | App settings → paste client id and secret |
| `VAULT_UNAVAILABLE` | 503 | no DPAPI vault (app not running in the desktop shell) | start CreatorOS normally; nothing was stored |
| `CALLBACK_NOT_READY` | 503 | the loopback listener is not running | restart the app; check port 47821 is free |
| `REDIRECT_INVALID` | 400 | redirect is not loopback, has a query, or wrong path | use exactly `http://127.0.0.1:47821/oauth/callback/<route>` |
| `SCOPE_UNKNOWN` | 400 | a scope outside the route's surface | only the listed scopes are accepted |
| `PKCE_REQUIRED` | 400 | TikTok flow without a verifier/challenge | internal invariant; report it, do not retry blindly |
| `VAULT_EXISTS` | 409 | a different secret already uses the id | use a fresh reservation |

### Callback / state

| Code | Meaning | Fix |
| --- | --- | --- |
| `STATE_INVALID` | the state was not one CreatorOS issued | start the connection again from the GUI |
| `STATE_EXPIRED` | older than 10 minutes | start again; finish within 10 minutes |
| `STATE_CONSUMED` | this callback was already used | the first one succeeded — check Connected accounts |
| `STATE_ROUTE_MISMATCH` | state belongs to another provider/route | the registered redirect URI points at the wrong route |
| `REDIRECT_MISMATCH` | the provider returned a different redirect | console value differs from CreatorOS's; make them identical |
| `AUTHORIZATION_DENIED` | the user pressed Cancel at the provider | start again and approve |
| `SCOPE_NOT_GRANTED` | a requested permission was refused | re-connect and grant it, or accept the capability as unavailable |

### Token / refresh

| Code | Meaning | Fix |
| --- | --- | --- |
| `TOKEN_EXPIRED` | access token expired | *Refresh token* on the account, or reconnect |
| `REAUTHORIZATION_REQUIRED` | no usable refresh path | reconnect the account |
| `REFRESH_IN_PROGRESS` | a refresh already holds the lock | wait; the lock is abandoned after 5 minutes |
| `CREDENTIAL_UNAVAILABLE` | vault record missing/undecryptable | reconnect; the account detail page shows which record |
| `REFRESH_FAILED` | last refresh failed, credential may still work | check *Run diagnostics*, then retry or reconnect |

### Provider

| Code | Meaning | Fix |
| --- | --- | --- |
| `PROVIDER_ERROR` | unmapped provider body; raw code in `detail` | read `detail`; usually an app configuration problem |
| `PROVIDER_UNAVAILABLE` | provider 5xx | retry later |
| `NETWORK_ERROR` | the request never left the machine | check the connection / VPN |
| `PROVIDER_TIMEOUT` | the provider did not answer in time | retry |
| `RATE_LIMITED` | throttled (`retry_after_s` when known) | wait the stated time |
| `APP_REVIEW_REQUIRED` | the permission needs Meta review | submit the app for review |
| `ACCOUNT_TYPE_UNSUPPORTED` | e.g. a personal Facebook profile | not connectable through the API; use a Page |
| `APP_IN_USE` | deleting an app that still has connected accounts | disconnect those accounts first |

## Situations, not codes

**"Connect Facebook" is disabled.** The app is not registered for that route. *App settings* →
pick the login path → paste client id (**and** secret if you have one) → Save.

**The browser opens but Facebook says the redirect URI is not allowed.** The console value must be
`http://127.0.0.1:47821/oauth/callback/facebook_login`, character for character. Note it is `http`,
`127.0.0.1` and not `localhost`, and `facebook_login` — not `facebook-login`.

**The browser goes back to CreatorOS with "Not connected".** The fixed page names the code. If it
says `STATE_EXPIRED`, the flow took over 10 minutes. If `STATE_CONSUMED`, it already worked.

**The wizard stays on "Waiting for the browser".** The callback never arrived. Is the listener
running (Vault status)? Did the browser open the right URL? Expand *Authorization URL* in the wizard
and check `redirect_uri` inside it.

**"App registered: yes" but connecting fails with `APP_NOT_CONFIGURED`.** The client *secret* is
missing. Config with a client id only can be verified, but cannot exchange a code.

**TikTok says `invalid_client`.** `client_key` (not `client_id`) is what TikTok expects — CreatorOS
sends the stored client id under that name, so this means the value itself is wrong.

**Instagram connected via path A cannot be revoked.** True: Instagram Business Login documents no
revoke endpoint. The credential is deleted here; remove CreatorOS in Instagram → Apps and websites to
invalidate it there. `revoked_at_provider` stays `false` and the GUI says why.

**Every capability says `requires_developer_approval`.** The app is in `development` mode. Only app
testers can authorize, and most publish permissions need review. Set the app mode to `live` in App
settings once Meta approves it.

**A capability is `unsupported` on one Instagram account and `available` on another.** They were
connected through different login paths (Instagram Login vs. Facebook Login); the scope vocabularies
differ. The Permissions page shows the route per account.

**A refresh says `rotated: false`.** Instagram extends the same token; only TikTok rotates.

## What never to do

* Do not paste a token into a chat, an issue or a log to "check" it.
* Do not add a scope that is not in the route's surface to work around a missing capability — the API
  rejects it (`SCOPE_UNKNOWN`), and the capability would still be evaluated from granted scopes.
* Do not delete vault files by hand. Disconnect the account: the vault record, the `secret_ref` row
  and the account state move together.
* Do not treat a passing test suite as proof that a provider works: every test uses recorded HTTP
  fixtures. The only real provider calls are *Verify now* and *Run diagnostics* on a connected
  account.
