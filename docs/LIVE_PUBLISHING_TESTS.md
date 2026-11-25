# Live publishing tests (opt-in, never automatic)

Nothing in this repository publishes to a real account on its own. Live verification is a deliberate,
manual act with several locks, because a live test that fires by accident is a public post.

## What is verified without a provider

Every publishing path is covered by fixtures (`httpx.MockTransport`) — 246 publishing tests in total.
They assert request shapes, provider-offset resumption, error classification, state transitions and the
ledger rows. They are evidence of *implementation*, and `docs/evidence/P7.md` labels them as such.

## What requires a live account

| Path | Needs |
| --- | --- |
| Facebook Page video | a Meta app, `pages_manage_posts` (app review for production), a Page the user administers |
| Instagram Reels | the above plus an Instagram professional account and a configured delivery bucket |
| TikTok Direct Post | a TikTok app with the Content Posting API add-on (`video.publish`) |
| TikTok Inbox | a TikTok app with `video.upload` only |

None of these exist on this machine, so **no live publication has been performed or claimed**
(`docs/evidence/P7.md`, category D).

## The harness

`scripts/live_publish.py` refuses unless every lock is open:

1. `CREATOROS_LIVE_PUBLISH=1` in the environment — a per-shell opt-in;
2. `--variant <id>` names a destination that exists, is not already published, and whose platform
   `/version` reports as `available`;
3. `--confirm "<phrase>"` matches exactly the phrase the script prints, which includes the destination
   id and the approval payload hash — so a command copied from a log cannot fire;
4. `CREATOROS_API` and `CREATOROS_SESSION_TOKEN` are set.

It prints the platform, mode, account, caption preview, approval state and payload hash, then queues the
destination through the same route the GUI uses and polls until a terminal state, printing each state
change and the attempt ledger.

```
set CREATOROS_LIVE_PUBLISH=1
set CREATOROS_API=http://127.0.0.1:<port>
set CREATOROS_SESSION_TOKEN=<token printed at boot>
python scripts/live_publish.py --variant 01M8... --confirm "PUBLISH 01M8... a1b2c3d4"
```

Exit codes: `0` published and confirmed; `1` not published, or unknown remote state (reconcile first);
`2` refused, with the reason (which lock was closed).

## Reading the result honestly

* `RESULT: published and confirmed by the provider` — the provider returned an id and, where it offers
  one, a permalink.
* `RESULT: unknown remote state` — the request may have been processed. Run the reconciliation check in
  the GUI or `POST /publishing/destinations/{id}/reconcile`; do **not** retry blindly.
* `RESULT: not published` — the attempt ledger says why; the destination keeps its category.

A live test does not prove exactly-once, because these providers cannot offer it. It proves that one
publication went out with the content that was approved.
