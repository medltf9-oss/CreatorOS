# Clean-clone Source gate

This is the reproducibility proof for the Source stage: a fresh clone with no build output, no
virtual environment and no SBOM must reach the same gate result as the development checkout.

## Procedure

```powershell
git clone --branch release/v1.0.0-readiness --single-branch -c core.autocrlf=false `
  https://github.com/medltf9-oss/CreatorOS.git D:\P\creatoros-clean
cd D:\P\creatoros-clean
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Root (Get-Location) -Apply
powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Source -ReleaseVersion 0.8.0
```

Bootstrap creates the backend `.venv`, installs the runtime/test dependencies, copies the pinned
FFmpeg pair into `<root>\tools\ffmpeg\bin` and leaves the working tree clean. The gate then
generates the source and prerequisite SBOMs from the checkout. No installer and no artifact SBOM
exist in a clean clone, so the artifact checks are reported as not required at Source instead of
being faked.

## Why the clone flags are not optional yet

`main` stores CRLF blobs for four `.ps1` files while `.gitattributes` declares `*.ps1 text eol=crlf`.
A default clone of `main` followed by `git checkout release/v1.0.0-readiness` therefore aborts with
`error: Your local changes to the following files would be overwritten by checkout` for
`scripts/bootstrap.ps1`, `scripts/diagnose.ps1`, `scripts/install-unpacked.ps1` and
`scripts/verify-install.ps1`, even though nothing was edited. The Wave 3 tree
normalizes those blobs to LF, so cloning the release branch directly with `core.autocrlf=false`
produces a clean tree (`git status --porcelain` empty, `git ls-files --eol` reports `i/lf`). Once
this branch is merged, a default clone is clean again; the flags can then be dropped.

## Gate working-directory fix

The first run from outside the clone failed both SBOM checks because the gate invoked
`scripts/generate_sbom.py` relative to the caller's current directory. The gate now resolves that
path from its own repository root, so `-File <clone>\scripts\release-gate.ps1` works from any
directory. The documented sequence still `cd`s into the clone because that is what a user does.

## Result

Clone created and bootstrapped at `1334fe9`: clone exit 0, `git status --porcelain` empty before
and after bootstrap, Source gate **13 passed, 0 failed** (`RELEASE GATE: PASSED`), with the
artifact SBOM checks reported as not required.

The same clone was then fast-forwarded to `5812915` (the CI-safe skip for this suite, no runtime
change), reset clean, and the gate re-run from the clone root: **13 passed, 0 failed** again.

| Evidence | Value |
| --- | --- |
| Clone HEAD | `5812915bdbba408d293f3ac58d54129e03576074` |
| Bootstrap | exit 0, working tree clean after apply |
| Source gate | 13 passed, 0 failed, PASSED |
| Source SBOM in clean clone | 49 components (the development checkout reports 66; both pass the same structural checks) |
| Artifact SBOM | absent by design at Source; build-release.ps1 generates it after the installer |

## What this does not prove

- No installer, signature, timestamp or artifact SBOM is produced by the Source stage.
- It does not run the backend or desktop test suites; those are separate commands on the same
  commit.
- It does not prove that Windows Smart App Control accepts the installer; that needs the clean
  SAC-on machine and is owner-only.
