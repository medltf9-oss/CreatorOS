# Clean-clone Source gate

This is the reproducibility proof for the Source stage: a fresh clone with no build output and no
virtual environment must reach the same gate result and the same source SBOM as the development
checkout. The artifact stage cannot run here because no installer exists.

## Procedure

```powershell
git clone --branch release/v1.0.0-readiness --single-branch -c core.autocrlf=false `
  https://github.com/medltf9-oss/CreatorOS.git D:\P\wave4\clean-clone
cd D:\P\wave4\clean-clone\app\backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip -q
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.lock -q
.\.venv\Scripts\python.exe -m pip install "packaging>=24" -q
.\.venv\Scripts\python.exe -m pip install --no-deps -e . -q
cd ..\..
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Source -ReleaseVersion 0.8.0
```

The backend steps are the same commands `scripts/bootstrap.ps1` runs for a data-root install. They
are repeated here instead of running bootstrap's full `-Apply`, because that mode also creates the
data folder tree and installs desktop dependencies inside the clone; both would make `git.clean`
false and turn a cleanliness proof into a self-fulfilling dirty run.

`packaging` is a release-tooling dependency of `scripts/backend_lock.py`, not a runtime dependency.
It is installed explicitly and is not in `requirements-runtime.lock` or the shipped payload.

## Why the clone flags are not optional yet

`main` stores CRLF blobs for four `.ps1` files while `.gitattributes` declares `*.ps1 text eol=crlf`.
A default clone of `main` followed by `git checkout release/v1.0.0-readiness` therefore aborts with
`error: Your local changes to the following files would be overwritten by checkout` for
`scripts/bootstrap.ps1`, `scripts/diagnose.ps1`, `scripts/install-unpacked.ps1` and
`scripts/verify-install.ps1`, even though nothing was edited. The release tree normalizes those
blobs to LF, so cloning the release branch directly with `core.autocrlf=false` produces a clean
tree (`git status --porcelain` empty). Once this branch is merged, a default clone is clean again.

## Result

Clone and backend environment created from commit `b10fe009dab65d7c092a8ab95079a68347564115`
(2026-11-26T13:19:43+01:00), then fast-forwarded from the earlier `d80aedd` run. `git status
--porcelain` was empty; no repository file was modified by the verification.

| Evidence | Value |
| --- | --- |
| Clone HEAD | `b10fe009dab65d7c092a8ab95079a68347564115` |
| Runtime lock in the fresh environment | `backend_lock.py --check` passes, 32 distributions |
| Source gate | **13 passed, 0 failed**, `RELEASE GATE: PASSED` |
| Source SBOM | 48 components, serial `urn:uuid:55491264-5c86-556a-b907-4b689f343e40` |
| Source SBOM SHA-256 | `38BCB7B8DBAA360C58800AB575632E2ED9EF00A2ED2A1514F733A877CD44D8DD` |
| Same commit in the development checkout | byte-identical SHA-256 (`38BCB7B8…`) |
| Artifact SBOM | absent by design at Source; `build-release.ps1` generates it after the installer |

The source SBOM timestamp is the commit time supplied by the gate, so the digest is a property of
the commit rather than of the clone's wall clock. The earlier hashes `7E61D01D…` (tree at
`b381b24`) and `923355F5…` (`d80aedd`) are correct for their own commits and are not expected to
reproduce at `b10fe00`. The `releases/` directory was removed before the final gate run so the gate
generated the document fresh at `b10fe00`; the retained file is that 38BC document, not the earlier
clone's `923355F5…` copy.

## What this does not prove

- No installer, signature, timestamp or artifact SBOM is produced by the Source stage.
- It does not run the backend or desktop test suites; those are separate commands on the same
  commit.
- It does not prove that Windows Smart App Control accepts the installer; that needs the clean
  SAC-on machine and is owner-only.