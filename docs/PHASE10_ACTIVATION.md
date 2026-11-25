# Phase 10 — live activation, provider verification and production release

**Status: complete.** Plan written before implementation, as in every earlier phase; each milestone
carries its commit and its measured result below.

**Baseline:** `37d9b65` (Phase 9 delivered, 0.8.0 installed via the unpacked path).
**Database:** `b8c9d0e1f2a3`, 48 tables, `social_account = 0`, `provider_app_config = 0`,
`secret_ref = 0`, `publish_job = 0`, `analytics_snapshot = 0`, `analytics_metric_definition = 129`.

---

## 1. Audit: what actually exists at `37d9b65`

The honest starting point is that almost all of the *machinery* for live operation is already built and
fixture-tested. What is missing is not code for its own sake; it is (a) external accounts, (b) a single
staged way to prove that the machinery works against a real provider once those accounts exist, and
(c) the last release blocker.

| Area | State at baseline | Evidence |
| --- | --- | --- |
| Git / installation | clean at `37d9b65`, app installed at `%LOCALAPPDATA%\Programs\CreatorOS`, Desktop + Start Menu shortcuts working | `git status`, shortcut inspection |
| Release configuration | `electron-builder.yml` (per-user NSIS, `signAndEditExecutable: false`, `disableAsarIntegrity: true`), `installer.nsh`, `build/icon.ico` | `docs/PHASE9_RELEASE.md` |
| Build / verify scripts | `build-release.ps1`, `verify-install.ps1`, `install-unpacked.ps1`, `uninstall-unpacked.ps1`, `build-backend.ps1`, `diagnose.ps1` | 17/17 install checks in Phase 9 |
| Credential vault | DPAPI `safeStorage` in the Electron main process, scoped loopback broker, `config/secrets/*.bin`, purposes incl. `provider_app_secret`, `delivery_credentials` | `electron/vault.cjs`, `vault-broker.cjs`, 18 tests |
| Provider app registration | `PUT /social/provider-apps/{platform}/{route}` + vault-sealed secret + `POST …/verify`, GUI **Social accounts → App settings** | `services/social/apps.py`, `pages/social/AppSettings.tsx` |
| OAuth | three login paths with PKCE where the provider requires it, fixed loopback callback, single-use sha256 state, rotating refresh with a journal | `services/social/oauth_engine.py`, ADR-23 |
| Capabilities | nine-state vocabulary, catalogue, per-account verdicts from stored facts, `/social/capabilities/matrix` | ADR-24 |
| Live provider probes | `GET /social/accounts/{id}/diagnostics` calls the real `debug_token` / profile / discovery paths | `integrations/social/{facebook,instagram,tiktok}.py` |
| Publishing | 18-state destination machine, approval bound to payload hash, executor on its own lane, reconciliation from evidence | ADR-25/26/27 |
| Media delivery | S3-compatible provider with checksums, signed expiring URLs, cleanup; credentials in the vault | ADR-28, `media_delivery/` |
| Analytics | 129 metrics, ingestion → aggregation → statistics → recommendations, reports, retention, version watch | Phase 8, `docs/evidence/P8.md` |
| Live publishing harness | `scripts/live_publish.py` with four locks; `scripts/publish_readiness.py` | `docs/LIVE_PUBLISHING_TESTS.md` |
| **Missing** | no provider app, no connected account, no delivery bucket, no code-signing certificate, no staged end-to-end verification harness, no module-level production health sweep | this document |

Consequences that follow from the audit, and that this phase must not pretend away:

* every provider-facing capability stays **fixture-tested, not live-tested** until a real app exists;
* `Smart App Control` is ON on this machine, so a freshly built **unsigned** installer is refused —
  signing is the only fix, and there is no certificate;
* the installed application is the unpacked layout, which is a documented, supported recovery option.

---

## 2. Milestones

Each milestone is committed independently with its own verification.

### P10.1 — Audit, plan, activation runbook — `bca4b5f`
* This document.
* `docs/PHASE10_ACTIVATION.md` (runbook), committed before the code that follows it.

### P10.2 — Activation readiness: one command that says exactly what is missing — `1e81f5f`
`scripts/activation_status.py`:
* reads the same rows `/version`, `/social/provider-apps`, `/publishing/delivery` and `/analytics/*`
  read — no new state, no new table;
* per platform and per route: app registered, redirect URI valid for that route, secret readable from
  the vault, scopes registered vs required, app mode, account connected, token state, capability
  verdicts, analytics scopes, delivery bucket health;
* prints a machine-readable report plus one human line per gap, and names the *next action* (which
  screen, which doc section) rather than a generic "not configured";
* exits `0` when nothing is actionable, `1` when something is misconfigured, `2` when the report
  itself could not be built.

### P10.3 — Staged live verification harness — `24a08e2`
`scripts/live_verify.py` — the twelve steps of the mission, in order, each with its own verdict:

| Step | Command | Network? |
| --- | --- | --- |
| 1 OAuth connection | `--account <id>` inspects the stored connection, token state and scope source | no |
| 2 Account discovery | calls the provider's discovery path for the account's route | yes |
| 3 Permission verification | provider `debug_token` / profile scopes vs required scopes | yes |
| 4 Capability snapshot | stored verdicts + a re-evaluation, compared | no |
| 5 Token lifecycle | expiry, refresh availability, journal integrity, rotation readiness | no |
| 6 Analytics synchronisation | enqueue a sync, poll the run, report availability counts (never a zero) | yes |
| 7 Media delivery | bucket health, upload/verify/sign/delete of a temporary object | yes |
| 8 Publishing preflight | `POST /publishing/preflight` for a named destination | no |
| 9 Human approval | approval state and payload hash for that destination | no |
| 10 Real publication | **refused unless** `--publish`, a typed confirmation phrase, and an approval exist | yes |
| 11 Remote status reconciliation | poll/reconcile the destination, print the outcome honestly | yes |
| 12 Analytics for published content | discovery + collection for the published object | yes |

Every step prints `ok | blocked | skipped` plus the reason. The harness never publishes on its own, and
a missing account/app/certificate produces `blocked`, never a fabricated pass.

### P10.4 — Code signing — `230bce7`
`scripts/sign-release.ps1`:
* `-Check` reports what is available (certificate in the store, `CSC_LINK`, timestamp server
  reachability) without touching anything;
* signs `CreatorOS.exe`, the bundled installer and the uninstaller with `signtool` (sha256, RFC-3161
  timestamp), then verifies signature status **and** chain with `Get-AuthenticodeSignature`;
* refuses to write a certificate or password anywhere in the repository, and refuses to run when
  `CSC_LINK` points inside `<PROJECT_ROOT>` (a certificate in the build tree will be committed by
  accident);
* is proven end-to-end with a throwaway self-signed certificate created in `CurrentUser\My`, verified
  and deleted again — which proves the *verification code path*, not Smart App Control acceptance;
* `docs/CODE_SIGNING.md` states plainly what signing does and does not buy: SmartScreen reputation is
  earned over time, and Smart App Control acceptance for an unsigned **installer** is not something a
  self-signed certificate fixes.

### P10.5 — Production health — `4cb5e3e`
`scripts/production-health.ps1` + `scripts/ui_health.cjs`:
* launch from the **Desktop shortcut** (not the exe path) and confirm the backend starts by itself;
* database reachable and at head; vault available and able to seal/read/delete a probe record;
* Ollama discovery, FFmpeg/ffprobe versions, disk space on the data root;
* **every page opens**: the harness walks the whole sidebar over CDP and fails on an error card, a
  blank pane, or a console error, and saves a screenshot per section;
* clean shutdown: no `CreatorOS.exe` or backend interpreter left behind after the window closes;
* data survives an upgrade-style reinstall; uninstall preserves the data root;
* defects found here are fixed in the same milestone, not reported as "known".

### P10.6 — Documentation and final report — this commit
`docs/PHASE10_ACTIVATION.md` (runbook), `docs/LIVE_PROVIDER_VERIFICATION.md` (the twelve steps and what
each verdict means), `docs/PRODUCTION_READINESS.md` (checklist with the honest status of every item),
`docs/CODE_SIGNING.md`; README and release notes updated; evidence appended with the split used in
Phases 8 and 9 (verified here / artefact only / not verifiable here).

## 3. Activation runbook

Everything below is done by a person, on this machine, in this order. **No secret is ever pasted into
chat, a log, a `.env`, or a committed file.** Client secrets and API keys go into
**Social accounts → App settings** (or **Publishing → Media delivery** for object-storage keys), which
hands them to the Electron main process and seals them with Windows DPAPI. The backend only ever
receives a reference id.

Check where you are at any point with:

```powershell
app\backend\.venv\Scripts\python.exe scripts\activation_status.py
```

It prints one line per check with the next action, and exits non-zero only when something is
**misconfigured** (a redirect URI a provider will reject, a required scope missing from the
registration, a secret the vault cannot open). A fresh install with nothing configured is reported as
ready-to-activate, not as a failure. `--probe` additionally makes the read-only provider call; add
`--require-ready` when you want "still missing" to fail as well, which is what a live test should use.

### 3.1 The redirect URI, decided first

Every login path uses the same fixed loopback callback with the route as its last segment:

```
http://127.0.0.1:47821/oauth/callback/facebook_login
http://127.0.0.1:47821/oauth/callback/instagram_login
http://127.0.0.1:47821/oauth/callback/tiktok_login
```

* the port is `CREATOROS_SOCIAL_CALLBACK_PORT` (default `47821`) and must be free;
* Instagram through a Facebook Page authorizes on the **Facebook** login path, so it uses the
  `facebook_login` URI; Instagram Business Login uses `instagram_login`. The two are separate apps in
  the provider console and are never merged here;
* providers compare the string **exactly** - `localhost` instead of `127.0.0.1`, a different port, or a
  trailing slash is refused at the authorization step, so the check in section 3.2 flags it before you
  get there;
* the listener exists only while a connection attempt is running, so nothing is left listening.

### 3.2 Meta: app, product, permissions

1. <https://developers.facebook.com/apps> → **Create app** → type **Business**.
2. Add the **Facebook Login for Business** product for Facebook Pages, and/or the **Instagram** product
   for Instagram. Instagram has two independent login paths in CreatorOS and each needs its own
   registration.
3. **Facebook Login → Settings → Valid OAuth Redirect URIs**: paste the exact callback URI for that
   path. For Instagram Business Login, register the `instagram_login` URI in the Instagram product's
   settings.
4. **App settings → Basic**: copy the **App ID** into CreatorOS → Social accounts → App settings →
   Client ID, and the **App secret** into Client secret. The secret goes straight to the DPAPI vault;
   the app id is ordinary configuration because it is public by design.
5. Request exactly what each path needs. CreatorOS registers these scopes and reports every one that is
   missing from the registration:

   | Path | Required | Optional (separate review) |
   | --- | --- | --- |
   | `facebook_login` (Pages) | `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` | `read_insights` |
   | `instagram_login` | `instagram_business_basic`, `instagram_business_content_publish` | `instagram_business_manage_insights` |
   | `instagram/facebook_login` | `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` | `instagram_manage_insights` |

6. **App Review**: in Development mode only people with a role on the app can authorize it, and only the
   permissions granted to those roles work. `pages_manage_posts` and the insight permissions need
   **Advanced Access** before anyone else can authorize them; Instagram content publishing needs review
   too.
7. **Business Verification** (Business Settings → Security Centre) is a prerequisite for Advanced Access
   on most Pages and Instagram permissions. It is an external review with no shortcut, and nothing in
   this repository can substitute for it.
8. **Account prerequisites that are not permissions**: a Facebook Page you administer, an Instagram
   **professional** account (Business or Creator), and for the Facebook-login path the Instagram account
   linked to that Page. Page insights additionally need a Page with enough activity for the metrics to
   exist at all.

Until 6 and 7 complete, CreatorOS is *supposed* to report those capabilities as `needs_app_review` or
`requires_business_verification` rather than available. That is the truthful state, not a defect.

### 3.3 TikTok: app, Login Kit, Display API, Content Posting API

1. <https://developers.tiktok.com/> → **Manage apps** → create an app.
2. Add **Login Kit** with the redirect URI `http://127.0.0.1:47821/oauth/callback/tiktok_login`.
   TikTok documents `https` redirects for web apps; for a desktop app the loopback `http` URI is what
   the flow actually uses and the console has to accept it verbatim. If the console refuses an `http`
   loopback URI, record that as a provider-side restriction and stop - do not work around it with a
   tunnel or a hosted callback that hands tokens to a server.
3. Add **Display API** (`user.info.basic`, `video.list`) and, for publishing, **Content Posting API**
   (`video.upload` for the inbox path). `video.publish` (Direct Post) and `user.info.stats` are
   **additional scopes** with their own approval; CreatorOS registers them as optional and never assumes
   them (`--probe` reports `NOT_AUTHORIZED` per metric rather than a zero).
4. Copy the **Client key** into Client ID and the **Client secret** into Client secret.
5. TikTok requires PKCE: CreatorOS generates a verifier per attempt, stores it in the vault
   (`oauth_pkce_verifier`), sends the S256 challenge, and compares the returned `state` against a
   single-use hash. Refresh tokens **rotate**: the new refresh token is written to the rotation journal
   before the old one is discarded, so a crash mid-refresh does not lose the account.
6. **Audit**: a TikTok app in sandbox can only post to accounts added as testers. Direct Post becomes
   available after the app passes TikTok's audit. Until then CreatorOS reports Direct Post as
   unavailable and offers the Inbox (`video.upload`) path, where the creator finishes the post inside
   TikTok - which is also the only honest way to describe it: nothing is published without them.

### 3.4 Instagram media delivery (required before any Reels publish)

Instagram does not accept an upload; it fetches the video from a URL it can reach. CreatorOS therefore
requires an S3-compatible bucket with a public HTTPS read path:

1. **Publishing → Media delivery**: provider (`aws`, `cloudflare_r2`, `digitalocean_spaces`,
   `backblaze_b2`, `wasabi`, `minio`, …), bucket, region, optional custom endpoint, prefix, and URL
   lifetime (default 900 s, ceiling 7 days).
2. The access key id and secret access key go to the **same DPAPI vault** (purpose
   `delivery_credentials`), never into the settings JSON.
3. **Check** runs a real probe: write a small object, verify the checksum, sign a URL, read it back,
   delete it. A bucket that cannot pass that is not usable for publishing, and the app names the
   requirement that failed.
4. The originals stay on `D:` - only a temporary copy is uploaded, and it is deleted after the post. No
   public-permanent bucket, no ngrok/Cloudflare-tunnel style exposure of this machine: the delivery
   provider's own HTTPS endpoint is the only thing Instagram ever reaches.

### 3.5 Verifying what the provider actually granted

Configuration is not permission. After connecting an account:

* **Social accounts → Connected accounts** shows the stored scope list and where it came from
  (`debug_token` for Meta, the token response for TikTok);
* **Account details → Diagnostics** (equivalently `GET /social/accounts/{id}/diagnostics`) makes the real
  provider call: Meta `debug_token`, the profile read, and account discovery. It returns granted scopes,
  token type, expiry, whether the app id matches, and how many accounts are reachable;
* **Permissions** re-evaluates the capability catalogue from those stored facts and shows
  `available` / `needs_app_review` / `requires_business_verification` / `unavailable` per capability,
  each with its reason;
* `scripts/activation_status.py --probe` prints the same verdicts in a terminal for a scripted check.

A capability is `available` only when the provider's own answer supports it. Nothing in this product
infers permission from the fact that a scope was requested.

---

## 4. What Phase 10 measured

Every number below was produced on this machine and can be reproduced with the command named beside it.

| Result | Command |
| --- | --- |
| 0 required failures, 0 warnings, backend suite 1564 passed / 9 skipped inside it | `scripts\diagnose.ps1` |
| 35/35 production-health checks, 49/49 pages | `scripts\production-health.ps1` |
| 39/39 with the upgrade path; 40/40 with uninstall and reinstall | `scripts\production-health.ps1 -IncludeUpgrade` · `-SkipSweep -IncludeUninstall` |
| 12/12 installer checks | `scripts\verify-install.ps1` |
| signing, chain and tamper detection proven with a throwaway certificate | `scripts\sign-release.ps1 -SelfTest` |
| no certificate available, installer unsigned | `scripts\sign-release.ps1 -Check` |
| 12-step verification: 5 blocked, 4 skipped, 1 ok, exit 1 | `scripts\live_verify.py` against a live backend |
| 0 provider apps, 0 accounts, 0 delivery buckets | `scripts\activation_status.py` |
| backend 1564 passed / 9 skipped, frontend 200 passed, `ruff` and `tsc` clean | the suites |

The honest summary: **the application is production-ready; distribution is not.** Everything that can be
verified without a provider account or a certificate verifies. The four things that cannot are named in
`docs/PRODUCTION_READINESS.md` §4, and none of them is a code defect.

## 5. What this phase will not do

