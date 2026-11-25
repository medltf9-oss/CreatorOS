# Upgrading CreatorOS

The application and your data are separate. Upgrading replaces the application; it never touches
`<PROJECT_ROOT>` except to apply migrations to the database, which are additive and reversible.

## What happens on an upgrade

1. **Back up first.** The installed app can do it for you: *Studio → Settings → Backups* → create a
   backup. Or copy `<DATA_ROOT>\data\database\creatoros.db` (and `-wal`/`-shm` if present) somewhere
   safe. Phase 9 changes no schema, so an 0.8.0 upgrade applies **no** migration.
2. Quit CreatorOS. A running app holds the database and the local models.
3. Install the new version over the old one (the installer keeps your install folder) or run
   `scripts\install-unpacked.ps1` again.
4. Start CreatorOS. The shell runs `migrate` before it starts the API and refuses to start if the
   migration fails. The result is in `<data root>\logs\backend\backend.log` (`migrate exited code=0`).

Your accounts, tokens, media, campaigns, analytics history and recommendations are untouched. Tokens
stay decryptable because the credential vault's master key lives in Electron's `userData`
(`%APPDATA%\creatoros-desktop\Local State`), whose directory name is pinned in
`electron/paths.cjs` — a product rename must not orphan it.

## Version compatibility

| Release | Database head | Backend | Notes |
| --- | --- | --- | --- |
| 0.8.0 | `b8c9d0e1f2a3` (48 tables) | Python 3.12.10 bundled | Phase 8 analytics complete; Phase 9 packaging |
| 0.7.x | `b8c9d0e1f2a3` | development venv | same schema |

`GET /version` reports `api`, `version` and the analytics capabilities; the backend's
`python -m creatoros version` reports the database revision and the migration head. If they differ,
the app did not migrate — check the log before using it.

## Rolling back

* **Application only:** install the previous release. If the newer release applied a migration, the
  older code will not understand the schema; restore the database backup you made in step 1 instead.
* **Data:** stop the app, replace `creatoros.db` with your backup, start the app. Media is never
  rewritten by an upgrade, so a database backup is sufficient.
* **Analytics retention** is the only feature that deletes data, and it is preview-first, needs the
  word `DELETE` typed, and audits every execution. An upgrade never triggers it immediately: it runs
  on the next daily maintenance tick, under the policy stored in settings.

## Upgrading from a development checkout

If you were running `app\backend\.venv` and Electron from source, nothing is lost by installing the
packaged app: it reads the same `<PROJECT_ROOT>` root. Keep the checkout for development; both can exist
as long as only one runs at a time (the shell takes a single-instance lock per user session, and two
backends against one SQLite file is how a WAL database gets corrupted).

```powershell
# rebuild and reinstall from source
powershell -File scripts\build-release.ps1
powershell -File scripts\install-unpacked.ps1
```
