# ADR-35 — Live verification is opt-in, staged, and never approves anything

**Status:** Accepted, implemented and exercised (`scripts/live_verify.py`, 20 tests in
`app/backend/tests/test_live_verify_harness.py`, protocol in `docs/LIVE_PROVIDER_VERIFICATION.md`).
**Date:** Phase 10.

## Context

Every provider-facing capability in this product is fixture-tested and none of it is real-provider
tested. The gap is not code; it is a procedure. Without one, three failure modes are all likely:

1. **The harness that cannot fail.** A script that prints "OK" because it caught an exception, or that
   reads a field the API does not return and treats `null` as success, produces a comfortable summary
   and no information. The whole point of Phase 10 is that this is not acceptable.
2. **The harness that publishes by accident.** A live test that ends with a real post, run by a
   scheduler, an IDE run button or a curious person at the wrong moment.
3. **The harness that approves.** A test that "requests approval" to get past a gate has replaced the
   human decision it was supposed to verify, and afterwards nobody knows whether the approval was real.

## Decision

**One script, twelve ordered steps, four verdicts, two independent locks.**

* The twelve steps are the ones the mission lists: connection, discovery, granted permissions,
  capability re-evaluation, token lifecycle, analytics sync, media delivery, preflight, approval,
  publication, reconciliation, analytics for the published object.
* Each step reports `ok`, `blocked`, `skipped` or `failed`. `failed` is reserved for a contradiction -
  the harness expected something the product claims and the API disagreed. A missing grant is
  `blocked`, because "Meta has not approved Advanced Access yet" is a fact, not a defect.
* The whole script refuses unless `CREATOROS_LIVE_VERIFY=1` is set in the invoking shell. No
  scheduler, no IDE run button, no CI pipeline can inherit that accidentally.
* Publication (step 10) additionally requires `--publish` **and** the exact phrase
  `PUBLISH <destination-id> <approved-payload-hash>`. A copy-pasted command from a log cannot fire,
  because the hash changes with the content.
* The script **never** calls an approval route. Step 9 reads the approval and fails the run when the
  approved hash no longer matches the payload. Approval stays a human decision made in the app, which
  is the thing being verified.

## Consequences

* A run is evidence. `docs/evidence/P10.md` records the first real run against a live backend, which
  reported 5 blocked, 4 skipped, 1 ok and exited 1 - exactly right for an installation with no provider
  app.
* The verdicts are stronger than a boolean: `blocked` carries the provider's own reason code
  (`NOT_AUTHORIZED`, `VAULT_UNAVAILABLE`, `DELIVERY_NOT_CONFIGURED`), which is what makes the next
  action obvious.
* The harness is tested against the real routes through `TestClient`, so a misspelled response field
  fails a test rather than producing a false pass. Writing it that way caught one real defect
  immediately: step 5 originally trusted `GET /social/accounts/{id}/token`, whose state is computed
  from stored facts alone, and therefore reported "token valid" while the vault could not decrypt the
  credential at all. It now cross-checks `secret_health`.
* Two things the harness cannot do, and does not pretend to: it cannot make a provider grant a
  permission, and it cannot prove exactly-once publishing (the providers offer no idempotency key for a
  create; the product reconciles instead).
