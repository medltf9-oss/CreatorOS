# ADR-19 — Media storage layout and import pipeline

* Status: Accepted
* Date: 2026-11-22
* Phase: 5
* Supersedes: nothing. Extends the Phase 2 `media_asset` table.

## Context

Phase 2 created `media_asset` with a `source_path` column but no policy for where files live,
how they are named, or how a partial import is recovered. Phase 5 must import multi-gigabyte
video into a local-first app on a single Windows volume, with no second database and no second
queue.

Two failure modes are unacceptable here:

1. A user filename or a rendered path escapes `data/media/` (accidental or malicious).
2. An interrupted copy leaves a half-file that the database believes is complete.

## Decision

**Content-addressed storage under `data/media/`, one directory per purpose, atomic promote.**

* `originals/<sha[0:2]>/<sha256><ext>` — the only place an imported source lives.
* `thumbnails/`, `previews/`, `proxies/`, `transcodes/`, `audio/`, `waveforms/`, `staging/`,
  `quarantine/` — every one is a `PathKind` in `core/settings.py`; no component builds a path
  from a string.
* The filename is metadata only. It appears in the UI and in `original_filename`, never in a
  filesystem path.
* Import order is: validate → hash → dedupe → copy to `staging/` while re-hashing → `os.replace`
  into `originals/` → probe → insert row → generate derivatives.
* `os.replace()` is atomic on NTFS within a volume, so a crash leaves either a staging file
  (swept later) or a complete original — never a truncated one that a row points at.
* Dedupe is a SHA-256 lookup, which makes a repeat import a metadata operation (299 ms
  measured) instead of a copy.
* Symlinks, directories, oversized files and files outside the root are rejected before any
  byte is written.

## Consequences

**Good**

* No collision handling, no "copy (2).mp4", no path traversal via filename.
* Import is idempotent by construction; `media_asset.sha256` stays the identity.
* Backups and the recovery scan can reason about directories instead of guessing.
* Deleting a derivative can never touch an original because they live in different areas.

**Costs**

* Two files that differ by one byte are two assets — correct, but dedupe cannot merge near
  duplicates. Near-duplicate detection remains an agent task (`duplicate_content_agent`), not
  a filesystem decision.
* Sharding means the same asset's derivatives sit in several directories; the Storage page
  aggregates by area rather than per asset.
* A rename in the UI changes metadata, not the file — which is the point, but it means the
  on-disk name is not human-readable. The detail page always shows the original filename and
  the stored path side by side.

## Verification

* `tests/test_media_storage.py` (16 passed, 1 skipped) — hash paths, atomic promote, symlink
  and oversize rejection, space guard, sweep, containment.
* `tests/test_media_import.py` (18) — real imports, duplicate detection, rejection of a fake
  `.mp4`, same-name/size preview path.
* `tests/test_media_migration.py` (5) — real-database copy upgrade, integrity, FK checks,
  downgrade.
