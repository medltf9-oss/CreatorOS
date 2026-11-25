# Live provider verification

How to find out what actually works against a real Facebook Page, Instagram account or TikTok profile -
and how to record the answer without overstating it.

Two things are deliberately separate in this product, and this document is about the second one:

* the **automated suite** proves the code paths with mocked provider responses (`docs/evidence/P7.md`,
  `docs/evidence/P8.md`). It proves implementation, never an account.
* the **staged live verification** in `scripts/live_verify.py` talks to the provider, with a real token,
  for a real account. It is the only thing that produces "real provider tested" evidence.

---

## 1. The four classifications

Every capability in every report is stated in exactly one of these terms:

| Term | Means | Proven by |
| --- | --- | --- |
| **Implemented** | the code path exists, is wired to a route, and appears in the API | code + `docs/ANALYTICS_API.md` / route tests |
| **Fixture tested** | a test drives the path with a mocked provider answer, including the failure branches | `app/backend/tests`, `app/desktop/tests` |
| **Real provider tested** | a live call reached the provider and the provider's answer is recorded | `scripts/live_verify.py`, saved output |
| **Blocked by approval** | it needs a grant, review, credential or human decision this machine does not have | the harness `blocked` verdict, with the reason |

A capability is never described with a term it has not earned. In particular, "publishing works" is
*fixture tested* until a specific post exists at the provider.

---

## 2. The twelve steps

`scripts/live_verify.py` implements the controlled sequence. Each step prints `ok`, `blocked`,
`skipped` or `failed`, with the facts it used.

| # | Step | What it calls | What "ok" means |
| --- | --- | --- | --- |
| 1 | OAuth connection | `GET /social/accounts/{id}` | connected, token state, scope source and count |
| 2 | Account discovery | `GET /social/accounts/{id}/diagnostics` | the provider answered: token valid, app id matches, N accounts reachable |
| 3 | Permission verification | provider scope list vs the route's required scopes | every required scope was actually granted |
| 4 | Capability snapshot | `GET`/`POST /social/accounts/{id}/capabilities` | stored verdicts re-evaluated; any change is shown |
| 5 | Token lifecycle | `GET /social/accounts/{id}/token`, `GET /social/rotations` | usable now, expiry known, no unresolved rotation |
| 6 | Analytics synchronisation | `POST /analytics/sync/{id}`, `GET /analytics/runs` | a run closed with at least one observed metric |
| 7 | Media delivery | `GET /publishing/delivery`, `POST /publishing/delivery/check` | write, checksum, sign, read, delete all passed |
| 8 | Publishing preflight | `GET …/destinations/{id}/actions`, `POST …/revalidate` | the destination validates and may be queued |
| 9 | Human approval | `GET …/destinations/{id}/approval` | an approval exists **for the current payload hash** |
| 10 | Real publication | `POST …/destinations/{id}/publish-now` | the destination reached `published` with a permalink |
| 11 | Remote reconciliation | `POST …/destinations/{id}/reconcile` | the provider's current state was read back |
| 12 | Analytics for published content | `POST /analytics/sync/{id}`, `GET /analytics/publications` | the publication was discovered and has observations |

Steps that are read-only say so; step 6 and 12 read provider data and write local rows; step 7 writes
and deletes one temporary object in *your* bucket; step 10 publishes.

`failed` is reserved for a contradiction: the harness expected something the product claims and the API
disagreed (for example, the approved hash no longer matches the payload). A missing grant is `blocked`.

---

## 3. Running it

The harness refuses to run unless `CREATOROS_LIVE_VERIFY=1` is set: no scheduler, IDE run button or CI
job can make a provider call on its own.

```powershell
$env:CREATOROS_LIVE_VERIFY = "1"
$env:CREATOROS_API = "http://127.0.0.1:5000"          # the port the desktop shell printed at boot
$env:CREATOROS_SESSION_TOKEN = "<the shell's token>"

# steps 1-9 and 11-12, no publishing
app\backend\.venv\Scripts\python.exe scripts\live_verify.py --account <account-id>

# add the destination steps (still no publishing)
... --account <account-id> --destination <variant-id>

# step 10, only when you mean it: the phrase must match exactly, including the approval hash
... --destination <variant-id> --publish --confirm "PUBLISH <variant-id> <approved-hash>"
```

The harness never approves anything. Approval happens in the application (**Publishing → destination →
request approval → approve**), is bound to the payload hash, and step 9 only reports whether one exists
for the payload that is about to be published. A destination whose text changed after approval fails
step 9, which is the whole point of binding the hash.

`--wait <seconds>` (default 300) bounds each poll. `--json` prints the machine-readable step list.

---

## 4. What to do with each verdict

* **All `ok`** → the capability is *real provider tested*. Record the output in
  `docs/evidence/P10.md` under the account it ran against.
* **`blocked` on step 3** → the app has not been granted a scope. Fix the grant (App Review / Advanced
  Access / audit), reconnect, re-run. Do not "work around" it by removing the scope from the
  registration: the scope list is what the product needs to do the job.
* **`blocked` on step 7** → no usable delivery bucket. Instagram Reels cannot work until step 7 passes;
  the app blocks the destination with `DELIVERY_NOT_CONFIGURED` rather than failing at publish time.
* **`blocked` on step 9** → no approval, or the payload changed after approval. This is the system
  working. Approve the current payload if you want to publish it.
* **`blocked` on step 6** → the provider answered but every metric was unavailable. Read the per-metric
  reason on **Analytics → Availability**; a metric that is not permitted is reported as
  `not_authorized`, never as `0`.
* **`failed`** → a defect. Capture the facts block and open it as a bug; the harness is asserting
  something the product promises.

---

## 5. Recording the result

The output is evidence only if it says which account, what date, and which provider version. Keep it as:

```
docs/evidence/P10.md
  | step | verdict | account | platform | date | facts |
```

The evidence file must keep *fixture tested* rows and *real provider tested* rows visibly separate. At
the time of writing, every row is fixture tested: this machine has no Meta app, no TikTok app, no
connected account and no delivery bucket, and `scripts/activation_status.py` prints exactly that.

---

## 6. What verification cannot prove

Even a fully green run does not prove:

* **exactly-once publishing.** The providers offer no idempotency key for a create; the executor records
  an attempt ledger and reconciles ambiguity, which is the honest maximum. A retry after an ambiguous
  response may duplicate a post; that is why the destination state `unknown_remote_state` exists and why
  reconciliation runs before any retry.
* **that a metric will still exist.** Meta is retiring reach and impression metrics in June 2026 for all
  versions; the version watch in **Analytics → Settings → Provider versions** records what the provider
  documents and never silently changes a pinned version.
* **that review will be granted.** App Review and Business Verification are external decisions.
* **that an unsigned installer will be accepted by every Windows policy.** See `docs/CODE_SIGNING.md`.
