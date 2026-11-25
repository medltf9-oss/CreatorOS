# Scheduling and missed runs

## How a time is stored

`scheduled_at_utc` is an absolute instant; `display_timezone` is the IANA zone the person chose. The GUI
renders the instant in that zone. Storing a wall-clock time instead would move a 09:00 post by an hour
at a daylight-saving boundary, which is the bug this split exists to prevent.

`PublishScheduler.schedule()` refuses a naive timestamp (`INVALID_SCHEDULE`) and an unknown zone
(`INVALID_TIMEZONE`) rather than guessing.

## Changing the zone

`reschedule_local()` keeps the **wall clock** and recomputes the instant: 09:00 Paris becomes 09:00 New
York. That is what a person means by "my timezone changed". The audit row records both instants and both
zones.

## The sweeper

A dedicated thread (`creatoros-scheduler`) runs `tick()` every `CREATOROS_SCHEDULER_SECONDS` (default
15, `0` disables). It owns a fresh session per sweep, and an exception in one sweep — including a failing
`close()` — cannot kill it. `POST /publishing/schedule/tick` runs exactly the same code on demand.

`tick()` is idempotent: the queue's idempotency key is per destination (`publish:<job_id>`), so sweeping
twice queues once. That is what makes a restart safe.

## What happens when a time is missed

"Missed" means the instant passed by more than the grace window (default 120 s) — typically because the
application was closed. The policy decides:

| Policy | Behaviour | Why it exists |
| --- | --- | --- |
| `ask` (default) | `requires_user_action`, `state_reason = missed_schedule`, `missed_at` set, nothing queued | Publishing 14 hours late is a decision a person should make |
| `publish_now` | queued immediately, `missed_at` set, audit row records how late | For a team that would rather be late than absent |
| `skip` | `requires_user_action`, `state_reason = missed_schedule_skipped`, nothing queued | Stops, and the GUI proposes a new time first |

`ask` and `skip` both stop and wait; they differ only in what the GUI offers first. **None of the three
publishes silently.**

A missed destination with no approval is not queued under any policy: it is recorded as
`missed_schedule_no_approval`, because a job that would immediately refuse itself is noise.

## Answering the question

* `POST /publishing/destinations/{id}/publish-missed-now` — "publish it now"
* `POST /publishing/destinations/{id}/schedule` with a new time — "reschedule"
* Both refuse with `NOT_A_MISSED_SCHEDULE` if the destination never missed anything.

## Tests

`tests/test_publishing_scheduler.py` (20 tests): future, due-within-grace, missed under all three
policies, no-approval cases, tick-twice-queues-once, wall-clock preservation across zones, naive and
unknown-zone refusal, and a live sweeper thread that survives an exception.
