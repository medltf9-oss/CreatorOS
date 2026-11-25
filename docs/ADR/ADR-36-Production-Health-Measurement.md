# ADR-36 — Production health is measured on the installed app, from the shortcut, and it can fail

**Status:** Accepted, implemented and exercised (`scripts/production-health.ps1`,
`app/desktop/scripts/ui_health.cjs`, `docs/PRODUCTION_READINESS.md`). **Date:** Phase 10.

## Context

`scripts/verify-install.ps1` (Phase 9) proves the installer produces a working install: the app starts,
the backend serves, the shortcuts exist, the database is at head. It does **not** answer the questions a
production release actually raises:

* does every module open, or only the three that were screenshotted?
* does closing the window leave an orphan holding the database?
* does a launch/close/upgrade cycle mutate or lose data?
* does the credential vault work from the place that uses it - the renderer - or only from the backend?

Phase 9 answered these by inspection and screenshots. Inspection is not a gate: it does not fail a
build, and it does not survive the next change.

## Decision

**Launch the installed product the way a user does, and measure the things a user would notice.**

1. **Launch from `CreatorOS.lnk`**, not from the exe path. The shortcut is the artefact on the Desktop;
   if it is broken, the product is broken, and only a run from the shortcut can see that.
2. **Reach the backend the way the app does.** The per-launch session token is deliberately never
   written to disk (ADR-07), so a second process cannot authenticate to the API. The token is read from
   the app's own preload bridge over CDP (`window.creatoros.bootstrap()`), which is the same call the
   renderer makes. This is not a backdoor: it runs as the same user, with the same privileges, against
   the same app.
3. **Define page failure as the app's own error surface.** Every page on a machine with no connected
   accounts legitimately shows empty states, so "is there text on screen" passes everything. The sweep
   counts a page as failed when the app renders its `ErrorNote` card (a single, identifiable component),
   reads under 80 characters, or logs a console error.
4. **Prove the sweep can fail.** `ui_health.cjs --self-check` injects that exact DOM, asserts the probe
   reports it, removes it and asserts the probe is clean again. A gate that has never failed is not
   known to be a gate.
5. **Fail on data drift, but name the exceptions.** Row counts are compared before and after; audit
   events, queue jobs and one revoked `secret_ref` (the vault probe's own record, kept for the audit
   trail) are allowed to grow, and any other change fails the run.
6. **Keep the destructive paths opt-in.** `-IncludeUpgrade` and `-IncludeUninstall` are separate
   switches; the default run touches nothing but its own probe secret.

## Consequences

* One command answers "is this installation healthy": 35 checks, 39 with the upgrade path, 40 with the
  uninstall path. Measured today: all pass.
* The sweep found nothing, which is itself the useful result: 49/49 pages render on the installed
  build, with no console errors, on a machine whose data is nearly empty - the state most likely to
  break a page with a null-handling bug.
* Writing the vault probe exposed a real design fact that a backend-only test would have hidden:
  sealing happens in the Electron main process, so `POST /social/vault/secrets/reserve` alone proves
  nothing. The probe now runs reserve → DPAPI seal → adopt → read → unlink → destroy in the renderer.
* The data-drift check produced a real finding too: `DELETE /social/vault/secrets/{id}` revokes the row
  and keeps it, rather than removing it. The check now allows exactly that one-row delta, which is a
  stronger assertion than skipping the table.
* Still not covered, and stated in `docs/PRODUCTION_READINESS.md`: a clean Windows machine with no
  development tooling. That is the only place "will a stranger be able to install and run this?" can be
  answered, and this machine cannot be that machine.
