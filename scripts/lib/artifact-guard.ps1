<#
.SYNOPSIS
    Verifies a release artifact's bytes and Authenticode signature immediately before it is executed.

.DESCRIPTION
    A build can be replaced, truncated or rebuilt after its checksum file was written. Running the
    installer without looking at the bytes again would execute whatever happens to sit at that path.
    Assert-ArtifactVerified closes that window: it hashes the artifact with Get-FileHash (the caller
    cannot supply or override the expected digest), requires exactly one matching SHA-256 entry in the
    release SHA256SUMS.txt, and inspects the Authenticode signature.

    The function refuses to return unless every requested check passed. Refusals throw with a message
    that starts with 'ARTIFACT GUARD:' and names the artifact, so a caller can report the refusal
    instead of executing the file.

    SHA256SUMS.txt format accepted (the format scripts/build-release.ps1 writes, and the GNU coreutils
    format): '<64 hex chars><two spaces><filename>', optionally with the binary marker
    ('<hash> *<filename>') and an optional parenthetical annotation after the filename, for example
    '<hash>  CreatorOS-Setup-1.0.0.exe  (136.4 MB)'. Blank lines and '#' comments are ignored.

    Signature policy:
      - default: a valid Authenticode signature passes; an unsigned artifact is refused unless
        -AllowUnsigned is passed.
      - -AllowUnsigned: also accept Status NotSigned, and say loudly that the build must not be
        published. Use it for local development builds only.
      - -RequireSignature: require Status Valid, a signer certificate and an RFC 3161 timestamp
        (TimeStamperCertificate). Use it for release evidence.
      - A signature that exists but is not valid (HashMismatch, NotTrusted, UnknownError, ...) is
        always refused, with or without -AllowUnsigned.

    The function does not sign anything and does not replace signing, the release gate or the SBOM.

.PARAMETER Path
    The artifact to verify. Its directory is where SHA256SUMS.txt is looked for unless -ChecksumFile
    is given.

.PARAMETER ChecksumFile
    An explicit SHA256SUMS.txt. Default: SHA256SUMS.txt next to the artifact.

.PARAMETER RequireSignature
    Release evidence mode: require a valid Authenticode signature with an RFC 3161 timestamp.

.PARAMETER AllowUnsigned
    Development mode: accept an unsigned artifact and warn that it must not be published.

.OUTPUTS
    PSCustomObject with Path, Name, Sha256, ChecksumFile, SignatureStatus, Signed, Timestamped and
    Unsigned.

.EXAMPLE
    $artifact = Assert-ArtifactVerified -Path releases\1.0.0\CreatorOS-Setup-1.0.0.exe -AllowUnsigned

.EXAMPLE
    Assert-ArtifactVerified -Path releases\1.0.0\CreatorOS-Setup-1.0.0.exe -RequireSignature -ChecksumFile releases\1.0.0\SHA256SUMS.txt
#>

function Assert-ArtifactVerified {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$Path,
        [string]$ChecksumFile = '',
        [switch]$RequireSignature,
        [switch]$AllowUnsigned
    )

    if ($RequireSignature -and $AllowUnsigned) {
        throw 'ARTIFACT GUARD: -RequireSignature and -AllowUnsigned are mutually exclusive: a release artifact must be signed, a development artifact may be unsigned.'
    }

    # --- 1. the artifact itself -----------------------------------------------------------------
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ("ARTIFACT GUARD: artifact not found: {0}" -f $Path)
    }
    $artifact = (Resolve-Path -LiteralPath $Path).ProviderPath
    $artifactName = Split-Path -Leaf $artifact

    # --- 2. the release digest manifest ---------------------------------------------------------
    if ($ChecksumFile) {
        if (-not (Test-Path -LiteralPath $ChecksumFile -PathType Leaf)) {
            throw ("ARTIFACT GUARD: checksum file not found: {0} (artifact: {1})" -f $ChecksumFile, $artifactName)
        }
        $sumsPath = (Resolve-Path -LiteralPath $ChecksumFile).ProviderPath
    } else {
        $sumsPath = Join-Path (Split-Path -Parent $artifact) 'SHA256SUMS.txt'
        if (-not (Test-Path -LiteralPath $sumsPath -PathType Leaf)) {
            throw ("ARTIFACT GUARD: SHA256SUMS.txt not found for {0}: expected {1}" -f $artifactName, $sumsPath)
        }
    }

    # --- 3. exactly one entry for this basename -------------------------------------------------
    # Format written by scripts/build-release.ps1 and by GNU coreutils:
    #   <64 hex chars><two spaces><filename>[<two spaces>(annotation)]
    #   <64 hex chars><space><asterisk><filename>            (binary mode marker)
    # Comments (#) and blank lines are ignored; the parenthetical annotation is not part of the name.
    $entryHashes = @()
    $manifestText = [System.IO.File]::ReadAllText($sumsPath)
    foreach ($line in ($manifestText -split '\r?\n')) {
        $candidate = $line.Trim()
        if ($candidate.Length -eq 0) { continue }
        if ($candidate.StartsWith('#')) { continue }
        $parsed = [regex]::Match($candidate, '^([0-9A-Fa-f]{64})(  | \*)(.+)$')
        if (-not $parsed.Success) { continue }
        $entryHash = $parsed.Groups[1].Value.ToLowerInvariant()
        $entryName = $parsed.Groups[3].Value
        $annotation = [regex]::Match($entryName, '^(.+?)  \([^()]*\)$')
        if ($annotation.Success) { $entryName = $annotation.Groups[1].Value }
        if ($entryName -ne $artifactName) { continue }
        $entryHashes += $entryHash
    }

    if ($entryHashes.Count -eq 0) {
        throw ("ARTIFACT GUARD: no SHA-256 entry for {0} in {1}" -f $artifactName, $sumsPath)
    }
    if ($entryHashes.Count -gt 1) {
        throw ("ARTIFACT GUARD: duplicate SHA-256 entries for {0} in {1} ({2} entries); refusing to guess which one is right" -f $artifactName, $sumsPath, $entryHashes.Count)
    }
    $expectedHash = $entryHashes[0]

    # --- 4. hash the bytes that would be executed -----------------------------------------------
    # Get-FileHash is the only source of truth: there is no parameter through which a caller could
    # supply or override the expected digest.
    $actualHash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw ("ARTIFACT GUARD: SHA-256 mismatch for {0}: manifest says {1}, file hashes to {2} ({3})" -f $artifactName, $expectedHash, $actualHash, $sumsPath)
    }

    # --- 5. Authenticode ------------------------------------------------------------------------
    try {
        $signature = Get-AuthenticodeSignature -LiteralPath $artifact -ErrorAction Stop
    } catch {
        throw ("ARTIFACT GUARD: could not read the Authenticode signature of {0}: {1}" -f $artifactName, $_.Exception.Message)
    }
    if ($null -eq $signature) {
        throw ("ARTIFACT GUARD: could not read the Authenticode signature of {0}: no signature object was returned" -f $artifactName)
    }
    $signatureStatus = [string]$signature.Status
    $signed = ($signatureStatus -eq 'Valid')
    $timestamped = ($null -ne $signature.TimeStamperCertificate)

    if ($RequireSignature) {
        if ($signatureStatus -ne 'Valid') {
            throw ("ARTIFACT GUARD: -RequireSignature: {0} is not validly signed (status={1}); release artifacts need a valid Authenticode signature" -f $artifactName, $signatureStatus)
        }
        if ($null -eq $signature.SignerCertificate) {
            throw ("ARTIFACT GUARD: -RequireSignature: {0} has no signer certificate (status={1})" -f $artifactName, $signatureStatus)
        }
        if ($null -eq $signature.TimeStamperCertificate) {
            throw ("ARTIFACT GUARD: -RequireSignature: {0} is signed but has no RFC 3161 timestamp (TimeStamperCertificate is empty)" -f $artifactName)
        }
    } elseif ($signatureStatus -eq 'NotSigned') {
        if (-not $AllowUnsigned) {
            throw ("ARTIFACT GUARD: {0} is unsigned; pass -AllowUnsigned for a local development build or -RequireSignature for release evidence" -f $artifactName)
        }
        Write-Warning ("UNSIGNED BUILD: {0} has no Authenticode signature. This is a local development build: it must not be published, attached to a release, or described as release evidence." -f $artifactName)
    } elseif (-not $signed) {
        $statusMessage = ''
        if ($signature.StatusMessage) { $statusMessage = ' ' + $signature.StatusMessage }
        throw ("ARTIFACT GUARD: {0} has an invalid Authenticode signature (status={1}).{2}" -f $artifactName, $signatureStatus, $statusMessage)
    } elseif (-not $timestamped) {
        Write-Warning ("{0} is signed but carries no RFC 3161 timestamp; acceptable for a local build, not for release evidence (-RequireSignature enforces it)." -f $artifactName)
    }

    return [pscustomobject]@{
        Path = $artifact
        Name = $artifactName
        Sha256 = $actualHash
        ChecksumFile = $sumsPath
        SignatureStatus = $signatureStatus
        Signed = $signed
        Timestamped = $timestamped
        Unsigned = ($signatureStatus -eq 'NotSigned')
    }
}
