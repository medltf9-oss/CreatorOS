# Owner action: code-signing certificate for v1.0.0

**Status:** BLOCKED — owner-only purchase and identity validation
**Prepared:** 2026-11-25
**Applies to:** the Windows x64 installer and every CreatorOS-owned executable in it
**Detail:** `docs/release/CERTIFICATE_PROCUREMENT.md`
**Proof command:** `powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check`

This is the one action that cannot be delegated, automated or simulated. Everything else in the
release is either done or waiting on this certificate (and on the clean-machine and licensing
decisions tracked in `V1_RELEASE_BLOCKERS.md`).

## 1. What the release needs

A publicly trusted Authenticode code-signing certificate that:

- chains to a CA root in the Microsoft Trusted Root Program;
- carries the Code Signing EKU;
- is issued to the real publisher after the CA validates their government identity;
- keeps its private key in CA-compliant hardware (token, HSM) or a CA-operated signing service;
- is valid for no more than **460 days** for certificates issued on or after 2026-03-01
  (CA/Browser Forum Code Signing Baseline Requirements 3.11.0, §6.3.2).

A self-signed certificate, a locally trusted root, a test certificate or a certificate in someone
else's name does not pass. The agent will not create one and call the result signed.

## 2. Decision 1 — the public publisher name (owner)

An individual-validation certificate displays the verified person in Windows Explorer, in the UAC
prompt and in `signtool` output. Before ordering, confirm:

| Question | Owner answer |
| --- | --- |
| Exact legal name as printed on the government ID | _to be written by the owner_ |
| Order of given/family name the CA should use | _to be confirmed at checkout_ |
| Is the publisher the individual, or a registered legal entity? | _to be decided_ |
| Public country/region that may appear | Algeria (`DZ`), if the CA supports it |

Do not use a pseudonym, "CreatorOS" alone, or a company that does not legally exist. The name on
the certificate is the name users will see when they trust the installer.

## 3. Decision 2 — route and budget (owner)

Prices verified against the vendors’ official pages on **2026-11-25**. Confirm at checkout; shipping,
tax and customs are quoted there.

| Route | Published price | Publisher shown | Fit for this release |
| --- | --- | --- | --- |
| **SSL.com IV + YubiKey FIPS** (recommended) | **US$129.00** certificate + **US$379.00** token, plus shipping | The individual’s verified name | Yes. Algeria is an accepted country and is not restricted; no company needed |
| SSL.com IV + eSigner cloud signing | Certificate price + eSigner subscription (tiered; confirm current tier) | Individual’s verified name | Yes, but needs CodeSignTool/CSC integration instead of the current token path |
| **Certum Open Source** set / cloud / activation code | **€69.00 / €49.00 / €25.00** gross + **€38.00** shipping to Algeria | “Open Source Developer” + holder’s name | Only if CreatorOS stays strictly non-commercial open source; Certum decides eligibility; subject is not the bare legal name |
| Sectigo individual via reseller | About US$219.99 + about US$130 international token shipping (reseller quote; verify) | Individual | Possible, but adds a reseller and a new token each year |
| DigiCert OV | Organisation-validated, materially more expensive | Registered organisation only | Only if the owner forms a legal entity |
| Microsoft Artifact Signing (Public Trust) | — | — | **Not available.** Individual developers must be in the US or Canada; organisations are limited to a country list that does not include Algeria |

**Budget guidance:** roughly **US$520–$600 delivered** for SSL.com IV + token (plus local customs),
or **€107–€121** for the Certum open-source set. A token is a one-time purchase that may be reused at
renewal; the certificate is annual.

Why SSL.com IV is the recommendation: the certificate is issued to the person who actually
publishes, it needs no company formation, Algeria is explicitly on the accepted list, the private
key stays on the FIPS token, and the existing `scripts\sign-release.ps1` already signs by thumbprint
and timestamps with RFC 3161.

## 4. Eligibility answers for an Algerian individual

| # | Requirement question | Status (2026-11-25) |
| --- | --- | --- |
| 1 | Algerian individual accepted? | SSL.com: yes (DZ on accepted list, not restricted). Certum: ships to Algeria; individual open-source eligibility is decided by Certum |
| 2 | Identity/address evidence | Government photo ID + proof of address; CA may require a liveness check or callback |
| 3 | Is the individual’s name the Windows publisher? | SSL.com IV: yes. Certum: “Open Source Developer” plus the holder’s name |
| 4 | CA in the Microsoft Trusted Root Program? | Yes for SSL.com and Certum code-signing chains; verify the issued chain in `certutil -verify` before release |
| 5 | Individual validation without a company? | SSL.com IV: yes. Certum Open Source: yes for qualifying maintainers |
| 6 | Hardware or HSM key protection? | SSL.com: YubiKey FIPS or cloud HSM/eSigner. Certum: card + reader, or SimplySign cloud |
| 7 | Can the key be exported? | No, by policy. Default and requirement: non-exportable |
| 8 | Token delivery to Algeria | SSL.com: international shipping quoted at checkout. Certum: €38.00 net to Algeria; customs/duty extra |
| 9 | RFC 3161 timestamp support | Yes; the CA’s documentation recommends a TSA URL (default: `http://timestamp.digicert.com`) |
| 10 | Total cost and renewal | See the table above; renewal is the certificate price, a new token is not normally required |
| 11 | Validity | Maximum 460 days for certificates issued on or after 2026-03-01 |
| 12 | Open-source/commercial use | SSL.com IV permits signing the project’s binaries. Certum Open Source restricts use to non-commercial open source |
| 13 | SignTool/NSIS compatibility | Standard Authenticode; the current pipeline signs `CreatorOS.exe` and the NSIS installer |
| 14 | Chain and instructions | The CA provides the chain and token middleware; the release keeps the public `.cer` only |
| 15 | Revocation/renewal | Via the CA portal; revocation of the certificate invalidates every signature made with it |
| 16 | Lost or damaged token | Replacement is a CA process and usually a new order; keep the token backed up only as the CA permits (never as an exported PFX) |

## 5. Exact owner steps

1. Write down the exact legal name and confirm it is acceptable as a public publisher string.
2. Open the chosen CA’s official product page (links below), select the individual/IV product, the
   token or cloud option, and the shipping destination. Record the total before paying.
3. Pay with a method that produces a receipt. Keep the order number.
4. Complete the CA’s identity verification in its own portal: government ID, proof of address and any
   callback or liveness step. Never send those documents to the agent or into the repository.
5. Receive the token (or activate the cloud signing credential). Install the vendor middleware, set a
   PIN, and confirm the certificate appears with a private key and the Code Signing EKU.
6. On the build machine, run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check
   ```

   It must report a CA-issued certificate with a private key. Self-signed or missing private key is a
   failure, not a warning.
7. Send the release owner (not the agent) the certificate thumbprint, subject, issuer, serial,
   validity dates, the public certificate file and the approved publisher string. **Never** the PIN,
   PFX, token password or private key.
8. Keep the token physically protected and limit who can sign. Signing requires the token present and
   the PIN entered locally.

## 6. What the agent already did in parallel

- `scripts\sign-release.ps1`: thumbprint signing, `/tr` + `/td SHA256` RFC 3161 timestamping,
  `signtool verify /pa /all`, and a fail-closed `-Check` path.
- `scripts\build-release.ps1 -RequireSignature`: refuses to produce a release installer without a
  verified signature and timestamp.
- `scripts\release-gate.ps1`: certificate, Authenticode, timestamp, publisher and clean-machine gates
  that fail closed when evidence is missing.
- `docs/release/DEPENDENCY_MANIFEST.md` and the prerequisites manifest: every third-party byte is
  pinned by hash so the signed payload can be re-verified.
- Evidence tooling: `scripts\capture-security-state.ps1` and `scripts\build-sac-evidence.ps1`, which
  currently refuse on this machine because Smart App Control is off and the installer is unsigned.

## 7. What closes this gate

- The certificate is present in the Windows certificate store with its private key and Code Signing
  EKU; `sign-release.ps1 -Check` passes on the exact release commit.
- The installer and every CreatorOS-owned PE file in it have a valid Authenticode signature
  (`signtool verify /pa /all`), a verified RFC 3161 timestamp, and a subject matching the approved
  publisher.
- `docs/evidence/install-results.json` records that verification, with the artifact SHA-256, on the
  final signed build.
- A clean Windows 11 machine with Smart App Control **enforced** installs that exact signed artifact
  and records the result (separate owner action: `V1_RELEASE_BLOCKERS.md` §1).

## 8. Official sources (checked 2026-11-25)

- CA/Browser Forum code-signing requirements: <https://cabforum.org/working-groups/code-signing/requirements/>
- Microsoft Trusted Root Program requirements: <https://learn.microsoft.com/en-us/security/trusted-root/program-requirements>
- Microsoft code-signing options: <https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options>
- Microsoft Artifact Signing eligibility: <https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart>
- SSL.com IV Code Signing: <https://www.ssl.com/products/software-integrity/code-signing/iv/>
- SSL.com accepted countries: <https://secure.ssl.com/csrs/country_codes>
- Certum Code Signing shop: <https://shop.certum.eu/code-signing.html>
- Certum required documents: <https://support.certum.eu/en/code-signing-required-documents/>
- Certum shipping rates: <https://shop.certum.eu/payment-shipping>
