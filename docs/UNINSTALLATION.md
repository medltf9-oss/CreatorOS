# Uninstalling CreatorOS

CreatorOS separates the **application** from your **data root**, and uninstalling removes only the
application.

## Installer-made installation

*Settings → Apps → Installed apps → CreatorOS → Uninstall*, or run
`%LOCALAPPDATA%\Programs\CreatorOS\Uninstall CreatorOS.exe`.

The uninstaller:

* closes nothing you did not already close (quit the app first; the installer's own shortcut does not
  force-quit it),
* removes the application directory,
* removes the Desktop shortcut and the Start Menu entry,
* removes the uninstall registry entry.

A silent uninstall returns before it has finished (`Un_A.exe` continues in the background), so if you
are scripting it, poll for the directory to disappear rather than sampling once.

## Fallback installation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall-unpacked.ps1
```

It closes the app, removes both shortcuts, the "Installed apps" entry and
`%LOCALAPPDATA%\Programs\CreatorOS`, and then prints the data root it did **not** touch.

## What is left behind, deliberately

| Path | What | Why it stays |
| --- | --- | --- |
| `<PROJECT_ROOT>` (or `CREATOROS_ROOT`) | database, media, models, credentials, logs, backups | deleting it destroys work CreatorOS cannot regenerate |
| `%APPDATA%\creatoros-desktop` | Electron profile: window state, and the DPAPI master key that decrypts your stored provider credentials | a reinstall keeps your connected accounts working; deleting it makes existing encrypted blobs unreadable |

### Removing everything

Only when you really mean to lose the work:

```powershell
# 1. make a copy first if there is any doubt
Copy-Item '<DATA_ROOT>\data\database\creatoros.db' "$env:USERPROFILE\Desktop\creatoros-final.db"

# 2. the data root
Remove-Item '<PROJECT_ROOT>' -Recurse -Force

# 3. the Electron profile (credential master key, window state)
Remove-Item "$env:APPDATA\creatoros-desktop" -Recurse -Force
```

Step 3 is what makes your existing encrypted credentials unrecoverable; skip it if you plan to
reinstall and want your connected accounts to keep working.

## Before you uninstall

* Analytics history is only in the database. Reports are files under
  `<DATA_ROOT>\data\media\exports\reports`; copy them out if you want them.
* An in-flight publish or media job is lost with the app. Let the queue drain
  (*Studio → Overview → Queue*) before uninstalling.
* Nothing in the removal path deletes data under the data root, and the uninstaller never asks for
  administrator rights.
