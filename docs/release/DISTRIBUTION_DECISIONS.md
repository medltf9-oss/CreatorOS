# Ollama and FFmpeg distribution decision packages

Wave 3 / release-candidate engineering. This document closes the distribution question for the two
external payloads the v1.0.0 brief asks to bundle. It is an engineering decision package, not a legal
opinion: obligations below are paraphrased from the named licences, and every option lists the
evidence that must exist before the release gate may accept it. Owner decisions are listed in §4.

## 0. What actually ships today (verified)

`app/desktop/electron-builder.yml` (`extraResources`) ships exactly: the backend interpreter bundle,
the icon, `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md`. It does **not** ship FFmpeg, ffprobe or
Ollama. `app/backend/src/creatoros/resources/prerequisites.v1.json` records
`approved_for_bundling: false` for both components, and `THIRD_PARTY_NOTICES.md` says the same in
prose. The Wave 5 0.8.0 installer is 139,882,940 bytes. Adding the pinned Ollama installer
(1,953,587,080 B) and both FFmpeg binaries (220,965,888 B) would total **2,314,435,908 B**
(2.15 GiB), which is **166,952,260 B over** GitHub's 2,147,483,648-byte per-file limit. The former
2,317,608,020 total was arithmetic error; 2,314,441,488 used the superseded installer.

Consequences:

- The current installer has no GPL or GPU-runtime redistribution surface. That is a strength, not a
  gap to paper over.
- It also does not satisfy the brief's "full offline installer" requirement. That requirement is
  therefore blocked on a decision below, not on missing build work.

## 1. FFmpeg / ffprobe

### 1.1 Verified facts

| Field | Value |
| --- | --- |
| Version | `9.1-essentials_build-www.gyan.dev` |
| Build page | <https://www.gyan.dev/ffmpeg/builds/> |
| `ffmpeg.exe` | 110,590,976 bytes, SHA-256 `ed8f66cb877087a96f5bbb40a2944f0e764f2ad8870d5cb647360d912e62bc07` |
| `ffprobe.exe` | 110,374,912 bytes, SHA-256 `df0199f606faa88e9ad192732a7c3987acfb647663534c297772e17730611a08` |
| Authenticode | not signed |
| Licence | GPLv3, SPDX `GPL-3.0-or-later`; built `--enable-gpl --enable-version3 --enable-libx264`, statically linking libx264 |
| Source reference | <https://github.com/FFmpeg/FFmpeg/commit/74e4393595> |
| Bundling approval | `approved_for_bundling: false` |
| Where the app looks | `<DATA_ROOT>\tools\ffmpeg\bin`; the wizard verifies bytes, it does not execute them |

### 1.2 Exact GPLv3 obligations if CreatorOS conveys the binary

These are the obligations that apply to the *binary* FFmpeg build, assuming CreatorOS distributes it
inside or alongside its own installer.

1. **Licence and notices (§4, §5).** Every recipient receives the GPLv3 text, the copyright notices
   and the no-warranty statement; notices stay intact. A CreatorOS EULA may not add terms to the
   FFmpeg files.
2. **Corresponding Source (§1, §6).** For this executable that means: the FFmpeg source at the exact
   commit, the source of every statically linked library in this build (libx264 and any other
   GPL/LGPL pieces), and the scripts/configure line that produced the executable. A source tarball
   alone is insufficient if the build scripts that reproduce *this* binary are missing — that is the
   current blocker recorded in the manifest.
3. **Conveyance route (§6).** For an installer downloaded from the internet, §6(d) is the practical
   route: equivalent access to the Corresponding Source from the same place, at no further charge.
   §6(b) written offer applies to physical products and must remain valid for at least three years.
4. **No additional restrictions (§10).** Nothing in the CreatorOS licence or EULA may forbid copying
   or redistributing the FFmpeg files, or require terms outside the GPL for them.
5. **Aggregation vs derivative work.** Shipping `ffmpeg.exe` as a separate program that CreatorOS
   invokes by path is normally mere aggregation, so CreatorOS's own Apache-2.0 code need not become
   GPL. Counsel must confirm the packaging and EULA do not present the result as one combined work;
   the README and notices must not claim FFmpeg is Apache-2.0.
6. **Patent grant, termination, warranty (§11, §8, §12).** The GPLv3 patent grant comes with the
   code; termination and reinstatement rules apply; CreatorOS may not imply a warranty for FFmpeg.
7. **Recording.** The source archive digest belongs in the release record and in the artifact SBOM
   (or as a release asset with a recorded SHA-256), because the gate verifies digests and refuses
   unverifiable claims.

An LGPL build would remove obligations 1-3 *for the GPL parts* and replace them with LGPL duties
(source of the library, notices, and the ability to relink or replace it — for static LGPL linking,
object files or a shared library). But this build's H.264 encoder is libx264, which is GPL; an LGPL
build cannot include it.

### 1.3 Architectures

| Option | User experience | CreatorOS conveys FFmpeg? | Obligations | Payload delta | Offline | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| A. Bundle the GPL build + Corresponding Source archive | media works immediately | yes | §1.2 items 1-7; exact build scripts, all static deps' source, notices, EULA review, source hosting | +220,965,888 B binaries, plus a source archive (tens of MB) | yes | Correct only after the source/build-script gap is closed and counsel signs off |
| B. Rebuild FFmpeg under LGPL (hardware encoders, openh264 fallback) | media works; H.264 depends on GPU or openh264 | yes (LGPL) | LGPL source/notices/relink; patent review for openh264; no libx264 | similar | yes | Viable engineering route, not a packaging tweak; changes the encoder product decision |
| C. First-run download from gyan.dev with the pinned hash | one guided download | no | verify SHA-256 before use; never execute silently; network UX and failure states | 0 | no | Recommended if one-click media is required |
| D. Present-only, user installs it (today) | manual install documented in `docs/INSTALLATION.md` | no | document the requirement; wizard verifies bytes | 0 | no | Shipping baseline; zero redistribution risk |
| E. Separate optional "media pack" the user downloads | isolated download | yes, if CreatorOS hosts it | same as A for the pack; keeps the base installer small | 0 in base | optional | Reasonable if offline media is a product goal |

### 1.4 Recommendation

- **v1.0.0:** keep option D. It is the only option that is fully verified today, and it keeps the
  release honest: the wizard tells the user what is missing and verifies the bytes when present.
- If the owner requires one-click media for 1.0.0, choose **C**; the manifest already pins the source
  URL, size and SHA-256, so the work is UX plus integrity verification, not licensing.
- Choose **A** only after: (i) the exact build scripts or a reproducible rebuild of the same flags
  exist, (ii) the source archive plus every static dependency's source is hosted and hashed, (iii)
  notices and SBOM entries are added, (iv) counsel accepts the aggregation/EULA position. Until then
  `approved_for_bundling` must stay `false` — the gate depends on it being truthful.
- **B** is a product decision for a later release, not a release-candidate fix.

## 2. Ollama

### 2.1 Verified facts

| Field | Value |
| --- | --- |
| Version | `0.40.18` |
| Installer | `OllamaSetup.exe`, 1,953,587,080 bytes |
| SHA-256 | `ad2bb47aa969b21cf8dd81b2d9f9a6d5030270ed242ef55087118c37c510acc2` |
| Authenticode | `Valid`; `CN=Ollama Inc., O=Ollama Inc., L=Toronto, S=Ontario, C=CA` |
| Timestamp | DigiCert SHA256 RSA4096 Timestamp Responder 2026 1 |
| Installer technology | Inno Setup; silent `OllamaSetup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` |
| Default install | per-user under `%LOCALAPPDATA%\Programs\Ollama` |
| Licence (manifest) | MIT for Ollama; the installer also carries GPU runtime components under their own terms |
| Acquisition (manifest) | network, consent required, pinned hash and vendor signature; `approved_for_bundling: false` |
| Where the app looks | Ollama endpoint from settings; models under `<DATA_ROOT>\models\ollama` |

The GPU runtime component inventory of `OllamaSetup.exe` has **not** been produced. The manifest's
caveat is the honest state: MIT covers Ollama's own code, not necessarily everything the installer
places on disk.

### 2.2 Exact obligations per option

- **MIT (if CreatorOS redistributes Ollama code):** include the copyright notice and the MIT
  permission text with the distributed copy; no warranty. MIT grants no trademark rights: "Ollama"
  may be used nominatively ("works with Ollama") but not to imply endorsement.
- **Redistributing the vendor installer unmodified:** MIT notice for Ollama, **plus** the notices of
  every third-party component inside the installer. GPU runtime pieces are commonly redistributed
  under vendor terms (for NVIDIA, the CUDA EULA's redistribution attachment; AMD/Intel components
  under their own licences). Those terms have not been read or inventoried here, so bundling cannot
  honestly be called compliant today. The manifest says the same.
- **Downloading and running the vendor installer on the user's machine:** no Ollama redistribution;
  the duties are (i) explicit user consent, (ii) verify SHA-256 **and** the Authenticode signer
  before executing, (iii) never install silently without consent, (iv) record the consent decision.
  The manifest already encodes exactly this (`consent.required=true`, pinned hash, expected signer).
- **Model weights:** separate licences (per-model, from the Ollama registry or the model publisher),
  downloaded separately; they are not covered by Ollama's MIT licence and must not be bundled.

### 2.3 Architectures

| Option | User experience | CreatorOS conveys Ollama? | Obligations | Payload delta | Offline | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| A. Bundle `OllamaSetup.exe`, install silently at install time | AI ready after install | yes | MIT notice + full third-party/GPU inventory + consent screen + installer EULA review + >2 GiB hosting plan | +1,953,587,080 B | yes | Not approvable until the payload is inventoried; the combined installer also exceeds GitHub's 2 GiB per-asset limit |
| B. Guided first-run download, verify hash + signer, run with consent | one guided download | no | consent, integrity + signer verification, failure states, no silent install | 0 | no | Recommended; matches the manifest contract |
| C. Detect or guide manual install (today) | documented manual step | no | documentation and detection only | 0 | no | Shipping baseline; zero redistribution risk |
| D. Extract and bundle only parts of the installer | smaller "runtime" | yes (modified) | redistribution of modified/extracted components, unsupported by the vendor, extra licence review | smaller | yes | Rejected |
| E. Separate optional "AI pack" hosted by CreatorOS | optional download | yes, in the pack | same as A for the pack, isolated from the base installer | 0 in base | optional | Workable if A's inventory is ever completed |

### 2.4 Recommendation

- **v1.0.0:** implement **B** as the product path (the manifest already pins URL, hash, signer and
  consent text); keep **C** as the fallback if the download UX is not finished. Neither option
  conveys Ollama bytes, so neither adds a licence surface to the installer.
- **A** only after the installer payload is inventoried, GPU runtime terms are reviewed, the
  combined payload has a hosting plan (split assets or an external host), and the owner explicitly
  accepts a 1.82 GiB download. Then `approved_for_bundling` may become `true` with evidence.
- **D** is rejected: it modifies/extracts a vendor payload and inherits support and licence risk.

## 3. Cross-cutting requirements once a decision changes the manifest

1. Artifact SBOM entries for every bundled component, including licences; `release.sbom.consistency`
   must still pass, and `scripts/generate_sbom.py --profile artifact` must name the exact versions
   and hashes.
2. `THIRD_PARTY_NOTICES.md` and the licence texts travel in `extraResources`, not only in the repo.
3. Corresponding Source assets (if GPL) are release assets with SHA-256 recorded in the release
   evidence, not a promise in prose.
4. A gate check should refuse Artifact/Release when a component present in the payload has
   `approved_for_bundling: false` (no such check exists yet; the gate currently validates the
   manifest's structure and digests).
5. A payload over 2 GiB cannot be a single GitHub release asset; the publish step needs a split or
   an external host recorded in the release notes.

## 4. Owner decisions required

1. **FFmpeg:** D (keep present-only; recommended), C (download with pinned hash), A (bundle GPL +
   source), or B (LGPL rebuild).
2. **Ollama:** C (keep detect/guide), B (guided verified download; recommended), A (bundle), or E
   (separate optional pack).
3. **If A is chosen for either:** authorize the source/asset hosting, commission the payload
   inventory (Ollama) or source/build-script collection (FFmpeg), and obtain counsel sign-off on
   the EULA/aggregation position.
4. **Scope:** confirm whether "full offline installer" remains a v1.0.0 requirement. If yes, the only
   compliant path is A for both, and it is blocked on the evidence above — not on engineering time.

## 5. What is not claimed

- No legal review has been performed; this is an engineering decision package.
- The Ollama installer's GPU component licences have not been read or inventoried.
- No certificate, Authenticode signature, timestamp or Smart App Control evidence is involved here.
- Choosing an option changes no manifest value by itself; the manifest changes only when the
  evidence for that option exists.
