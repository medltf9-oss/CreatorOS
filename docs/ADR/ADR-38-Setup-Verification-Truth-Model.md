# ADR-38 — Setup verification is a recorded fact, and completion cannot outrun it

**Status:** Accepted, implemented and tested in Wave 2.
**Date:** 2026-11-25
**Supersedes:** nothing. Refines ADR-37 (first-run setup and pinned prerequisites).

## Context

ADR-37 promised that the first-run page would distinguish *present* from *verified* from *could not
check*. The first implementation did not deliver the middle state: `GET /setup/status` hard-coded
`verified = False`, verification results were discarded after the response, and FFmpeg presence
compared size only. A file could be replaced with equal-size bytes and still look green, and a
successful hash of 1.8 GiB left no trace on the next screen.

## Decision

1. **A verification that finishes is recorded.** The backend stores, per component, the path,
   size, `st_mtime_ns`, SHA-256, signer subject, timestamped flag and time in `app_setting`
   (`setup.verification`). Failed verifications are recorded too; a failure can explain itself.
2. **`verified` requires a passing record that still matches.** Status compares the recorded size
   and modified time with the file now. Only then is the state `verified`. A changed file becomes
   `unverified` and the detail names what changed. Presence without a record is `ok` — stated as
   "present, not verified" — never green.
3. **Status never re-hashes.** A page refresh must not read 1.8 GiB. The record is a fingerprint of
   one byte-level run, not a live proof; the UI and API say "recorded verification" in those words.
   `/verify` always re-hashes.
4. **Verification is bounded.** Targets must be regular files; symlinks and reparse points are
   refused (422 `VERIFY_SYMLINK` / `VERIFY_NOT_REGULAR_FILE`), files over 4 GiB are refused
   (`VERIFY_TOO_LARGE`), runs are serialised by a module lock, and a 120 s deadline returns 504
   `VERIFY_TIMEOUT`. A refused or timed-out attempt writes no record.
5. **Completion cannot outrun a failing required check.** `POST /setup/complete` returns 409
   `REQUIRED_CHECKS_FAILING` when a required check (`data.root`, `media.ffmpeg`,
   `prerequisites.manifest`) fails. The user may override only through the explicit
   `override_required_checks` request field; the override is stored and audited, and it does not
   change the check results.
6. **Completion is idempotent and upgrade-safe.** The first `completed_at` and
   `completed_version` survive later calls; notes are kept unless replaced. An upgrade does not
   reopen the wizard: status reports `version_changed` and a `setup.version_changed` warning, so
   the user is informed without losing the recorded decision.

## Consequences

- The API and the page can now show six distinct situations: verified (recorded), present not
  verified, unverified because the file changed, verification failed, missing, and unknown.
- A verification record is evidence of what one run observed, not an authorisation. Nothing is
  installed, executed or enabled because a component is verified.
- Accepted limitations, recorded in `docs/release/V1_RELEASE_BLOCKERS.md`:
  the fingerprint check and the hash are not atomic (TOCTOU); a same-size swap with a forced-equal
  `st_mtime_ns` stays `verified` until the next `/verify`; a recorded failure does not carry its
  check ids; refused and timed-out attempts are not audited; `ffprobe` is still not verified.
- The desktop contract is additive: `verification`, `version_changed`, `override_required_checks`.
  Older clients keep working.

## Alternatives rejected

- **Re-hash on every status request.** Correct but unusable: a 1.8 GiB hash per page load, and an
  easy denial of service.
- **Keep verification only in memory.** `verified` would disappear on restart, which is the defect
  this ADR fixes.
- **Treat size equality as integrity.** Demonstrably false for equal-size tampering; removed from
  the FFmpeg presence path.
- **Reopen the wizard on every version change.** Discards a recorded user decision and nags on patch
  releases; a non-blocking notice carries the same information.
- **Silently complete with failing checks.** Makes the green state meaningless; an explicit,
  audited override preserves user agency without lying.
