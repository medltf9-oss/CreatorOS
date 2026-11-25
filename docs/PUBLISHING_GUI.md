# The publishing GUI

Ten pages under the **Publishing** section of CreatorOS Studio. Every one reads real data from the API
above; none of them fakes a state.

The rule the whole section is built on: **a control works, or it is disabled and says why.** The
backend supplies the reasons (`GET /publishing/destinations/{id}/actions` returns `enabled` plus a
`reason` for each verb), so the renderer never has to invent a sentence and can never offer something
the API would refuse.

## Pages

| Page | What it answers | Honest states you may see |
| --- | --- | --- |
| **Publishing** (overview) | how much is open, what needs me, what is published, is delivery configured | "Nothing has been composed yet", "No accounts connected", "Media delivery: not configured — Instagram is blocked" |
| **Compose** | the intent list, and creating one | empty list with a hint; platform filter |
| **Intent detail** | one intent and its destinations | "No destination yet"; the Add form's account select is disabled with "connect an account first" |
| **Approvals** | approve or reject exactly what would be posted | the full payload (caption, account, surface, validation, media hash, schedule, payload hash); "Nothing is waiting" |
| **Publish queue** | what is uploading, and what is unknown | progress from provider offsets; "Every destination has a known state"; Check provider disabled without a provider id |
| **Schedule** | upcoming, missed, and the late policy | three policies, each stating what it does; "Nothing scheduled" |
| **Published** | the confirmed publications | "Nothing published yet" — only provider-confirmed rows; "no permalink" when the provider returned none |
| **Destination detail** | the whole truth about one destination | attempt, delivery and reconciliation ledgers; actions with reasons; manual resolution needs a note |
| **Needs you** | everything stopped, with its reason | each row shows the backend's `state_reason` |
| **Media delivery** | the bucket Instagram fetches from | "not configured" with the missing pieces listed; credential id, never a key |

## Screenshots

Taken against the real database on this machine, where no provider app and no bucket exist — so the
disabled-with-reason states are the ones you see:

* `<DATA_ROOT>\temp\p7-pub-overview.png`
* `<DATA_ROOT>\temp\p7-pub-compose.png`
* `<DATA_ROOT>\temp\p7-pub-intent-detail.png`
* `<DATA_ROOT>\temp\p7-pub-approvals.png`
* `<DATA_ROOT>\temp\p7-pub-publish-queue.png`
* `<DATA_ROOT>\temp\p7-pub-schedule.png`
* `<DATA_ROOT>\temp\p7-pub-published.png`
* `<DATA_ROOT>\temp\p7-pub-needs-you.png`
* `<DATA_ROOT>\temp\p7-pub-media-delivery.png`

## What the pages deliberately do not do

* **No "publish" button in the composer.** Drafting and publishing are different pages, because they
  are different decisions.
* **No percentage for provider-side processing.** The provider reports a status string; inventing a bar
  would be a lie.
* **No cancel once the post may exist.** The button is disabled with "reconcile before cancelling",
  because "cancelled" would otherwise be false.
* **No automatic refresh loop.** Every page has a Refresh control, and the version that guards a
  decision (approval, cancel, retry) reloads after the action so the next state is the real one.
* **No credential field anywhere.** The delivery page takes a *vault secret id*; the value is sealed by
  the Electron shell (ADR-22).

## Verifying a page

```
cd <PROJECT_ROOT>\app\desktop
pnpm dev                      # with CREATOROS_DEBUG_PORT=9222 for the driver
python <PROJECT_ROOT>\scripts\ui_drive.py clickx "Publishing" 2
python <PROJECT_ROOT>\scripts\ui_drive.py shot <DATA_ROOT>\temp\out.png
```
