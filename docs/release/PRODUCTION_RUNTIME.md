# Packaged production runtime and dependency inventory (Wave 3)

Scope: validate the minimized backend runtime that the installer ships, and compare the packaged
dependency inventory with the artifact SBOM. Tool: `scripts/runtime-inventory.py` (new, standard
library only, deterministic JSON + Markdown, exit 0 clean / 1 finding / 2 cannot inspect). Tests:
`app/backend/tests/test_runtime_inventory.py`, **14 passed**.

## Verdict

| Question | Answer |
| --- | --- |
| Does the current build script produce a minimized bundle? | **Yes, after three fixes recorded below.** 60.4 MB, 2,674 files, 34 runtime distributions, 0 development-only distributions, inventory exit 0. |
| Was the shipped 0.8.0 unpacked payload minimized? | **No.** It contains 52 distributions, 18 development-only ones and a duplicate `pip`. It predates the current pruning and is not what a 1.0.0 build produces. |
| Is the artifact SBOM bound to the packaged bytes? | **No.** It is generated from `app/backend/.venv`, so it lists 17 development components that a clean payload does not contain. This is an open 1.0.0 requirement, not a cosmetic difference. |
| Does the CPython tree keep its vendor signature? | Not checked by this inventory; `build-backend.ps1` checks `python.exe`'s Authenticode status (Valid, Python Software Foundation) during the build. |

## What ships

The backend is not frozen with PyInstaller. It is a signed CPython 3.12.10 tree (`python.exe`,
`python3.dll`, `python312.dll`, `vcruntime140*.dll`, `DLLs/`, `Lib/`) plus the `creatoros` package,
migrations, `alembic.ini` and `PYTHON-LICENSE.txt`. No compiler and no bootloader executable is
shipped. `scripts/build-backend.ps1` prunes the developer environment, copies the package, and runs a
migrate + `/health/live` smoke test before declaring success.

## Evidence: three builds compared

| Payload | Files | Bytes | Distributions | Development-only | Inventory exit |
| --- | ---: | ---: | ---: | ---: | ---: |
| `releases/0.8.0/win-unpacked/resources/backend` (shipped audit payload, stale) | 5,689 | 98,640,439 | 52 | 18 (7 on the hard forbidden list) | 1 |
| Fresh build before the fixes | 3,456 | 64,644,874 | 38 | 4 (metadata only) | 1 |
| Fresh build after the fixes (`installer_builds/wave3-check`) | 2,674 | 63,384,618 | 34 | 0 | 0 |

The 0.8.0 payload's findings: `coverage`, `pip` (twice: 25.0.1 and 26.3), `pyinstaller`, `pytest`,
`ruff`, `setuptools` on the hard forbidden list; `altgraph`, `iniconfig`, `packaging`, `pefile`,
`pluggy`, `Pygments`, `pyinstaller-hooks-contrib`, `pytest-asyncio`, `pytest-cov`, `pywin32-ctypes`,
`respx` as wider development-only packages. Its RECORD hashes matched (0 mismatches), so the bytes
are internally consistent — the problem is what was shipped, not corruption.

## Defects found and fixed in `scripts/build-backend.ps1`

1. **dist-info names are normalized (PEP 503).** The metadata removal matched `pytest-asyncio-`,
   but the directory is `pytest_asyncio-1.5.0.dist-info`, so four development distributions stayed
   discoverable through `importlib.metadata` even though their modules were gone. The pattern now
   accepts `-` or `_`, and the build refuses to finish if any dev metadata remains.
2. **Two module names were wrong.** `pyinstaller-hooks-contrib` installs `_pyinstaller_hooks_contrib`
   (leading underscore) and `pywin32-ctypes` installs `win32ctypes` (not `pywin32_ctypes`); the
   previous names removed only the metadata, leaving 750 files (680 KB) of dead code.
3. **Single-file modules were never removed.** `pefile.py`, `peutils.py` and pytest's `py.py` live
   directly in `site-packages`, not in a package directory; the removal and the leak guard only
   checked the bare module name. Both now check `name` and `name.py`.

After the fixes the only unrecorded loose file is `Lib/site-packages/README.txt` (121 bytes).
`creatoros/` (161 files) and eight `sqlalchemy/` files are unrecorded because they are copied in
after the wheel was installed; that is expected and reported, not hidden.

## RECORD integrity and declared dependencies (clean bundle)

- Declared size mismatches: **0**. SHA-256 mismatches: **0** (hashes verified).
- Missing declared `.pyc` files: **1,005**, missing console scripts: **9**, other missing files:
  **9**. The build strips bytecode and console scripts, so these RECORD entries describe files a
  source-only payload intentionally omits. The inventory reports them instead of treating them as
  corruption.
- Declared runtime requirements: 12; missing from bundle: 0; specifier mismatches: 0.
- **Open defect:** `pyproject.toml` says `creatoros-backend 0.8.0`, while the copied dist-info says
  `0.1.0`. `importlib.metadata.version('creatoros-backend')` therefore reports the wrong version in
  the packaged app, and both the inventory and the artifact SBOM flag it. The build must regenerate
  the first-party metadata at the release version (or stop copying a stale dist-info) before 1.0.0.

## Artifact SBOM vs packaged inventory

Running the inventory against `releases/0.8.0/sbom.cdx.json` and the clean bundle:

- Python components in the SBOM: **51**; matched distributions: **33**;
- in the bundle but absent/different in the SBOM: **0**;
- in the SBOM but absent from the payload: **17** — `altgraph`, `coverage`, `iniconfig`, `packaging`,
  `pefile`, `pip 26.3`, `pluggy`, `pygments`, `pyinstaller`, `pyinstaller-hooks-contrib`, `pytest`,
  `pytest-asyncio`, `pytest-cov`, `pywin32-ctypes`, `respx`, `ruff`, `setuptools`.

All 17 are development tools. They are in the SBOM because `scripts/generate_sbom.py` collects
Python components from `app/backend/.venv` (the tested environment), not from the packaged payload.
The SBOM documents that limitation honestly, but the artifact document is still supposed to describe
the bytes that ship. For 1.0.0 this must be closed: either add a payload source to the artifact
profile, or make the release gate fail when the packaged inventory is not a subset of the artifact
SBOM. The exact check today is:

```powershell
app\backend\.venv\Scripts\python.exe scripts\runtime-inventory.py `
  --payload releases\<version>\win-unpacked\resources\backend `
  --sbom releases\<version>\sbom.cdx.json `
  --json-out releases\<version>\runtime-inventory.json `
  --markdown-out releases\<version>\runtime-inventory.md
```

Exit 1 with `absent-from-payload` findings means the SBOM claims components the installer does not
ship; that is a release blocker for an artifact-bound SBOM, not a warning.

## Reproduce

```powershell
cd D:\CreatorOS
pwsh -NoProfile -File scripts\build-backend.ps1 -OutputRoot installer_builds\wave3-check
app\backend\.venv\Scripts\python.exe scripts\runtime-inventory.py --payload installer_builds\wave3-check --no-sbom
app\backend\.venv\Scripts\python.exe -m pytest app\backend\tests\test_runtime_inventory.py -q
```

The build takes roughly three minutes and runs its own migrate + health smoke test. The inventory
is deterministic: same payload, byte-identical JSON (no timestamps, no machine paths below the repo
root).

## Gate status

`scripts/release-gate.ps1` does **not** run this inventory yet. Recommended placement: the Artifact
stage, after the installer exists, with the payload from `win-unpacked`; fail closed on exit 1 and
treat exit 2 as infrastructure failure. It cannot run at Source (no payload) and must run before
Release. Until it is wired, the command above is the evidence the owner must capture for 1.0.0.

## Limits

- No Authenticode verification of the interpreter or DLLs; the build logs the signature status, the
  inventory records `not-checked`.
- RECORD lists are metadata claims, not proof of import reachability.
- Licence values are copied from package metadata and are not a legal review.
- These numbers describe an unsigned local build of 0.8.0, not a signed 1.0.0 artifact.
