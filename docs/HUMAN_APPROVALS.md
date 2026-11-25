# Human Approvals

Approval is the boundary between "an agent prepared something" and "a person authorised it".
Phase 4 builds the machinery; Phase 7 will use it to gate real publishing. Nothing in
Phase 4 performs an external action on the strength of an approval — the capability map
says so explicitly: `human_approvals: intent_only`.

## The record

```sql
approval_request(
  id, action, state, agent_id, task_id, objective_id,
  target_type, target_id, summary, payload_json, payload_hash,
  rationale, risks_json, expires_at, requested_by,
  decided_by, decided_at, decision_note, consumed_at, created_at, updated_at
)
```

`state` ∈ `pending | approved | rejected | expired | cancelled`.

## Hash binding

```python
payload_hash = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))
```

The hash is stored with the request. Everything that later wants to act on the approval
must pass the exact payload again:

- `verify_for_use(id, payload)` recomputes the hash and fails closed on mismatch
  (`ApprovalHashMismatch`), expiry (`ApprovalExpired`), wrong state (`ApprovalStateError`),
  or prior use (`consumed_at is not None`).
- `consume(id, payload)` verifies and then stamps `consumed_at`, making the approval
  **single-use**.

This closes the classic approval hole: approving "publish video X to Reel Y" and then
having the payload swapped for a different video or account before execution. If one byte
changes, the hash changes, and the approval no longer applies.

## Lifecycle

```
agent (human.approval.request)
      │  create(action, target, summary, payload, risks, ttl_hours=72)
      ▼
   pending ──decide(approve)──► approved ──consume(payload)──► used (consumed_at set)
      │                            │
      │                            └──payload changed──► ApprovalHashMismatch (denied)
      ├──decide(reject)──► rejected
      ├──cancel──────────► cancelled
      └──expire_stale()──► expired
```

- Duplicate suppression: creating a request whose `payload_hash` already has a **pending**
  request returns the existing one, so a retrying agent cannot spam the human.
- Expiry: `ttl_hours` (default 72) sets `expires_at`; `POST /agents/approvals/expire`
  sweeps stale pending rows and returns how many it expired.
- Decision: `decided_by`, `decided_at` and an optional `decision_note` are stored. The API
  requires an authenticated caller; agents have no tool that can decide.

## What an approval can and cannot do in Phase 4

| Can | Cannot |
|---|---|
| be requested by an agent (with rationale + risks) | be decided by an agent |
| be listed, inspected, approved, rejected, cancelled by a human | authorise an upload (no publishing integration yet) |
| be verified and consumed by future code (Phase 7) | be reused (single use) |
| expire | change its payload after approval |

`human.approval.request` is in `RESTRICTED_TOOLS` with requirement `always_human`, so even
the act of *asking* is recorded and visible.

## Why the hash includes the target

The payload stored is the full action description, including `target_type`, `target_id`,
platform, account and content hash where available. A summary string is not enough: a
human approving "publish this" must be approving a specific artifact on a specific account.

## GUI

The workforce **Settings** page shows approval state counts and the pending queue; the
Agent detail page shows an agent's approval requirement. Deciding an approval is a
deliberate click with the payload visible, and the confirm button is disabled while the
request is not pending.

## Tests

`tests/test_agent_approvals.py` (16 tests): creation + hash, duplicate suppression, get/list
filtering, approve/reject transitions, illegal transitions, expiry sweep, cancel, hash
mismatch denial, single-use consumption, double-consume denial, decision metadata, and
`verify_for_use` on every failure mode.
