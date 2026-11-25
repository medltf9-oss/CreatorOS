# Security policy

## Supported versions

Security fixes are provided for the latest release line, currently **0.8.x**.
Older builds may be asked to upgrade before a report is investigated.

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.**

Use GitHub's private vulnerability reporting for this repository:

1. Open the **Security** tab.
2. Choose **Report a vulnerability**.
3. Include the affected version, Windows version, reproduction steps, impact,
   and any relevant logs with secrets and personal data removed.

If private reporting is unavailable, open a minimal issue stating that you need
a private security contact. Do not include exploit details, credentials, account
identifiers or personal data in that issue.

There is no bug bounty and no guaranteed response time. Maintainers will
acknowledge a complete report when they can, investigate, and coordinate a fix
and disclosure. Please give reasonable time for a fix before publishing details.

## Scope and security model

CreatorOS is a local desktop application. The following are in scope:

- The Electron main process, preload bridge and renderer isolation.
- The loopback FastAPI service, its per-launch session token and IPC authentication.
- Path traversal, signed media URLs, upload handling and archive extraction.
- Credential vault encryption, key handling, OAuth state/PKCE and token storage.
- Publishing approval bypass, payload tampering, or an action that publishes
  without an explicit human approval.
- Secrets or private data written outside the configured data root, or included
  in logs, crash output or the repository.
- Dependency vulnerabilities with a realistic exploit path.

Out of scope:

- Attacks that require an already-compromised administrator account or physical
  access to an unlocked machine.
- Provider-side vulnerabilities. Report those to the provider.
- Issues caused by disabling Windows security controls, running untrusted
  Electron flags, or exposing the loopback API through a proxy.
- Reports containing real credentials, private media or personal data.

## Safe research

Test only against your own installation and accounts. Do not access other
people's data, perform denial-of-service testing, or publish live content as part
of research. Stop and report if you encounter real user data.

## Dependency and release integrity

Release artifacts are built from tagged source and published with SHA-256
checksums. The 0.8.0 installer is unsigned; verify the checksum before running
it and see `docs/CODE_SIGNING.md` for the current signing position.
