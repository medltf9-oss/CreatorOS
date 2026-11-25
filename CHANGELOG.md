# Changelog

All notable changes to CreatorOS are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Release-readiness work toward `1.0.0`. This is not a `1.0.0` release, and the source version is
unchanged; see `docs/release/V1_RELEASE_BLOCKERS.md` for what still blocks publication.

### Added

- A first-run setup page and `/setup` API: a pinned `creatoros.prerequisites.v1` manifest, bounded
  presence detection for Ollama and FFmpeg, on-request SHA-256, size, Authenticode, publisher and
  timestamp verification of a downloaded prerequisite, and a recorded finish/skip decision. The
  application never downloads or installs a prerequisite itself.
- The setup truth model: every verification attempt that finishes is persisted with its SHA-256,
  size, modified time, path, signer and time, and `GET /setup/status` reports `verified` only when
  that record passed and the file's size and modified time still match. A changed file downgrades to
  `unverified` with the reason. The desktop shows *verified (recorded)*, *present, not verified*,
  *unverified — file changed* and *verification failed* as different states.
- `scripts/generate_sbom.py`: deterministic CycloneDX 1.5 SBOM generation from real repository
  inputs (desktop and backend versions, Node integrity digests, Python environment, FFmpeg hashes and
  versions), with a `--check` validation mode.
- ADR-37 and an updated `docs/INSTALLATION.md`, covering the setup flow, prerequisite verification,
  the FFmpeg layout, the data root and uninstall behaviour.
- Release-governance records: `docs/release/OWNER_CERTIFICATE_ACTION.md` (the owner-only certificate
  checklist, current Algeria-eligible options and prices), `docs/release/SIGNATURE_INVENTORY.md` (the
  measured 64-file PE inventory and signing policy) and `docs/release/V1_RELEASE_GATE_MATRIX.md` (the
  authoritative pass/blocked/not-run matrix).

### Changed

- `scripts/release-gate.ps1` validates the SBOM's format, spec version, component fields, duplicate
  references, release version, serial-number shape, timestamp freshness (30 days) and the presence of
  at least one SHA-256 component hash instead of accepting any parseable JSON, and adds
  `release.prerequisites` / `release.prerequisites-hashes` gates that validate the pinned manifest
  and cross-check every digest against the dependency audit record.
- `scripts/generate_sbom.py --artifact` binds the installer's SHA-256 and size into
  `metadata.component`, and `scripts/build-release.ps1` generates that document after the checksums,
  so the SBOM, `SHA256SUMS.txt` and the installer describe the same bytes. New gate rows
  `artifact.sbom` (the SBOM must record the installer on disk) and `artifact.manifest-scope` (every
  file named in the checksum manifest must exist and match) close the reuse of stale evidence.
- Setup completion is transactional about its claims: `POST /setup/complete` returns 409
  `REQUIRED_CHECKS_FAILING` when a required check fails and requires an explicit
  `override_required_checks` decision, which is recorded in state and audit metadata. Completion is
  idempotent (the first `completed_at` and `completed_version` survive), notes are kept unless
  replaced, and an upgrade reports `version_changed` without reopening the wizard.
- Verification is bounded: symlinks, reparse points and non-regular files are rejected, files over
  4 GiB are refused, runs are serialised and a 120 s deadline returns 504 without writing a record.
  FFmpeg presence states that a size match is not integrity; only `/verify` reads the bytes.
- `docs/release/V1_ACCEPTANCE_MATRIX.md` records the twelve clean-machine scenarios and the evidence
  each requires. It is explicitly **NOT RUN**: no signed release candidate exists.
- A publisher subject is now compared relative-distinguished-name by relative-distinguished-name,
  so `CN=Ollama Inc. Support` no longer satisfies a pinned `CN=Ollama Inc.` (previously substring
  containment). Quoted commas are parsed correctly.
- `scripts/production-health.ps1` no longer contains a literal backspace byte in the repository
  virtualenv path, which made every post-uninstall check abort at assignment time.
- The shipped backend is no longer the developer virtualenv. `scripts/build-backend.ps1` prunes
  pytest, coverage, PyInstaller (including its four unsigned bootloader executables), pygments,
  packaging, ruff and the rest of the development toolchain, removes the setuptools `.pth` shim and
  the `__pycache__` written during verification, and fails if a pruned module reappears. The measured
  bundle fell from 98,640,439 bytes to 64,623,559 bytes; it still migrates and serves `/health/live`.

### Fixed

- The Smart App Control state recorded for the audit machine is corrected to off
  (`VerifiedAndReputablePolicyState = 0`) in ADR-34, with a note that its install evidence was not
  collected under SAC enforcement.

## [0.8.0] — 2026-11-24

The first public, open-source release.

### Added

- A complete desktop visual system: graphite-blue workspace, deep-blue
  navigation rail, mint/rose/lavender accents, one light feature panel, and a
  purpose-built light theme. Both themes share one token set.
- Collapsible 252/72 px navigation rail with icons, tooltips, keyboard access and
  persisted state.
- A product dashboard that reports real library, publishing, social, AI and
  storage state, with labelled empty and unavailable states.
- A separate AI infrastructure page for runtime health, model governance,
  capabilities and execution history.
- Apache-2.0 licensing, NOTICE, third-party notices, contribution, security and
  code-of-conduct documents, issue and pull-request templates, and CI.
- A documented design system in `docs/DESIGN_SYSTEM.md`.

### Changed

- The renderer palette is defined once and inherited by all 49 routes.
- Cards, buttons, fields, badges, stats and empty states now share one visual
  language and one radius/shadow scale.
- The interface uses Lucide outline icons instead of text-only navigation.
- Public documentation now describes source builds, privacy, limitations and the
  unsigned distribution honestly.
- Package metadata declares Apache-2.0 and the neutral project attribution.

### Security

- The credential vault, per-launch IPC token, loopback-only API, signed media
  URLs, human approvals and data-root path policy are unchanged.
- Secret scanning and history review were run before publication; no
  credentials, private keys, databases, media or model weights are tracked.

### Known limitations

- Windows x64 only.
- The installer is not code-signed. SmartScreen warns and Smart App Control may
  block it.
- Live provider publishing requires your own developer apps, account approvals
  and connected accounts; it is not preconfigured.
- FFmpeg (including GPL-enabled builds) and Ollama are installed separately and
  keep their own licences.
- Release validation was performed on one Windows machine; it is not a
  certification across all hardware and Windows configurations.
