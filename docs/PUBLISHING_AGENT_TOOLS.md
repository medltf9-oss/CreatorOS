# Publishing agent tools

The 111-agent workforce can **see** the publishing queue and **draft** a destination. It cannot
approve, schedule a run, retry, cancel or publish. That is a product decision (D4: publishing is a
human action) and it is enforced by tests rather than by comment.

## The three tools

| Tool | Category | Approval | What it does |
| --- | --- | --- | --- |
| `publishing.queue.read` | read | none | destinations with state, the reason for that state, schedule, attempts, permalink |
| `publishing.destination.read` | read | none | one destination's approval, attempt, delivery and reconciliation ledgers |
| `publishing.draft.create` | write | on-write | creates an intent (or uses one) plus one **draft** destination, with field provenance `agent:<id>` |

`publishing.draft.create` returns `next_step: "A human must request and grant approval; nothing is
uploaded until they do."` so a caller cannot read the result as "it is going out".

## What is not expressible

* No tool id contains `.publish`, `.approve`, `.retry`, `.cancel` or `.schedule-run`; a test asserts it.
* No authorisation payload can name an approval decision, a queue enqueue or an executor call.
* `toolbox.py` contains no reference to `PublishExecutor`, `PublishActions`, `.enqueue(` or
  `publish_now`; a test asserts it.

## Grants

`services/publishing/agent_grants.py`, key `publishing.tool_grants_v1`:

| Department | Tools |
| --- | --- |
| `strategic_intelligence`, `platform_adaptation`, `quality_governance`, `analytics_growth` | both reads |
| `publishing_operations` | both reads + `publishing.draft.create` |

The grant is additive, idempotent and audited, exactly like the Phase 5 media and Phase 6 social
grants: existing tool lists keep their order, nothing is ever removed, and the applied flag lives in
`app_setting`.

## Reading a tool result

`publishing.queue.read` returns

```json
{
  "items": [{"job_id": "...", "platform": "facebook", "state": "draft",
             "state_reason": "...", "scheduled_at_utc": null, "attempts": 0,
             "permalink": null, "needs_human": false}],
  "count": 1,
  "late_policy": {"policy": "ask", "grace_seconds": 120},
  "note": "An agent can read this queue. Only a human can approve or publish from it."
}
```

`publishing.destination.read` returns the same redacted projection the GUI gets, with instants as ISO
strings (tool output is persisted as JSON, so a `datetime` there would fail at write time — a real bug
that the tests caught).

## Tests

`tests/test_agent_publishing_tools.py` (11 tests): the tool categories and risks, the absence of any
publishing verb, the toolbox source scan, the queue projection (no path, no token), state filtering
including a rejected unknown state, the destination ledger, the draft's provenance and its untouched
approval fields, grant idempotence, and that a grant never removes a tool.
