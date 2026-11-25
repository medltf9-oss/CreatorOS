# ADR-24 — One canonical capability vocabulary

**Status:** Accepted, implemented and tested (Phase 6)
**Date:** 2026-11-23

## Context

Earlier phases already stored capability-ish strings. The queue/account tables and the Phase 4/5 code
used `eligible`, `missing_permission`, `needs_app_review`, `unsupported_account_type`,
`token_expired`, `rate_limited`. Phase 6 needs to answer, for every account and every capability,
"can this be done right now, and if not, why?" — and Phase 7 will ask the same question before it
publishes.

Two vocabularies in one database means two answers to the same question, and the UI ends up showing
whichever one a given code path happened to write.

## Decision

1. **Nine canonical states** (`CapabilityState`, `CANONICAL_CAPABILITY_STATES`):

   `available`, `needs_user_consent`, `requires_developer_approval`, `unsupported`, `unavailable`,
   `unknown`, `expired`, `revoked`, `manual_only`.

2. **Legacy members are retained** as enum members so old rows still parse, and
   `normalize_capability_state()` maps them onto the canonical set **at every boundary**
   (`LEGACY_CAPABILITY_STATES`: `eligible`→`available`, `missing_permission`→`needs_user_consent`,
   `needs_app_review`→`requires_developer_approval`, `unsupported_account_type`→`unsupported`,
   `token_expired`→`expired`, `rate_limited`→`unavailable`). The engine only ever emits canonical
   states.

3. **Verdicts are computed by a pure function** (`capability_rules.evaluate`) from stored facts —
   route, account type, granted scopes, token state, app mode — in a fixed order (route → manual →
   account type → dead token → all scopes granted → app approval → dev mode → consent). Same input,
   same output, and every verdict carries a human reason plus the evidence it used.

4. **Unreachable capabilities are reported, not hidden.** `capabilities_for()` deliberately does not
   filter by route: a capability this login path cannot reach is stored as `unsupported` with a
   reason. Hiding it would make "Instagram insights is missing" invisible instead of explained.

5. **`headline=false`** for capabilities that need a permission the connect flow does not request
   (`read_insights`, `instagram_business_manage_insights`). They appear with their reason but do not
   drag the account's overall state down, because they can never be available at connect time.

6. **Two facts are kept separate**: `connection_status` is credential health; capability states are
   permission truth. An account with a working token and a missing permission is `connected` with a
   `needs_user_consent` capability.

7. **Per-route scope vocabularies** are data (`CapabilityDefinition.route_scopes` +
   `scopes_for(route)`), because Instagram has two (`instagram_business_*` vs `instagram_*`).

8. **History is append-only**: `account_capability` holds the current verdict per (account,
   capability); `capability_snapshot` records each evaluation with its trigger.

## Consequences

**Good**

* One question, one answer: Phase 7 can branch on the state without knowing which phase wrote it.
* "Why is this disabled?" is answered by a stored reason string written at evaluation time, not
  reconstructed later from guesses.
* The GUI can render a matrix where an em dash (no record) is visibly different from `unsupported`.
* Legacy rows keep working without a data migration of every historical string.

**Costs**

* Two-step reading: a legacy string in an old row must go through `normalize_capability_state`, so
  every read path has to remember to do it (the services own the read paths, which is why this is a
  rule rather than a convention).
* The catalogue is now the single place that must be updated when a provider adds a permission; a
  missing entry means a capability nobody can see. Mitigated by `test_social_capabilities.py`
  asserting the catalogue/state coverage and by the matrix page listing the whole catalogue.
* `headline=false` is a judgement call per capability and is documented in `ACCOUNT_CAPABILITIES.md`.

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Keep the legacy vocabulary | two vocabularies in the DB, ambiguous UI, Phase 7 guessing |
| Hard-migrate every stored string | rewrites history rows; a reversible mapping achieves the same at read time |
| Store a boolean `can_publish` | loses the reason; a disabled button with no explanation is exactly what this project forbids |
| Filter capabilities by route | silently hides "unsupported" instead of reporting it |
| Evaluate lazily in the UI | puts the rules in the renderer, where they cannot be tested or audited |

## Evidence

* `tests/test_social_capabilities.py` — 18 tests: every state reachable with the right reason, legacy
  normalization, headline behaviour, no route filtering, Instagram per-route scopes, snapshot
  append-only.
* `GET /social/capabilities/matrix` on the live backend returns the catalogue and per-account states
  (screenshot in `docs/evidence/P6.md`).
* `docs/ACCOUNT_CAPABILITIES.md` is the normative description.
