# Packaged production runtime and dependency inventory (Wave 4)

Scope: validate the minimized backend runtime that the installer ships, and prove that the artifact
SBOM describes the bytes inside the packaged payload, not the builder's virtualenv. Tool:
`scripts/runtime-inventory.py` (standard library only, deterministic JSON + Markdown, exit 0 clean /
1 finding / 2 cannot inspect). Tests: `app/backend/tests/test_runtime_inventory.py`.

## Verdict

| Question | Answer |
| --- | --- |
| Does the build produce a minimized bundle? | **Yes.** 2,645 files, 62,513,538 bytes, 33 shipped distributions (32 lock distributions plus the first-party package), 0 development-only distributions, inventory exit 0. |
| Is the artifact SBOM bound to the packaged bytes? | **Yes.** `build-release.ps1` passes `--payload releases/<v>/win-unpacked/resources/backend`; the generator reads every Python component from the payload's `METADATA`/`RECORD`, not from `app/backend/.venv`. |
| Do the artifact SBOM and the independent inventory agree? | **Yes.** 33 Python components, 32 matched distributions, 0 in payload but absent from the SBOM, 0 in the SBOM but absent from the payload. The canonical tree SHA-256 agrees exactly. |
| Is the first-party version metadata correct? | **Yes.** The packaged `creatoros_backend-0.8.0.dist-info` says 0.8.0, matching `pyproject.toml`; `payload_metadata.py --check` passes against the packaged tree. |
| Does the CPython tree keep its vendor signature? | Not checked by this inventory; `build-backend.ps1` checks `python.exe`'s Authenticode status (Valid, Python Software Foundation) during the build. |

## What ships

The backend is not frozen with PyInstaller. It is a signed CPython 3.12.10 tree (`python.exe`,
`python3.dll`, `python312.dll`, `vcruntime140*.dll`, `DLLs/`, `Lib/`) plus the `creatoros` package,
canonical first-party metadata, migrations, `alembic.ini` and `PYTHON-LICENSE.txt`. No compiler and
no bootloader executable is shipped.

`scripts/build-backend.ps1` prunes the developer environment, copies only distributions allowed by
`app/backend/requirements-runtime.lock`, removes every unowned `site-packages` file, rewrites the
first-party dist-info from the actual shipped files, and runs a timeout-guarded migrate +
`/health/live` smoke test before declaring success.

## Final payload evidence (0.8.0 build, 2026-11-26)

| Payload | Files | Bytes | Distributions | Development-only | Inventory exit |
| --- | ---: | ---: | ---: | ---: | ---: |
| `releases/0.8.0/win-unpacked/resources/backend` | 2,645 | 62,513,538 | 33 | 0 | 0 |

- Canonical tree SHA-256: `49d3fb3126210bce11baeb46d069621e69f5441e6d3c83ea41e17f37397f225a`
- Installer: `CreatorOS-Setup-0.8.0.exe`, 139,888,520 bytes,
  SHA-256 `d3a73f608d903e72de35a5f2def18d31ed367dccbc4fe922ac7f739580e9cc07`
- RECORD: 0 size mismatches, 0 SHA-256 mismatches (all hashes verified)
- Missing declared `.pyc` files: 992; missing console scripts: 9; missing other files: 0
- Optional compiled extensions absent with a pure-Python fallback present: 8 (all SQLAlchemy
  accelerators quarantined by Smart App Control on the build machine). The artifact SBOM records
  this explicitly on the `creatoros-backend` component; it is a recorded limitation, not a silent
  omission.
- Console-script RECORD rows outside the payload (`../../Scripts/*.exe`): 9, explicitly counted as
  not shipped.

## Artifact SBOM vs packaged inventory

```powershell
app\backend\.venv\Scripts\python.exe scripts\runtime-inventory.py `
  --payload releases\0.8.0\win-unpacked\resources\backend `
  --sbom releases\0.8.0\sbom.cdx.json `
  --json-out releases\0.8.0\runtime-inventory.json `
  --markdown-out releases\0.8.0\runtime-inventory.md
```

- Python components in the artifact SBOM: **33**; matched distributions: **32**
- In the payload but absent/different in the SBOM: **0**
- In the SBOM but absent from the payload: **0**
- First-party `creatoros-backend`: payload 0.8.0, SBOM 0.8.0

`build-release.ps1` fails the build unless all of the following hold: the inventory exits 0; the
artifact SBOM tree hash and file count equal the inventory's; the source and artifact `pkg:pypi`
sets agree; the first-party versions agree; and `payload_metadata.py --check` passes on the packaged
tree. The inventory itself now treats missing or wrong-version declared dependencies and any
payload/SBOM disagreement as findings (exit 1), so it cannot report "clean" for a payload that lacks
its declared runtime. `release-gate.ps1 -Stage Artifact` repeats the inventory against the packaged
payload and compares its tree hash and file count with the artifact SBOM, so the gate opens the
payload rather than trusting the document alone.

## Why the Wave 3 defect is closed

Wave 3 generated the artifact SBOM from `app/backend/.venv`, so it claimed 17 development
distributions the installer did not ship. The artifact profile now requires `--payload` and derives
every Python component from the payload. A distribution in the payload that is not in the runtime
lock, a lock distribution missing from the payload, an unowned site-packages file, a stale
first-party dist-info, or a RECORD hash/size mismatch is a hard failure. The independent inventory
repeats the comparison without importing the generator, so one bug cannot make both agree.

## Limits

- No Authenticode verification of the interpreter or DLLs; the build logs the signature status, the
  inventory records `not-checked`.
- RECORD lists are metadata claims, not proof of import reachability.
- Licence values are copied from package metadata and are not a legal review.
- These numbers describe an unsigned local build of 0.8.0, not a signed 1.0.0 artifact.