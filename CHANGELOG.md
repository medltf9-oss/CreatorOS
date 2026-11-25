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
- `scripts/generate_sbom.py`: deterministic CycloneDX 1.5 SBOM generation from real repository
  inputs (desktop and backend versions, Node integrity digests, Python environment, FFmpeg hashes and
  versions), with a `--check` validation mode.
- ADR-37 and an updated `docs/INSTALLATION.md`, covering the setup flow, prerequisite verification,
  the FFmpeg layout, the data root and uninstall behaviour.

### Changed

- `scripts/release-gate.ps1` validates the SBOM's format, spec version, component fields, duplicate
  references and release version instead of accepting any parseable JSON, and adds
  `release.prerequisites` / `release.prerequisites-hashes` gates that validate the pinned manifest
  and cross-check every digest against the dependency audit record.

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
