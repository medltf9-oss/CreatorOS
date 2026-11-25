# Operational activation

This is the record of the operational-activation phase on this machine: what was audited, what works,
what is configured but not verified, and what cannot be verified here at all because it needs an
account, an approval, a certificate or a second computer.

Baseline: commit `33bc269` (Phase 10 complete), installed app 0.8.0, database at head
`b8c9d0e1f2a3`.

The phase is integration and verification, not feature work: no working subsystem was redesigned, no
Windows security setting was changed, no existing data was deleted, and no secret was written to a
terminal, a log or a file.

| Section | Subject | Milestone |
| --- | --- | --- |
| §1 | Activation audit | P11.2 |
| §2 | Local AI | P11.1 |
| §3 | Meta (Facebook Pages, Instagram) | P11.3 |
| §4 | Instagram media delivery (S3-compatible) | P11.3 |
| §5 | TikTok | P11.3 |
| §6 | Signed distribution | P11.3 |
| §7 | First real campaign | P11.3 |
| §8 | Final status matrix | P11.4 |

---

## 1. Activation audit

### 1.1 Repository and installation

| Fact | Value |
| --- | --- |
| Git HEAD at audit start | `33bc269`, working tree clean |
| Installed application | `%LOCALAPPDATA%\Programs\CreatorOS\CreatorOS.exe`, version 0.8.0, `isDev: false` |
| Shortcuts | Desktop and Start Menu, brand icon on both |
| Database | `<DATA_ROOT>\data\database\creatoros.db`, revision `b8c9d0e1f2a3`, 48 tables, `integrity_check ok`, `foreign_key_check []` |
| Data root | `<PROJECT_ROOT>` — nothing written to C: except Ollama's own bookkeeping and the app binary |
| Bundled interpreter | `resources\backend\python.exe` (3.12.10), so the app does not use a system Python |

Row counts that matter for activation:

| Table | Rows | Meaning |
| --- | --- | --- |
| `provider_app_config` | 0 | no developer app has been registered |
| `social_account` | 0 | no account is connected |
| `secret_ref` | 6 | all `revoked`: left over from health probes, no live secret |
| `publish_job` | 0 | nothing has ever been published |
| `analytics_snapshot` | 0 | nothing has ever been measured |
| `analytics_metric_definition` | 129 | the metric catalogue is seeded |
| `model_profile` | 6 | the six documented profiles exist |
| `agent_definition` | 111 | the workforce exists |
| `ai_execution` | 194 | real local inference has run |
| `media_asset` | 2 | two local media files from earlier phases |

### 1.2 What the four audit scripts say

| Script | Result | Exit |
| --- | --- | --- |
| `scripts/activation_status.py` | `MISSING` for all four login routes and for media delivery; analytics `READY` | **0** — a fresh install is coherent, not broken |
| `scripts/activation_status.py --require-ready` | same report, but any missing item is a failure | **1** |
| `scripts/publish_readiness.py` | `facebook: no_provider_app_configured`, `instagram: no_provider_app_configured`, `tiktok: no_provider_app_configured`, `media_delivery: not_configured` | 0 |
| `scripts/sign-release.ps1 -Check` | `CSC_LINK: none`, installer and app exe `NotSigned`, Smart App Control enforced | 0 |
| `scripts/production-health.ps1` | **35/35 in this phase**: 49/49 pages, clean launch from the Desktop shortcut, clean shutdown, no orphan backend, no data drift | 0 |
| `scripts/diagnose.ps1` | **0 failures, 0 warnings**: backend **1577 passed, 9 skipped** (456 s), Opt-In publish gate off, writes outside the data root limited to Ollama's documented 1.86 GB | 0 |

### 1.3 What the running product reports, asked directly

Every line below is a real call to the installed app's own API, not a reading of the source.

| Call | Answer | Reading |
| --- | --- | --- |
| `GET /social/oauth/routes` | callback listener **running** on `127.0.0.1:47821`, `vault_available: true`, all four routes `configured: false` | the loopback listener and vault are ready; nothing is registered |
| `POST /social/oauth/start` (facebook) | `409 APP_NOT_CONFIGURED` "the Facebook Login (Pages) app has not been registered yet" | the OAuth path fails cleanly and names the fix |
| `GET /social/callback` | `running: true`, `requests: 0` | the listener is up and has received nothing |
| `GET /social/accounts` | 0 items | no account |
| `GET /publishing/delivery` | `configured: false`, `credentials_present: false`, `reason: bucket configuration, credentials`; supported providers `b2, minio, r2, s3, spaces, wasabi` | delivery is implemented and unconfigured |
| `POST /publishing/delivery/check` | `ok: false`, `DELIVERY_NOT_CONFIGURED`, explaining that Instagram fetches from a public URL while Facebook and TikTok upload directly | the diagnostic distinguishes the paths that do and do not need a bucket |
| `GET /publishing/overview` | `total: 0`, delivery missing `bucket configuration`, `credentials` | nothing queued, nothing published |
| `GET /analytics/overview` | `accounts_total: 0`, `observations: 0`, `latest_collection_at: null` | nothing measured, and no zero is invented |
| `GET /ai/models` | 6 items, `ollama_online: true` | local AI is live (see §2) |
| `GET /ai/limits` | `max_concurrent_generations 1`, `max_ram_percent 85`, `min_free_ram_gb 2`, `max_vram_percent 92`, `max_queue_depth 25`, `generation_timeout_s 300` | the governor is enforcing the documented limits |

### 1.4 Outstanding operational requirements

Ordered by what unblocks the most. "Who" is the honest answer: several rows are not mine to do.

| # | Requirement | Blocks | Who |
| --- | --- | --- | --- |
| 1 | Create a Meta app (Business type), copy App ID + App Secret into *Social accounts → App settings* | Facebook Pages connect, both Instagram login paths | **user** (needs a Facebook account and the developer console) |
| 2 | Register the three loopback redirect URIs in that app | every Meta connect attempt | **user** |
| 3 | Meta App Review + Business Verification for the insights permissions | real analytics from Facebook/Instagram | **external approval** |
| 4 | Create a TikTok app (Login Kit + Content Posting API), register the loopback redirect URI | TikTok connect, upload, publish eligibility | **user** |
| 5 | TikTok audit / `video.publish` add-on approval | Direct Post | **external approval** |
| 6 | An S3-compatible bucket (b2 / minio / r2 / s3 / spaces / wasabi) and its keys | Instagram Reels publishing only | **user** (this phase must not create one) |
| 7 | A code-signing certificate (OV or EV) | distributable installer | **purchase** |
| 8 | A clean Windows 11 machine with SAC on and no developer tooling | the "will a stranger be able to install this?" question | **user** |
| 9 | Explicit, hash-bound approval per content item | any publication | **user**, at the moment of publishing |

Rows 1–6 are exactly what `scripts/activation_status.py` reports as `MISSING`; the report and this table
say the same thing, which is the point of having the script.

### 1.5 Deliberately not done in this audit

- No provider app, account or bucket was created: creating them needs accounts I do not have and would
  be an external action the user has not authorised for me to take on their behalf.
- No secret was requested in chat, and nothing was written to `.env`.
- No row was inserted into the live database to make a screen look configured. A fake registration would
  have made `activation_status.py` say `READY` while nothing could connect - the one outcome this phase
  must never produce.
- No Windows security setting was changed. Smart App Control remains enforced.

---

## 2. Local AI

**Status: working and verified.** Full measurements, root cause and the four defects that running a
real objective exposed are in [`LOCAL_AI_ACTIVATION.md`](LOCAL_AI_ACTIVATION.md). Summary:

| Item | Result |
| --- | --- |
| Models visible to the daemon | 6/6 (9.25 GB, all on `<DATA_ROOT>\models\ollama`); **none downloaded or moved** |
| Real inference through the product | `POST /ai/generate` → `qwen3:1.7b`, 37–39 tok/s, sensible answer |
| GPU placement | `/api/ps` reports `size_vram == size` → 100 %, reached through Vulkan because this Ollama build has no Pascal CUDA kernel. No CUDA configuration was changed |
| Streaming | `/ai/stream` emits `token` events then `meta` |
| Cancellation | `/ai/executions/{id}/cancel` → `200 {"cancelled_in_flight": true}`, partial text preserved |
| Resource Governor | 1 concurrent generation, 85 % RAM ceiling, 2 GB floor, 92 % VRAM ceiling, 25 queue depth, 300 s timeout |
| One whole objective | `succeeded`, `verdict: pass`, 15 tasks, 2626 tokens, nine usable captions in the report |

What was fixed to get there: the Ollama tray app's stored model path (it served an empty directory),
three schema-tolerance defects in the plan/copy contracts, a compiled deliverable that described work
instead of containing it, and a quality review that was shown an empty payload.

---

## 3. Meta (Facebook Pages, Instagram)

**Status: requires user action.** Nothing can be verified against Meta from here because no Meta
developer app exists and creating one needs a Facebook account and the developer console, which is the
user's action, not a script's.

### What is verified, on the installed app, without a provider

| Check | Result |
| --- | --- |
| Loopback OAuth listener | **running** on `127.0.0.1:47821`, `url_base http://127.0.0.1:47821/oauth/callback/` |
| Credential vault | available (`vault_available: true`) |
| The four login routes | `facebook/facebook_login`, `instagram/instagram_login`, `instagram/facebook_login`, `tiktok/tiktok_login` — all present, `configured: false`, with the exact scope lists each will request |
| Attempting to connect anyway | `POST /social/oauth/start` → **`409 APP_NOT_CONFIGURED`** "the Facebook Login (Pages) app has not been registered yet", with a correlation id |
| Untrusted callback | `GET /oauth/callback/facebook_login?error=access_denied` with no state → **`400 STATE_INVALID`** "the authorization response did not carry a usable state value" |
| Forged callback | `?code=abc&state=forged` → **`400 STATE_INVALID`** |
| Unknown callback path | `/oauth/callback/nope` → **`400 UNKNOWN_ROUTE`** "This callback path is not one of ours." |
| Non-callback path | `/not-a-callback` → **404** |
| GUI | *Social accounts → App settings* shows all four paths as **not configured / no secret** with their scope counts; *Connect account* lists the paths and their requirements (see `docs/evidence/p11-app-settings-unconfigured.png`, `p11-connect-account.png`) |

Fixture-tested and proven only against a mock: the whole authorization code exchange, Page discovery,
capability mapping, `AUTHORIZATION_DENIED` handling, PKCE for TikTok, refresh and revocation. These are
`fixture-tested`, not `real-provider-tested`, and this document never conflates the two.

### What the user has to do (about 15 minutes)

Follow [`DEVELOPER_APP_SETUP.md`](DEVELOPER_APP_SETUP.md) §1. In short:

1. Create a Meta app of type **Business**; note App ID and App Secret.
2. Add **Facebook Login** and, if Instagram Login is wanted, **API setup with Instagram login**.
3. Register these exact redirect URIs (the listener is already bound to this port and these paths):
   `http://127.0.0.1:47821/oauth/callback/facebook_login` and
   `http://127.0.0.1:47821/oauth/callback/instagram_login`.
4. Paste App ID + App Secret into *Social accounts → App settings* for each path and press **Save app**.
   The secret goes into the DPAPI vault; it is never written to a file, a log or this document.
5. Press **Verify configuration**: it checks the redirect shape, the registered scopes, that the vault
   can decrypt the secret, and the app mode. It reports provider reachability as `unknown` for a
   reason - that needs a human authorization.
6. Connect an account. In development mode only app testers can authorize, which is fine for a first
   run; the app shows `development` app mode everywhere it matters.

### What only Meta can grant

`read_insights`, `instagram_*_manage_insights` and Page-level insights need **App Review**, and a
Business app that publishes for other people needs **Business Verification**. Until then those
capabilities correctly report `requires_developer_approval` / `needs_app_review`, and analytics stays
empty rather than inventing numbers. Both are external approvals with a review queue, not a switch.

---

## 4. Instagram media delivery (S3-compatible)

**Status: requires user action.** Instagram does not accept an upload: it fetches the video from a
public HTTPS URL. Facebook and TikTok upload directly and do not need this, which is why only Reels is
blocked by it.

| Check | Result |
| --- | --- |
| `GET /publishing/delivery` | `configured: false`, `credentials_present: false`, `reason: "bucket configuration, credentials"`, supported providers `b2, minio, r2, s3, spaces, wasabi` |
| `POST /publishing/delivery/check` | `ok: false`, `DELIVERY_NOT_CONFIGURED`, with the explanation that Facebook and TikTok are unaffected |
| GUI | *Publishing → Media delivery* shows status **not configured**, credentials **missing**, the missing pieces in words, and a form with provider, bucket, region, public base URL, endpoint, prefix and URL lifetime (`docs/evidence/p11-media-delivery-unconfigured.png`) |
| Fixture-tested | upload → SHA-256 verification → signed URL with a TTL → expiry → object deletion, against a local fake endpoint |

What this phase deliberately did **not** do: create a bucket, use a public permanent bucket, or open a
tunnel. Originals stay in `<DATA_ROOT>\data`; delivery copies are temporary objects with a TTL and are
deleted after publication.

To close it: create a bucket at any S3-compatible provider, then enter provider/bucket/region/prefix and
the keys in *Publishing → Media delivery*. The keys go to the vault. Press **Check access** to prove the
credentials can write and delete, then publish a Reel.

---

## 5. TikTok

**Status: requires user action; Direct Post additionally requires external approval.**

| Check | Result |
| --- | --- |
| Route registered in the product | `tiktok/tiktok_login`, `pkce_required: true`, scopes `user.info.basic`, `video.list`, `video.upload` |
| Redirect URI it will send | `http://127.0.0.1:47821/oauth/callback/tiktok_login` |
| `video.publish` | **not requested by the connect flow.** Direct Post needs TikTok's audit and the add-on; claiming otherwise would be a false claim about a capability nobody has granted |
| Fixture-tested | PKCE S256 challenge/verifier, token exchange, refresh rotation, chunked upload, publish-status polling |

To close it: create the app at <https://developers.tiktok.com/>, add Login Kit and Content Posting API,
register the redirect URI above, paste client key + secret into *App settings*, connect an account, and
test an **upload to the inbox** first (that is what `video.upload` allows today). Direct Post is a
separate approval; until TikTok grants it, CreatorOS will not offer it.

---

## 6. Signed distribution

**Status: blocked on a certificate, which is a purchase.** No certificate exists on this machine, so
`CSC_LINK` is unset and both artefacts are `NotSigned`:

```
CreatorOS-Setup-0.8.0.exe   NotSigned   no timestamp
CreatorOS.exe               NotSigned   no timestamp
Smart App Control           enforced (ON)
signtool                    present, timestamp server reachable
```

The signing path itself was re-proven in this phase with a throwaway certificate
(`scripts/sign-release.ps1 -SelfTest`): sign → `UnknownError`/`UntrustedRoot` (exactly right for a
self-signed certificate) → timestamped → tampering detected as `HashMismatch` → certificate and
temporary files removed afterwards. It does **not** claim that Smart App Control accepts a self-signed
installer; it does not.

What is needed: an **OV** certificate (exportable `.pfx`, cheapest) or an **EV** certificate (hardware
token or cloud HSM, immediate SmartScreen reputation). Then:

```powershell
$env:CSC_LINK = 'C:\keys\creatoros-codesign.pfx'
$env:CSC_KEY_PASSWORD = '<password>'          # never printed, never written to a file
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1   # signs, verifies, then writes SHA256SUMS.txt
powershell -ExecutionPolicy Bypass -File scripts\verify-install.ps1 -Uninstall
```

No publisher identity was invented, and no Windows security setting was touched. SAC stays on;
`scripts/install-unpacked.ps1` remains the recovery route for this machine.

---

## 7. First real campaign

**Status: blocked, correctly, before any content work.** A real campaign needs a connected account to
address, and there is none. The harness enforces that rather than working around it:

```
$ python scripts/live_verify.py
stderr: no connected account: connect one in the app, or pass --account <id>
exit:   1
```

That is the whole result on this machine today, and it is the right one: the twelve-step verification
cannot start without an identity to verify.

For reference, the Phase 10 run against a **seeded fixture account in a throwaway data root** produced
5 `blocked`, 4 `skipped`, 1 `ok`, exit 1 (`docs/evidence/P10.md` §A) - which is what an honest harness
looks like when the provider side is missing: it does not manufacture a pass.

The publishing path is implemented, fixture-tested, and gated three times over:

1. a destination cannot publish until its **capabilities** are satisfied (account, scopes, media,
   delivery);
2. a human must approve content, and approval is bound to a **SHA-256 of the exact payload**;
3. the harness itself will not publish without `--publish` **and** the literal phrase
   `PUBLISH <id> <approved-hash>`.

Nothing was published, no analytics were inserted, and no account was used: there is no account.

When accounts exist, the campaign is: import the video → inspect → write captions, hashtags and platform
variants → run the quality checks → pick the destination → read the preflight → approve (human) →
publish → verify the remote id and URL → collect analytics. Each step is a screen that already exists.
---

## 8. Final status matrix

Six statuses, used strictly:

- **working and verified** - measured on this machine, on the installed app, with the result recorded
- **configured but not verified** - set up, but the provider side has never been exercised
- **requires user action** - needs a human at a console with their own account; no script can do it
- **requires external approval** - a review queue at the provider decides it
- **blocked** - cannot proceed here at all, with the reason and the thing that would unblock it
- **not attempted** - deliberately not started

| Area | Status | What is verified | What would change it |
| --- | --- | --- | --- |
| Local AI | **working and verified** | 6/6 models served from D:, 37.4 tok/s at 100 % VRAM, streaming, cancellation, governor limits, one objective `succeeded` with nine usable captions | nothing |
| AI agent workforce | **working and verified** | 111 agents, 15-task objective, quality review produced a verdict, 194 executions recorded | more capable models (hardware limit, not a defect) |
| Facebook Pages | **requires user action** | listener, vault, route spec, 409 on connect, untrusted-callback rejection, GUI state | create the Meta app, register the redirect, seal the secret, connect a Page |
| Facebook analytics | **requires external approval** | analytics engine, 129-metric catalogue, adapter, availability states - all fixture-tested | App Review for insights permissions, then a grant per account |
| Instagram (both login paths) | **requires user action** | as above, plus the Reels delivery requirement is stated in the app itself | Meta app + connect an eligible professional account |
| Instagram Reels publishing | **requires user action** | delivery provider abstraction, SHA-256 verify, signed TTL URL, cleanup - fixture-tested | an S3-compatible bucket and keys |
| TikTok | **requires user action** | PKCE route, scope list, redirect URI, upload path - fixture-tested | TikTok app + connect |
| TikTok Direct Post | **requires external approval** | `video.publish` is deliberately not requested | TikTok audit + add-on approval |
| S3 media delivery | **requires user action** | `DELIVERY_NOT_CONFIGURED` explains exactly what is missing; six providers supported | create a bucket, enter keys in *Publishing → Media delivery* |
| Publishing | **blocked** | three independent gates, all fixture-tested; nothing published, ever | a connected account and a human approval |
| Analytics collection | **blocked** | engine, scheduler, four lanes, availability rules; zero observations because zero accounts | an account with a granted scope |
| Code signing | **blocked** | script path proven end to end with a throwaway certificate today | buy an OV or EV certificate |
| Installer | **working and verified (NSIS and unpacked routes)** | 45/45 with upgrade, uninstall and reinstall on 2026-11-25, with Smart App Control turned off for the run; 35/35 on the same build | a signature so SAC-enabled machines can run the NSIS stub |
| NSIS distribution | **blocked** | unsigned; SAC refused the same bytes once and accepted them later | a trusted signature |
| Clean-machine verification | **blocked** | nothing; this machine has every tool the build needs | a second Windows 11 machine with SAC on |

Numerically, at the end of this phase: **0 real connected accounts, 0 verified provider permissions,
0 live analytics collections, 0 live publications, 0 signed artefacts.** Those zeros are measurements,
not omissions.

---

## 9. What this phase changed

| Milestone | Commit | Change |
| --- | --- | --- |
| P11.1 | `b7a46fc` | Local AI activation: model path repair scripts, three contract-tolerance fixes, a deliverable that contains the work, a quality review that sees it |
| P11.2 | `49eb67e` | Activation audit and the outstanding-requirements table |
| P11.3 | this commit | Meta / delivery / TikTok / signing / campaign verification record, evidence images, this matrix |

Nothing in Phases 1-10 was rewritten, no migration was added, the database schema is unchanged at
`b8c9d0e1f2a3`, and the only rows that changed are the ones the app wrote itself (audit events, queue
jobs, one revoked probe secret).

The app was reinstalled from the rebuilt packaged tree (`scripts/install-unpacked.ps1`) so that the
installed copy and the fix are the same bytes; `production-health.ps1` then re-verified the install at
35/35, `diagnose.ps1` at 0 failures / 0 warnings with 1577 backend tests passing.
