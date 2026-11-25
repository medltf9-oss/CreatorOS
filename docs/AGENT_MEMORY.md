# Agent Memory

Agents need to remember things between tasks, but a shared, unbounded text dump is how
one agent's guess becomes another agent's "fact", and how a leaked key ends up in a prompt.
`services/agent_memory.py` is deliberately small: six scopes, hard size caps, expiry, and
a write path that refuses secrets.

## Scopes

| Scope | Who sees it | Default retention | Typical content |
|---|---|---|---|
| `task` | the task that wrote it | 7 days | intermediate findings, tool output digest |
| `agent` | that one agent | 30 days | personal notes, learned constraints |
| `department` | every agent in the department | 90 days | shared assumptions, brand specifics |
| `project` | every agent working on the project | 180 days | decisions, briefs, approvals |
| `global` | everyone | 365 days | house rules, platform limits |
| `long_term` | everyone | 365 days | durable knowledge worth keeping |

`MemoryScope` was widened in Phase 4 (migration `3d820ccb3d8e`); the Phase 2 enum had
only the narrower set, and widening an enum in SQLite means rebuilding the column check
constraint, which the migration does.

`SCOPE_ORDER` is the read precedence used when two scopes disagree: `task` first, then
`agent`, `department`, `project`, `global`, `long_term`. Specific beats general.

## Read isolation

`query()` composes a filter, and the isolation rules are the important part:

- `global` and `long_term` are always readable — that is what they exist for.
- `project` rows are only returned when the query names the same `project_id`; a query
  without a project never sees project memory.
- `agent` and `task` rows are only returned for the same `owner_id` (agent id), so agent A
  cannot read agent B's private notes just by asking for `agent` scope.
- `department` rows are returned for the querying agent's department.
- expired rows (`expires_at < now`) are never returned, even if not yet deleted.

A rejected extra filter is not silently ignored: the query result includes which scopes
were actually searched.

## Bounds

| Limit | Value | Reason |
|---|---|---|
| `MAX_CONTENT_CHARS` | 20 000 per entry | a single memory cannot dominate a prompt |
| `MAX_CHARS_PER_SCOPE` | 200 000 | retention enforcement drops oldest first beyond this |
| `title` | 200 chars | UI display |
| query `limit` | 1–50 (default 5) | prompt budget |

`enforce_retention()` deletes expired rows and trims each scope over budget oldest-first,
and returns what it removed so the operation is reportable, not magic.

## Secrets are rejected, not redacted

The write path runs `detect_secret()` over the title **and** content before anything is
stored. On a match it raises `SecretInMemory` and stores nothing.

Detection reuses the Phase 2 log-redaction patterns (bearer tokens, `sk_live_…`, `ghp_…`,
`xoxb-…`, `AKIA…`, private-key blocks, Meta/Instagram/TikTok token shapes, JWTs) plus an
entropy check for long random strings. The exception names the *pattern*, never the match,
so the error itself cannot leak the secret into a log or a GUI toast.

Why reject instead of redact? A redacted secret is still a corrupted memory whose meaning
is gone; an agent should not be able to launder credentials into long-term storage in the
first place. The correct behaviour is a hard failure the caller must handle.

## How the orchestrator uses it

- After a task succeeds, the artifact summary is written to `task` scope (with the
  objective id), so a later task in the same objective can be given it as upstream context.
- Department-level conclusions are written to `department` scope for reuse in later
  objectives of the same department.
- Reads are always scoped to what the agent may see; nothing is "just global".

Memory is *context*, never authority: a stored note cannot change a budget, a tool grant,
or an approval state.

## API

| Method | Purpose |
|---|---|
| `POST /agents/memory` | write (scope, title, content, project/objective, ttl) |
| `POST /agents/memory/query` | scoped read with limit and filters |
| `GET /agents/memory` | recent entries for the GUI |
| `GET /agents/memory/stats` | rows/characters per scope, expired count |
| `POST /agents/memory/retention` | run retention enforcement, returns removed counts |

## Tests

`tests/test_agent_memory.py` (19 tests): scope inclusion, owner isolation, project
isolation, expiry filtering, retention trimming, secret rejection for each pattern family,
size caps, stats, recent listing, and the "no partial write on rejection" invariant.
