# v1.0.0 software bill of materials

`scripts/generate_sbom.py` writes a CycloneDX 1.5 JSON document from artifacts that exist in this
checkout. It is a machine-readable inventory of what was inspected, not a legal opinion. The release
gate's SBOM check and this document are described in the sections below.

## What the document covers

| Component(s) | Source read by the generator |
| --- | --- |
| `CreatorOS` product component | `--version`, or `app/desktop/package.json` when omitted |
| `creatoros-desktop` (application) | `app/desktop/package.json` version, Apache-2.0 detected from `LICENSE` |
| `creatoros-backend` (application) | `app/backend/pyproject.toml` `[project] version`, Apache-2.0 from `LICENSE` |
| Node direct dependencies | `app/desktop/pnpm-lock.yaml` `importers['.']`: `dependencies` are `required`, `devDependencies` are `excluded` with a `creatoros:scope-reason` saying they are build-time only. Exact versions come from the lockfile; a peer suffix is preserved in `creatoros:lockfile-resolution` and the pnpm SRI integrity string in `creatoros:pnpm-integrity` |
| Python distributions | `app/backend/.venv` installed metadata (`importlib.metadata`) because that is the tested environment. Versions are the installed versions and each component records `creatoros:version-source=venv` |
| Python without a venv | `[project].dependencies` specifiers recorded verbatim as the version, with `creatoros:version-source=pyspecifier`. A specifier is not a resolution and is never presented as one |
| `ffmpeg` and `ffprobe` | `tools/ffmpeg/bin/*.exe`: SHA-256 of the real file and the version string from the real `-version` banner. The licence and redistribution status are copied from `docs/release/DEPENDENCY_MANIFEST.md`, including `Blocked for bundling.` |
| `ollama` | Only when `--prerequisites` names a manifest that declares it (version, byte size, SHA-256, expected signer subject) |

Every component has a `bom-ref` and, where a package identity exists, a purl (`pkg:npm/...` or
`pkg:pypi/...`). Hashes in `hashes[]` are SHA-256, lowercase hex. Node packages are not hashed: the
lockfile records a sha512 SRI of the registry tarball, which is preserved as a property rather than
converted into a hash of a file this repository does not have.

Components are sorted by `(type, name, version)` and JSON is written with a fixed indent and LF
newlines, so the same inputs produce the same bytes apart from `metadata.timestamp`. Without
`--serial`, the serial number is a UUIDv5 over the sorted component list; it changes exactly when a
component changes and not when the clock changes. `--timestamp` and `--serial` make a run fully
reproducible.

## Generate

From the repository root, using the backend virtual environment:

```powershell
cd D:\CreatorOS
app\backend\.venv\Scripts\python.exe scripts\generate_sbom.py `
  --output releases\1.0.0\sbom.cdx.json `
  --version 1.0.0 `
  --prerequisites app\backend\src\creatoros\resources\prerequisites.v1.json `
  --check
```

Naming a missing manifest is a required-input error (exit 2). The generator never downloads anything:
it only reads local files and runs the FFmpeg binaries with `-version`.

`--check` re-reads the file just written and exits 1 with a list of structural problems: missing
top-level fields, duplicate `bom-ref`, a component without a name or type, a malformed hash, an
application component without a version, or no components. Exit code 2 means a required input was
missing or unreadable (desktop `package.json`, backend `pyproject.toml`, `pnpm-lock.yaml`, an
explicitly passed `--prerequisites` file, or an invalid `--timestamp`/`--serial`). A missing FFmpeg binary, an unreadable FFmpeg banner, an
absent `.venv`, or incomplete Ollama metadata do not fail the run; each is recorded in
`creatoros:collection-error` properties so the gap is visible in the document itself.

### Prerequisites manifest format

The generator reads the same pinned document the first-run wizard verifies against:
`app/backend/src/creatoros/resources/prerequisites.v1.json` (schema `creatoros.prerequisites.v1`).
From its `components` list it takes the entry whose `id` is `ollama` and records
`version`, `acquisition.size_bytes`, `acquisition.sha256` (lowercased) and
`signature.expected_subject`.

```json
{
  "schema": "creatoros.prerequisites.v1",
  "components": [
    {
      "id": "ollama",
      "version": "0.40.18",
      "acquisition": {
        "size_bytes": 1953587080,
        "sha256": "ad2bb47aa969b21cf8dd81b2d9f9a6d5030270ed242ef55087118c37c510acc2"
      },
      "signature": { "expected_subject": "CN=Ollama Inc., O=Ollama Inc., L=Toronto, S=Ontario, C=CA" },
      "license": { "name": "MIT for Ollama" }
    }
  ]
}
```

A missing `version` or SHA-256 is a hard error; a missing size or signer subject becomes a
`creatoros:collection-error`. `license.spdx` is used when present and `license.name` otherwise; a
non-SPDX name such as `MIT for Ollama` is not converted into a licence identifier, so the component
records `creatoros:license-status=unknown`. The older flat `{"ollama": {...}}` shape is still accepted,
and a document with any other `schema` value is refused rather than silently ignored. This file is an
input, not a grant of redistribution rights. A version or SHA-256 that disagrees with the repository's
`DEPENDENCY_MANIFEST.md` is recorded as a collection error.

## How the release gate consumes it

`scripts/release-gate.ps1` looks for, in order:

1. `releases\<version>\sbom.cdx.json`
2. `releases\<version>\sbom.spdx.json`
3. `docs\release\sbom-<version>.cdx.json`

It takes the first existing file and validates it before passing `release.sbom`:

- `bomFormat` is `CycloneDX` and `specVersion` is `1.4`, `1.5` or `1.6`;
- `metadata.component.version` equals the gate's `-ReleaseVersion` (an SBOM for another version
  cannot satisfy this release);
- there is at least one component, and every component has a non-empty `name`, `version` and `type`;
- no `bom-ref` appears twice.

A parseable document that fails any of those closes the gate. The gate still does not verify hashes,
licences or provenance; those are the generator's and the reviewer's job. `--check` remains part of
the release procedure: run it, keep its exit code 0, and attach the same file the gate will read. Do
not hand-edit the document to satisfy the gate.

Two further gates in the same stage validate the prerequisites manifest itself:
`release.prerequisites` checks schema, ids, https-only downloads, positive sizes, 64-hex digests,
boolean signature/redistribution flags and consent, and `release.prerequisites-hashes` requires every
pinned digest to appear in `DEPENDENCY_MANIFEST.md`.

## Regenerate after any payload change

Regenerate and re-run `--check` before every tagged build, and again if any of these change:

- `app/desktop/package.json` or `app/desktop/pnpm-lock.yaml` (including a `pnpm install`);
- `app/backend/pyproject.toml`, or the installed set in `app/backend/.venv` (`pip install`, `pip
  uninstall`, an interpreter change);
- `LICENSE`;
- any file under `tools/ffmpeg/bin` (a different FFmpeg build must produce a different hash and
  banner version);
- the prerequisites manifest used for Ollama.

A serial number that changes between two runs means the component list changed; review the diff
before publishing. A serial number that stays the same while files were rebuilt means the recorded components,
versions, hashes and properties are unchanged, which is itself worth confirming.

## Current repository facts

- The source version is 0.8.0 (`app/desktop/package.json` and `app/backend/pyproject.toml`). Passing
  `--version 1.0.0` labels the product component; it does not change the two application component
  versions, which stay tied to their real source files until those are bumped. The
  generator records `creatoros:desktop-package-version` on the product component when the requested
  release version and the desktop version differ.
- FFmpeg is GPLv3 and the dependency manifest marks it **Blocked for bundling**. The SBOM repeats
  that status and does not upgrade it.
- Ollama's installer is signed and hash-verified but **not approved for v1.0.0 bundling yet**; it is
  included only when a prerequisites manifest declares it, as an excluded component.
- The backend virtual environment contains packaging and test tooling (for example `pyinstaller`,
  `pytest`, `pip`). Components not reachable from declared runtime or dev dependencies are marked
  `excluded` with a scope reason instead of being presented as shipped runtime dependencies.
- If the editable install metadata for `creatoros-backend` is stale (it currently reports 0.1.0 while
  `pyproject.toml` declares 0.8.0), the generator still uses the pyproject version and does not emit a
  second, conflicting backend component.

## What the document does NOT prove

- It is **not a legal review**. It records what the repository says about licences; it does not
  determine obligations, compatibility, or whether a licence claim is correct.
- It is **not provenance** for unverified binaries. A SHA-256 proves which bytes were on this machine
  when the file was read; it does not prove who built the binary, that it was signed, or that it is
  safe. FFmpeg is unsigned and Ollama's signer is only recorded, not verified here.
- It contains **no model weights**, user media, databases, credentials, or files under `models/`,
  `data/` and `logs/`.
- It does **not guarantee licence compatibility** between bundled components or with the product's
  Apache-2.0 licence.
- It does **not** list transitive Node dependencies. Only `importers['.']` direct dependencies are
  recorded; the lockfile remains the authority for the full tree.
- It does **not** hash installed Python distributions, the CPython interpreter payload, Electron,
  Node, or any file that is not explicitly listed above.
- It does **not** prove that the tested `.venv` or the recorded FFmpeg build is what an installer
  ships. The installer payload must be audited separately.

## Maintenance

The generator is standard-library-only Python 3.12. Its tests call the script by path and assert
against the real repository:

```powershell
cd D:\CreatorOS\app\backend
.\.venv\Scripts\python.exe -m pytest tests/test_release_sbom.py -q
.\.venv\Scripts\python.exe -m ruff check ..\..\scripts\generate_sbom.py tests\test_release_sbom.py
```

Only `app/backend/pyproject.toml` carries a `[tool.ruff]` section (`line-length = 110`). `scripts/`
is outside that configuration root, so ruff applies its default/global settings there; keep the
generator within 110 columns anyway.
