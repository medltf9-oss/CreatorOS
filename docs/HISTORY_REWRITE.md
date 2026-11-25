# History rewrite record

This repository's history was rewritten before publication to remove personal and
machine-specific data. This file records exactly what was done, why, and how it was verified.
The rewrite is destructive by nature: commit hashes changed, and the original history only
survives in a backup kept **outside** the repository (a mirror clone plus a bundle, both
verified with `git fsck` before the rewrite).

## Why

The early commits contained an absolute Windows data-root path, a personal username, a
machine hostname and local network addresses. Even in a private repository that is information
the project does not need to carry, and it would block a later publication decision.

## What ran

- `git-filter-repo` 2.x (`python -m git_filter_repo --replace-text <rules>`), applied to every
  commit on every ref. It re-encoded commit hashes, expired the reflog and ran `git gc`.
- A first pass replaced absolute project and data paths with `<PROJECT_ROOT>` / `<DATA_ROOT>`,
  the Windows profile of the build machine with `%USERPROFILE%`, the account name with `<user>`,
  and two local addresses with the RFC 5737 documentation range (192.0.2.0/24).
- That pass was too broad: a drive-letter pattern also matched URI scheme prefixes such as
  `app://creatoros` and `https://creatoros-media...`, and a PowerShell `env:` reference.
  A second corrective pass restored those exact strings in every commit.
- A third pass removed `docs/evidence/*.png` from all of history (`--invert-paths`). Those 152
  screenshots showed the local data root, the installed interpreter path and internal build
  labels; the written evidence records were kept.
- No remote existed at the time, so no force-push was possible or performed. The rewrite never
  left this machine.

## What was checked afterwards

- The intended commits, file tree, tests and documentation survived; the rewritten line has the
  same feature commits as the backup, with the same source content apart from the replacements.
- `git grep` over the working tree and over every commit's blobs finds no personal username, no
  machine hostname, no absolute build-machine data path and no local network address. Remaining `C:\Users\<user>`
  occurrences are documentation placeholders, not real accounts.
- Backend suite (`pytest -q`): 1577 passed, 9 skipped. Desktop (`node --test tests/`): 200 passed.
  `tsc --noEmit` and `vite build` are clean.

## Placeholder conventions

| Placeholder | Means |
| --- | --- |
| `%USERPROFILE%` | the current Windows user's profile directory |
| `<DATA_ROOT>` | the configured CreatorOS data root (default `%USERPROFILE%\CreatorOS`) |
| `<PROJECT_ROOT>` | the repository checkout |

These are documentation placeholders. They are never expanded at runtime; runtime code resolves
the real paths from the environment.
