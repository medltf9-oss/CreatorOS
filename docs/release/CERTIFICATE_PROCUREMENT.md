# Code-signing certificate procurement

**Status:** blocking owner action  
**Updated:** 2026-11-25  
**Release:** CreatorOS v1.0.0 Windows x64

No Authenticode certificate or private key is present in this repository or on the build
machine. The v1.0.0 installer must not be described as signed until a certificate issued by a
publicly trusted certificate authority (CA) is in the owner's control and the signed artifact has
been verified.

This document is procurement guidance, not a purchase, approval, or legal opinion. Prices and
availability must be confirmed at checkout; they change.

## 1. What must be purchased

A **publicly trusted code-signing certificate** that:

- chains to a root distributed by the Microsoft Trusted Root Program;
- contains the Code Signing extended key usage (EKU);
- is issued to the actual publisher identity (the individual owner, or a legal entity the owner
  controls), after CA identity/organisation validation;
- stores its private key in FIPS 140-2 Level 2, Common Criteria EAL 4+, or equivalent hardware,
  as required by the CA/Browser Forum since June 2023;
- has a validity of no more than 460 days (CA/Browser Forum Code Signing Baseline Requirements
  3.11.0, 2026-06-16, §6.3.2: certificates issued on or after 2026-03-01);
- can produce an Authenticode SHA-256 signature accepted by Windows 10/11 x64.

A self-signed certificate, an internally issued certificate, a test certificate, or a certificate
issued to another person or company does **not** satisfy this requirement. Smart App Control and
SmartScreen will not treat it as trusted.

The owner must also decide the **publisher name** that will appear in the certificate and in the
Windows "Publisher" field. It must match the validated identity. "CreatorOS contributors" is not
a legal person and cannot be validated unless an entity with that legal name exists.

## 2. Options for an individual resident in Algeria

### Not available: Microsoft Artifact Signing (Public Trust)

Microsoft's hosted signing service (formerly Trusted Signing) is not a route for this publisher.
Public Trust certificates are limited to organizations in the United States, Canada, the European
Union, the United Kingdom, Australia, New Zealand, Japan, South Korea, Singapore, Switzerland,
Norway and Israel; **individual developers must be located in the United States or Canada**. An
individual resident in Algeria does not qualify, and neither a fictitious foreign entity nor another
person's identity may be used to bypass that. A Private Trust profile does not issue a publicly
trusted code-signing certificate.

Verified 2026-11-25 at
<https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart> and
<https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options>.

### Option A — SSL.com Individual Validation (IV), recommended when name-only identity matters

*Verified 2026-11-25 against SSL.com's own pages: Algeria appears on the accepted-country list and
not on the restricted list; the one-year IV certificate is US$129.00 and the YubiKey FIPS token is
US$379.00.*

SSL.com issues an IV code-signing certificate to an individual, with no registered business
required. Its accepted country list includes Algeria (DZ); Algeria is not on its restricted
list.

| Item | Published price / fact |
| --- | --- |
| IV Code Signing, 1 year | **US$129.00** |
| YubiKey FIPS USB token | **US$379.00** each, plus shipping |
| Delivery | FIPS token; international shipping; delivery to Algeria is quoted at checkout |
| Validation | 3–5 business days standard, plus shipping |
| Certificate lifetime | Maximum 460 days for certificates issued on or after 2026-03-01 |
| Country eligibility | Algeria (`DZ`) is on SSL.com's accepted-country list; it is not export-restricted |
| Signing | Token in the Windows certificate store; existing signtool path works with the certificate thumbprint |
| Alternative | SSL.com eSigner cloud signing, Tier 1 US$180/year (240 signings); requires CodeSignTool/CSC integration rather than the current script |

**Approximate first-year cost:** US$508 plus international shipping, tax and any customs duty
(about US$540–$600 before local charges). Renewal is the certificate price unless a new token is
needed.

Why recommended: the certificate can carry the individual's verified name and country DZ; the
Windows publisher is the person who actually publishes the release. It does not require forming
a company.

Caveats:

- Confirm at checkout that the YubiKey can ship to Algeria and what the final shipping/tax is.
- The YubiKey PIN must be entered on the signing machine; the private key cannot be exported.
- An OV/IV signature removes "Unknown publisher" but does not instantly create SmartScreen
  reputation. Smart App Control acceptance must still be tested on a clean SAC-on machine.

Official references:

- <https://www.ssl.com/products/software-integrity/code-signing/iv/>
- <https://secure.ssl.com/certificates/iv-codesigning/buy>
- <https://www.ssl.com/country-codes/>
- <https://www.ssl.com/guide/esigner-pricing/>

### Option B — Certum Open Source Code Signing, lowest-cost option if the project qualifies

Certum (Asseco Data Systems) sells a discounted Open Source Code Signing certificate for
individual maintainers of public, non-commercial open-source projects. CreatorOS is public and
Apache-2.0, so it may qualify, but Certum decides.

| Item | Published price / fact |
| --- | --- |
| Open Source Code Signing — card + reader set | **€69.00 gross** |
| Open Source Code Signing — cloud (SimplySign) | **€49.00 gross** |
| Open Source Code Signing — activation code only | **€25.00 gross** |
| Shipping to Algeria | **€38.00 net** (Certum's official shipping table, outside-EU rate); customs/duty extra |
| Validity | 1 year |
| Certificate identity | Common Name includes "Open Source Developer"; Organization is "Open Source Developer" |
| Validation | Individual identity, utility bill, and a public project URL showing the applicant's relationship to the project |

**Approximate delivered cost:** €69 + €38 shipping + any tax/customs, about **€107–€121**.

Why consider it: it is a genuine publicly trusted code-signing certificate, explicitly aimed at
open-source maintainers, at a fraction of the cost of a commercial IV certificate. Certum states
the certificate is trusted by Microsoft and supports SmartScreen reputation.

Caveats:

- The certificate is issued **only to individuals** and is intended for **non-commercial**
  open-source distribution. Certum may revoke it if it is used to sign commercially distributed
  software. CreatorOS must remain a free open-source distribution for this option.
- The publisher string will be "Open Source Developer" plus the holder's name, not the person's
  name alone. If the release requirement is interpreted as requiring a certificate whose
  organisation field is exactly the publisher's legal name, this option does not meet that
  interpretation.
- The certificate's country field may reflect the CA's jurisdiction rather than Algeria. Confirm
  the exact subject with Certum before ordering if the country must be DZ.
- Physical card/reader shipping to Algeria and customs must be arranged.

Official references:

- <https://shop.certum.eu/open-source-code-signing.html>
- <https://shop.certum.eu/payment-shipping>
- <https://support.certum.eu/en/code-signing-required-documents/>
- <https://www.certum.eu/en/code-signing-certificates/>

### Option C — Sectigo individual/OV through a reseller

Some resellers sell a Sectigo individual code-signing certificate. Published reseller pricing is
about **US$219.99** for one year plus **US$130** for international token shipping, with a new
token each year under current rules. Sectigo's own site quotes a five-year package starting at
**US$536.25/year**, which is not cost-effective for this release.

This is a valid CA-issued option but adds a reseller and shipping layer. Confirm the certificate
is issued to the individual (not a company) and that the subject can carry the correct name and
country before paying.

References:

- <https://www.sectigo.com/ssl-certificates-tls/code-signing>
- <https://signmycode.com/sectigo-individual-code-signing>

### Option D — DigiCert OV

DigiCert's public code-signing products are organisation-validated; an individual without a
registered legal entity is not the target subscriber. The certificate is also materially more
expensive. This option becomes relevant only if the owner forms a legal entity and wants that
entity named as publisher.

## 3. Recommended decision

For the strict v1.0.0 requirement that the certificate be issued to the **actual publisher** with
the publisher's own name and country, choose **Option A (SSL.com IV + YubiKey)**. Budget roughly
US$550–$650 delivered, and allow 1–2 weeks for validation and shipping.

If the owner accepts the "Open Source Developer, <name>" publisher form and keeps CreatorOS
non-commercial, **Option B (Certum Open Source set)** is the economical route at roughly
€105–€120 delivered. It still satisfies "publicly trusted CA-issued certificate"; it does not
satisfy a name-only interpretation of the publisher requirement.

Either way, do **not** buy an EV certificate for this release. EV is not required for a normal
NSIS installer and costs more; OV/IV is sufficient for Authenticode trust. SmartScreen reputation
is separate and must be earned.

## 4. Owner procurement steps

1. **Confirm the legal publisher identity.** Use the exact legal name on the government ID
   (first/last name order as the CA requires). Decide whether the publisher is the individual or
   a registered entity. Do not use a pseudonym or "CreatorOS" alone.
2. **Confirm eligibility for Algeria.** For SSL.com, verify DZ is accepted and international
   token shipping is available at checkout. For Certum, verify the Open Source project qualifies
   and that the card/reader ships to Algeria.
3. **Place the order** with the CA or its authorised reseller. Use a monitored email address and a
   phone number the CA can call. Pay by a method that provides a receipt.
4. **Complete identity validation.** Typically: government photo ID, proof of address (utility
   bill), liveness/video or notarised identity confirmation, and a callback. Certum additionally
   requires the public project URL and evidence of the applicant's relationship to it.
5. **Receive and activate the token.** Install the vendor middleware/card reader driver, set a PIN,
   and confirm the certificate appears under Certificates - Current User - Personal with a
   private key and the Code Signing EKU. Keep the PIN out of the repository and out of chat.
6. **Send the public evidence to the release owner.** The certificate thumbprint, subject, issuer,
   serial number, validity dates, and a screenshot of certutil -store My are safe to share. Never
   share the PFX, PIN, token password, or private key.
7. **Run the local readiness check:**

   ~~~powershell
   powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check
   ~~~

   It must report a CA-issued certificate with a private key. A self-signed result is a failure.
8. **Sign only after the unsigned installer has passed its own tests.** The signer must be present
   to unlock the token. The release pipeline must fail closed if signing or timestamping fails.

## 5. What the release owner must provide back

- CA name and order/receipt reference.
- Certificate subject, issuer, serial, SHA-1/SHA-256 thumbprint, validity dates.
- Confirmation that the certificate has a private key and Code Signing EKU.
- The public certificate (.cer/.crt) only — never the private key.
- The exact publisher name to display in release notes.
- Confirmation of which timestamp authority to use (default: DigiCert RFC 3161,
  http://timestamp.digicert.com).

Once the token is present on the signing machine, the existing scripts\sign-release.ps1 can
sign with -Thumbprint, timestamp with /tr, and verify the chain. A hardware token is supported
through the Windows certificate store; no PFX export is required or permitted.

## 6. Explicit non-actions

- The agent will not purchase a certificate, submit identity documents, or accept a CA's terms on
  the owner's behalf.
- The agent will not generate a self-signed certificate and call the result signed.
- The agent will not attach the unsigned v0.8.0 installer to a v1.0.0 release.
- The agent will not publish a v1.0.0 installer until the certificate, timestamp, and clean
  SAC-on acceptance evidence exist.
