# Changelog

All notable changes to CreatorOS are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
