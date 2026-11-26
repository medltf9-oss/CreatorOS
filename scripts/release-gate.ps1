# CreatorOS release gate.
#
# Checks the mandatory gates for a Windows release and fails closed. It does not build, sign or
# publish anything. Run it before tagging, after signing, and again before publishing.
#
#   powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Source
#   powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Artifact -ExpectedPublisher "Jane Doe"
#   powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Release -ExpectedPublisher "Jane Doe" -EvidencePath docs\evidence\v1.0.0-clean-sac.json
#
# Stage Source    version, documentation, source + prerequisite SBOMs (generated when absent),
#                 tracked-secret scan, clean git tree
# Stage Artifact  Source + installer digest, manifest, Authenticode, timestamp, publisher
# Stage Release   Artifact + clean SAC-on evidence + tag + GitHub release asset + CI + branch protection
#
# A failed gate is a release blocker. Do not work around it by disabling a check.

[CmdletBinding()]
param(
    [string]$ReleaseVersion = '1.0.0',
    [ValidateSet('Source', 'Artifact', 'Release')]
    [string]$Stage = 'Release',
    [string]$Artifact = '',
    [string]$EvidencePath = '',
    [string]$ExpectedPublisher = $env:CREATOROS_PUBLISHER,
    [switch]$AllowDirty,
    [switch]$SkipNetwork
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$failures = [System.Collections.Generic.List[string]]::new()
$passes = [System.Collections.Generic.List[string]]::new()

function Add-Gate {
    param([string]$Id, [bool]$Ok, [string]$Detail)
    if ($Ok) {
        $passes.Add("$Id - $Detail")
        Write-Host ("  PASS  {0,-34} {1}" -f $Id, $Detail) -ForegroundColor Green
    } else {
        $failures.Add("$Id - $Detail")
        Write-Host ("  FAIL  {0,-34} {1}" -f $Id, $Detail) -ForegroundColor Red
    }
}

function Get-TrackedTextMatch {
    param([string]$Pattern)
    $extensions = @('.md', '.txt', '.ps1', '.py', '.js', '.cjs', '.mjs', '.ts', '.tsx', '.json', '.yml', '.yaml', '.example', '.toml', '.ini', '.nsh', '.css', '.html')
    $files = & git -C $repo ls-files
    $hits = @()
    foreach ($relative in $files) {
        $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
        if ($extensions -notcontains $extension) { continue }
        # Test fixtures, documentation examples and the evidence directory are audited separately;
        # they intentionally contain placeholder strings such as ghp_abc... and <password>.
        if ($relative -match '(^|/)(tests?|fixtures?)/' -or $relative -match '\.md$' -or $relative -match '^docs/') { continue }
        $full = Join-Path $repo $relative
        if (-not (Test-Path -LiteralPath $full)) { continue }
        $found = Select-String -LiteralPath $full -Pattern $Pattern -ErrorAction SilentlyContinue
        foreach ($item in $found) { $hits += ($relative + ' line ' + $item.LineNumber) }
    }
    return $hits
}

function Get-SbomPython {
    $venv = Join-Path $repo 'app\backend\.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    foreach ($name in @('python', 'py')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    return $null
}

function New-SbomForGate {
    param([string]$Profile, [string]$Output, [string]$Prerequisites)
    $python = Get-SbomPython
    if (-not $python) {
        return @{ ok = $false; detail = 'no Python interpreter: create app\backend\.venv or put python on PATH' }
    }
    $arguments = @(
        'scripts\generate_sbom.py',
        '--output', $Output,
        '--profile', $Profile,
        '--version', $ReleaseVersion,
        '--check'
    )
    if ($Prerequisites) { $arguments += @('--prerequisites', $Prerequisites) }
    $text = @(& $python @arguments 2>&1)
    $code = $LASTEXITCODE
    $tail = ($text | Select-Object -Last 1) -join ' '
    return @{ ok = ($code -eq 0); detail = "generated $Profile profile: $tail" }
}

function Test-SbomFile {
    param(
        [string]$Path,
        [ValidateSet('source', 'prerequisites', 'artifact')][string]$Profile
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{ ok = $false; detail = "missing SBOM: $Path" }
    }
    try {
        $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $problems = @()
        if ([string]$document.bomFormat -ne 'CycloneDX') { $problems += "bomFormat is '$($document.bomFormat)', expected CycloneDX" }
        if (@('1.4', '1.5', '1.6') -notcontains [string]$document.specVersion) { $problems += "specVersion is '$($document.specVersion)'" }
        $documentVersion = [string]$document.metadata.component.version
        if ($documentVersion -ne $ReleaseVersion) { $problems += "metadata.component.version is '$documentVersion', expected $ReleaseVersion" }
        $serial = [string]$document.serialNumber
        if ($serial -notmatch '^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
            $serial -eq 'urn:uuid:00000000-0000-0000-0000-000000000000') {
            $problems += "serialNumber is '$serial', expected a real urn:uuid"
        }
        $stamp = [datetime]::MinValue
        $stampText = [string]$document.metadata.timestamp
        $parsedStamp = [datetime]::TryParse(
            $stampText,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal,
            [ref]$stamp)
        if (-not $parsedStamp) {
            $problems += "metadata.timestamp is '$stampText'"
        } else {
            $ageDays = ((Get-Date).ToUniversalTime() - $stamp.ToUniversalTime()).TotalDays
            if ($ageDays -gt 30) { $problems += "SBOM is $([math]::Round($ageDays)) days old; evidence must be fresh (<= 30)" }
            if ($ageDays -lt -1) { $problems += 'SBOM timestamp is in the future' }
        }
        $components = @($document.components | Where-Object { $_ })
        if ($components.Count -lt 1) { $problems += 'no components' }
        $incomplete = @($components | Where-Object { -not $_.name -or -not $_.version -or -not $_.type })
        if ($incomplete.Count -gt 0) { $problems += "$($incomplete.Count) component(s) missing name, version or type" }
        $duplicateRefs = @($components | ForEach-Object { [string]$_.'bom-ref' } | Where-Object { $_ } | Group-Object | Where-Object { $_.Count -gt 1 })
        if ($duplicateRefs.Count -gt 0) { $problems += "duplicate bom-ref: $((($duplicateRefs | ForEach-Object { $_.Name }) -join ', '))" }
        $collectionErrors = @($components | Where-Object {
            $_.properties -and @($_.properties | Where-Object { [string]$_.name -eq 'creatoros:collection-error' }).Count -gt 0
        })
        if ($collectionErrors.Count -gt 0) { $problems += "$($collectionErrors.Count) component(s) carry a collection error; the environment is incomplete" }
        if ($Profile -eq 'source') {
            foreach ($required in @('creatoros-desktop', 'creatoros-backend')) {
                if (@($components | Where-Object { [string]$_.name -eq $required }).Count -eq 0) {
                    $problems += "source SBOM has no $required component"
                }
            }
            if ($components.Count -lt 3) { $problems += 'source SBOM has fewer than 3 components' }
        }
        if ($Profile -eq 'prerequisites') {
            foreach ($required in @('ffmpeg', 'ffprobe')) {
                $matches = @($components | Where-Object { [string]$_.name -eq $required })
                if ($matches.Count -ne 1) {
                    $problems += "prerequisite SBOM must carry exactly one $required component"
                } elseif (@($matches[0].hashes | Where-Object { [string]$_.alg -eq 'SHA-256' -and $_.content }).Count -ne 1) {
                    $problems += "$required carries no SHA-256 hash"
                }
            }
            $unhashed = @($components | Where-Object {
                @($_.hashes | Where-Object { [string]$_.alg -eq 'SHA-256' -and $_.content }).Count -eq 0
            })
            if ($unhashed.Count -gt 0) { $problems += "$($unhashed.Count) prerequisite component(s) carry no SHA-256 hash" }
            $unlicensed = @($components | Where-Object {
                -not $_.licenses -and -not ($_.properties | Where-Object { [string]$_.name -eq 'creatoros:license-status' })
            })
            if ($unlicensed.Count -gt 0) { $problems += "$($unlicensed.Count) prerequisite component(s) record no licence" }
        }
        if ($Profile -eq 'artifact') {
            if ($components.Count -lt 3) { $problems += 'artifact SBOM has fewer than 3 components' }
        }
        if ($problems.Count -eq 0) {
            return @{ ok = $true; detail = "$Path ($($components.Count) components, $Profile, version $documentVersion)" }
        }
        return @{ ok = $false; detail = "$Path failed: $($problems -join '; ')" }
    } catch {
        return @{ ok = $false; detail = "SBOM is not valid JSON: $Path ($($_.Exception.Message))" }
    }
}

Write-Host ''
Write-Host "CreatorOS release gate $ReleaseVersion (stage: $Stage)" -ForegroundColor Cyan
Write-Host "Repository: $repo" -ForegroundColor DarkGray
Write-Host ''

# --------------------------------------------------------------------------------- source identity
$packagePath = Join-Path $repo 'app\desktop\package.json'
$package = Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json
Add-Gate 'source.desktop-version' ($package.version -eq $ReleaseVersion) "package.json is $($package.version), expected $ReleaseVersion"

$pyproject = Get-Content -LiteralPath (Join-Path $repo 'app\backend\pyproject.toml') -Raw
$match = [regex]::Match($pyproject, '(?m)^version\s*=\s*"([^"]+)"')
$backendVersion = if ($match.Success) { $match.Groups[1].Value } else { 'missing' }
Add-Gate 'source.backend-version' ($backendVersion -eq $ReleaseVersion) "pyproject.toml is $backendVersion, expected $ReleaseVersion"

$settings = Get-Content -LiteralPath (Join-Path $repo 'app\backend\src\creatoros\core\settings.py') -Raw
$match = [regex]::Match($settings, 'version:\s*str\s*=\s*"([^"]+)"')
$settingsVersion = if ($match.Success) { $match.Groups[1].Value } else { 'missing' }
Add-Gate 'source.settings-version' ($settingsVersion -eq $ReleaseVersion) "settings.py is $settingsVersion, expected $ReleaseVersion"

$changelog = Get-Content -LiteralPath (Join-Path $repo 'CHANGELOG.md') -Raw
Add-Gate 'source.changelog' ($changelog.Contains("## [$ReleaseVersion]")) "CHANGELOG.md has a $ReleaseVersion section"

$notesPath = Join-Path $repo "docs\RELEASE_NOTES_$ReleaseVersion.md"
Add-Gate 'source.release-notes' (Test-Path -LiteralPath $notesPath) "docs\RELEASE_NOTES_$ReleaseVersion.md exists"

$requiredDocs = @(
    'LICENSE',
    'NOTICE',
    'THIRD_PARTY_NOTICES.md',
    "docs\RELEASE_NOTES_$ReleaseVersion.md",
    'docs\CODE_SIGNING.md',
    'docs\INSTALLATION.md'
)
$missingDocs = @($requiredDocs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $repo $_)) })
Add-Gate 'source.required-docs' ($missingDocs.Count -eq 0) $(if ($missingDocs.Count -eq 0) { 'all required release documents exist' } else { 'missing: ' + ($missingDocs -join ', ') })

# --------------------------------------------------------------------------------- git state
if ($AllowDirty) {
    Write-Host '  SKIP  git.clean                          -AllowDirty was requested' -ForegroundColor Yellow
} else {
    $status = & git -C $repo status --porcelain
    $dirty = ($status | Where-Object { $_ -and $_.Trim() }) -join '; '
    Add-Gate 'git.clean' ([string]::IsNullOrWhiteSpace($dirty)) $(if ([string]::IsNullOrWhiteSpace($dirty)) { 'working tree is clean' } else { "uncommitted changes: $dirty" })
}

$head = (& git -C $repo rev-parse HEAD).Trim()
Add-Gate 'git.head' ($head -match '^[0-9a-f]{40}$') "HEAD is $head"

# --------------------------------------------------------------------------------- tracked secrets
$privateKeyHits = Get-TrackedTextMatch 'BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY'
$tokenHits = Get-TrackedTextMatch 'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}'
$passwordHits = Get-TrackedTextMatch 'CSC_KEY_PASSWORD\s*=\s*["''](?!(<|…|\.\.\.))[^"'']{8,}["'']'
$secretHits = @($privateKeyHits + $tokenHits + $passwordHits | Select-Object -Unique)
Add-Gate 'security.tracked-secrets' ($secretHits.Count -eq 0) $(if ($secretHits.Count -eq 0) { 'no private keys, tokens or literal signing passwords in tracked source/config files (docs and test fixtures excluded)' } else { 'review: ' + ($secretHits -join ', ') })

# --------------------------------------------------------------------------------- SBOM and manifests
# Three SBOMs, three claims. The source document describes the checkout and its dependencies; the
# prerequisite document describes the tools the user supplies; the artifact document is bound to the
# installer bytes. Source and prerequisite documents are generated when absent, so a clean clone can
# run this stage without inventing evidence. An artifact SBOM is never generated here: it must hash a
# real installer, which only the build produces.
$releaseDir = Join-Path $repo "releases\$ReleaseVersion"
$prereqManifest = Join-Path $repo 'app\backend\src\creatoros\resources\prerequisites.v1.json'
$sourceSbomPath = Join-Path $releaseDir 'sbom-source.cdx.json'
$prereqSbomPath = Join-Path $releaseDir 'sbom-prerequisites.cdx.json'

foreach ($spec in @(
    @{ Path = $sourceSbomPath; Profile = 'source' },
    @{ Path = $prereqSbomPath; Profile = 'prerequisites' }
)) {
    $generationFailed = $false
    if (-not (Test-Path -LiteralPath $spec.Path)) {
        $generated = New-SbomForGate -Profile $spec.Profile -Output $spec.Path -Prerequisites $prereqManifest
        if (-not $generated.ok) {
            Add-Gate "release.sbom.$($spec.Profile)" $false $generated.detail
            $generationFailed = $true
        } else {
            Write-Host ("  INFO  {0,-34} {1}" -f "release.sbom.$($spec.Profile)", $generated.detail) -ForegroundColor DarkGray
        }
    }
    if (-not $generationFailed) {
        $result = Test-SbomFile -Path $spec.Path -Profile $spec.Profile
        Add-Gate "release.sbom.$($spec.Profile)" $result.ok $result.detail
    }
}

$sbomCandidates = @(
    (Join-Path $releaseDir 'sbom.cdx.json'),
    (Join-Path $releaseDir 'sbom.spdx.json'),
    (Join-Path $repo "docs\release\sbom-$ReleaseVersion.cdx.json")
)
$sbomPath = $sbomCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($sbomPath) {
    $artifactSbom = Test-SbomFile -Path $sbomPath -Profile 'artifact'
    Add-Gate 'release.sbom' $artifactSbom.ok $artifactSbom.detail
    try {
        $sourceRefs = @((Get-Content -LiteralPath $sourceSbomPath -Raw | ConvertFrom-Json).components | ForEach-Object { [string]$_.'bom-ref' })
        $artifactRefs = @((Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json).components | ForEach-Object { [string]$_.'bom-ref' })
        $missingRefs = @($sourceRefs | Where-Object { $_ -and $artifactRefs -notcontains $_ })
        Add-Gate 'release.sbom.consistency' ($missingRefs.Count -eq 0) $(if ($missingRefs.Count -eq 0) { "artifact SBOM contains all $($sourceRefs.Count) source components" } else { "artifact SBOM omits $($missingRefs.Count) source component(s): $($missingRefs -join ', ')" })
    } catch {
        Add-Gate 'release.sbom.consistency' $false "cannot compare source and artifact SBOMs: $($_.Exception.Message)"
    }
} elseif ($Stage -eq 'Source') {
    Write-Host '  INFO  release.sbom                     no artifact SBOM yet; generated by build-release.ps1 after the installer exists' -ForegroundColor DarkGray
} else {
    Add-Gate 'release.sbom' $false "no artifact SBOM for $ReleaseVersion; run scripts\build-release.ps1"
}

# The pinned prerequisite manifest is what the first-run wizard verifies against. It must stay
# machine-readable, fully pinned and consistent with the audit record a release is judged by.
$prereqPath = Join-Path $repo 'app\backend\src\creatoros\resources\prerequisites.v1.json'
$prereqOk = $false
$prereqDetail = "missing: $prereqPath"
$prereqHashesOk = $false
$prereqHashDetail = 'not checked'
if (Test-Path -LiteralPath $prereqPath) {
    try {
        $prereq = Get-Content -LiteralPath $prereqPath -Raw | ConvertFrom-Json
        $problems = @()
        if ([string]$prereq.schema -ne 'creatoros.prerequisites.v1') { $problems += "schema is '$($prereq.schema)'" }
        $prereqComponents = @($prereq.components | Where-Object { $_ })
        if ($prereqComponents.Count -lt 1) { $problems += 'no components' }
        $ids = @($prereqComponents | ForEach-Object { [string]$_.id })
        if (($ids | Select-Object -Unique).Count -ne $ids.Count) { $problems += 'duplicate component ids' }
        foreach ($component in $prereqComponents) {
            $label = [string]$component.id
            if ($label -notmatch '^[a-z0-9][a-z0-9-]*$') { $problems += "invalid id '$label'" }
            $acquisition = $component.acquisition
            if (-not $acquisition) { $problems += "$label has no acquisition"; continue }
            $mode = [string]$acquisition.mode
            if (@('official-download', 'local') -notcontains $mode) { $problems += "$label has unknown acquisition mode '$mode'" }
            if ($mode -eq 'official-download') {
                if (-not ([string]$acquisition.url).StartsWith('https://')) { $problems += "$label download URL is not https" }
                if ([string]$acquisition.url -notmatch '^https://[^\s]+$') { $problems += "$label download URL has spaces" }
                if ($component.consent.required -ne $true) { $problems += "$label downloads over the network without consent" }
            }
            if ([string]$acquisition.filename -match '[\\/:*?"<>|]') { $problems += "$label filename is not a bare file name" }
            if (-not ($acquisition.size_bytes -is [long] -or $acquisition.size_bytes -is [int]) -or $acquisition.size_bytes -le 0) { $problems += "$label has no positive size_bytes" }
            if ([string]$acquisition.sha256 -notmatch '^[0-9a-f]{64}$') { $problems += "$label sha256 is not 64 lowercase hex characters" }
            if ($null -eq $component.signature -or $component.signature.required -isnot [bool]) { $problems += "$label does not declare signature.required as a boolean" }
            elseif ($component.signature.required -eq $true -and [string]::IsNullOrWhiteSpace([string]$component.signature.expected_subject)) { $problems += "$label requires a signature but names no expected_subject" }
            if ($null -eq $component.redistribution -or $component.redistribution.approved_for_bundling -isnot [bool]) { $problems += "$label does not declare redistribution.approved_for_bundling as a boolean" }
        }
        if ($problems.Count -eq 0) {
            $prereqOk = $true
            $prereqDetail = "$($prereqComponents.Count) pinned components in prerequisites.v1.json"
        } else {
            $prereqDetail = "prerequisites.v1.json failed: $($problems -join '; ')"
        }

        $dependencyDoc = Join-Path $repo 'docs\release\DEPENDENCY_MANIFEST.md'
        if (-not (Test-Path -LiteralPath $dependencyDoc)) {
            $prereqHashDetail = "missing audit record: $dependencyDoc"
        } else {
            $docText = (Get-Content -LiteralPath $dependencyDoc -Raw).ToLowerInvariant()
            $missingHashes = @($prereqComponents | Where-Object { -not $docText.Contains(([string]$_.acquisition.sha256).ToLowerInvariant()) } | ForEach-Object { $_.id })
            if ($missingHashes.Count -eq 0) {
                $prereqHashesOk = $true
                $prereqHashDetail = 'every pinned digest is recorded in docs\release\DEPENDENCY_MANIFEST.md'
            } else {
                $prereqHashDetail = "digests absent from DEPENDENCY_MANIFEST.md: $($missingHashes -join ', ')"
            }
        }
    } catch {
        $prereqDetail = "prerequisites.v1.json is not valid JSON: $($_.Exception.Message)"
    }
}
Add-Gate 'release.prerequisites' $prereqOk $prereqDetail
Add-Gate 'release.prerequisites-hashes' $prereqHashesOk $prereqHashDetail

# --------------------------------------------------------------------------------- signing identity
$now = Get-Date
$signingCerts = @(Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue | Where-Object {
    $_.HasPrivateKey -and
    $_.NotAfter -gt $now -and
    $_.NotBefore -lt $now -and
    $_.Issuer -ne $_.Subject -and
    ($_.EnhancedKeyUsageList | Where-Object { $_.ObjectId -eq '1.3.6.1.5.5.7.3.3' })
})
$certDetail = if ($signingCerts.Count -eq 0) {
    'no valid CA-issued code-signing certificate with a private key in CurrentUser\My or LocalMachine\My'
} else {
    ($signingCerts | ForEach-Object { "$($_.Subject) [$($_.Thumbprint)]" }) -join '; '
}
if ($Stage -eq 'Source') {
    Write-Host '  INFO  signing.certificate                not required at Source stage' -ForegroundColor DarkGray
} else {
    Add-Gate 'signing.certificate' ($signingCerts.Count -gt 0) $certDetail
}

if (-not $ExpectedPublisher -and $Stage -ne 'Source') {
    Add-Gate 'signing.publisher' $false 'set -ExpectedPublisher or CREATOROS_PUBLISHER to the validated certificate subject'
} elseif ($Stage -ne 'Source') {
    Add-Gate 'signing.publisher' $true "expected publisher: $ExpectedPublisher"
}

# --------------------------------------------------------------------------------- artifact
if ($Stage -ne 'Source') {
    $artifactPath = if ($Artifact) { $Artifact } else { Join-Path $releaseDir "CreatorOS-Setup-$ReleaseVersion.exe" }
    $artifactExists = Test-Path -LiteralPath $artifactPath
    Add-Gate 'artifact.exists' $artifactExists $(if ($artifactExists) { $artifactPath } else { "missing: $artifactPath" })

    if ($artifactExists) {
        $item = Get-Item -LiteralPath $artifactPath
        Add-Gate 'artifact.size' ($item.Length -gt 50MB) ("{0:N0} bytes" -f $item.Length)
        $productVersion = $item.VersionInfo.ProductVersion
        Add-Gate 'artifact.version' ($productVersion -eq $ReleaseVersion) "installer ProductVersion: $productVersion"
        $hash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $name = $item.Name
        $manifestPath = Join-Path $releaseDir 'SHA256SUMS.txt'
        $manifestText = if (Test-Path -LiteralPath $manifestPath) { Get-Content -LiteralPath $manifestPath -Raw } else { '' }
        $manifestOk = $manifestText -match ('(?im)^' + [regex]::Escape($hash) + '\s+' + [regex]::Escape($name) + '\b')
        Add-Gate 'artifact.manifest' $manifestOk "SHA256SUMS.txt records $hash for $name"

        # Every file the manifest names must exist next to it and carry the recorded digest. Checking
        # only the installer would let a stale blockmap or an extra asset ship under a green gate.
        $manifestProblems = @()
        $manifestLines = [regex]::Matches($manifestText, '(?m)^([0-9a-fA-F]{64})[ \t]{2}([^\r\n]+?)(?:[ \t]{2}\([^)]*\))?[ \t]*\r?$')
        foreach ($match in $manifestLines) {
            $recorded = $match.Groups[1].Value.ToLowerInvariant()
            $listedName = $match.Groups[2].Value.Trim()
            $listedPath = Join-Path $releaseDir $listedName
            if (-not (Test-Path -LiteralPath $listedPath -PathType Leaf)) {
                $manifestProblems += "missing file: $listedName"
                continue
            }
            $actual = (Get-FileHash -LiteralPath $listedPath -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $recorded) { $manifestProblems += "hash mismatch: $listedName" }
        }
        if ($manifestLines.Count -eq 0) { $manifestProblems += 'no digest lines in SHA256SUMS.txt' }
        Add-Gate 'artifact.manifest-scope' ($manifestProblems.Count -eq 0) $(if ($manifestProblems.Count -eq 0) { "$($manifestLines.Count) manifest entr$(if ($manifestLines.Count -eq 1) { 'y' } else { 'ies' }) verified" } else { $manifestProblems -join '; ' })

        # The SBOM is release evidence only when it describes these exact bytes. A document that omits
        # the installer hash is provenance for a build, not for the artifact being released.
        $sbomBindOk = $false
        $sbomBindDetail = 'no SBOM found to bind the installer hash'
        if ($sbomPath -and (Test-Path -LiteralPath $sbomPath)) {
            try {
                $sbomDoc = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json
                $recordedHashes = @($sbomDoc.metadata.component.hashes | Where-Object { $_ -and [string]$_.alg -eq 'SHA-256' } | ForEach-Object { ([string]$_.content).ToLowerInvariant() })
                $sbomBindOk = $recordedHashes -contains $hash
                $sbomBindDetail = if ($sbomBindOk) {
                    "SBOM metadata.component.hashes records installer SHA-256 $hash"
                } else {
                    "SBOM does not record installer SHA-256 $hash (recorded: $($recordedHashes -join ', '))"
                }
            } catch {
                $sbomBindDetail = "SBOM cannot be read: $($_.Exception.Message)"
            }
        }
        Add-Gate 'artifact.sbom' $sbomBindOk $sbomBindDetail

        $sig = Get-AuthenticodeSignature -LiteralPath $artifactPath
        Add-Gate 'artifact.signature' ($sig.Status -eq 'Valid') "Authenticode status: $($sig.Status)"
        Add-Gate 'artifact.timestamp' ($null -ne $sig.TimeStamperCertificate) $(if ($sig.TimeStamperCertificate) { "RFC 3161 timestamp: $($sig.TimeStamperCertificate.Subject)" } else { 'no timestamp certificate' })

        $signerSubject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }
        $signerOk = $ExpectedPublisher -and $signerSubject -like "*$ExpectedPublisher*"
        Add-Gate 'artifact.publisher' ([bool]$signerOk) "signer: $signerSubject"

        $signtool = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'x64\signtool.exe' } |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
        if ($signtool) {
            $previousErrorAction = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                & $signtool verify /pa /all $artifactPath 2>&1 | Out-Null
                $signtoolExit = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $previousErrorAction
            }
            Add-Gate 'artifact.signtool' ($signtoolExit -eq 0) "signtool verify /pa /all (exit $signtoolExit)"
        } else {
            Add-Gate 'artifact.signtool' $false 'signtool.exe not found; cannot independently verify the chain'
        }
    }
}

# --------------------------------------------------------------------------------- clean-machine evidence
if ($Stage -eq 'Release') {
    $evidence = if ($EvidencePath) { $EvidencePath } else { Join-Path $repo "docs\evidence\v$ReleaseVersion-clean-sac.json" }
    $evidenceExists = Test-Path -LiteralPath $evidence
    Add-Gate 'clean.evidence-file' $evidenceExists $(if ($evidenceExists) { $evidence } else { "missing clean-machine evidence: $evidence" })

    if ($evidenceExists) {
        try {
            $e = Get-Content -LiteralPath $evidence -Raw | ConvertFrom-Json
            $artifactHash = if ($artifactExists) { (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant() } else { '' }

            # The shape of the file is a contract (docs\evidence\SCHEMA.md). A missing key or a value of
            # the wrong type is not a "maybe": it means the file was not produced by the documented
            # pipeline, so every later gate would be reading something other than what it assumes.
            $schemaProblems = @()
            $evidenceKeys = @('release', 'artifact_sha256', 'date', 'tester', 'windows', 'sac', 'defender', 'installer', 'tests')
            foreach ($key in $evidenceKeys) {
                if ($null -eq $e.PSObject.Properties[$key]) { $schemaProblems += "missing '$key'" }
            }
            foreach ($property in $e.PSObject.Properties) {
                if ($evidenceKeys -notcontains $property.Name) { $schemaProblems += "unexpected '$($property.Name)'" }
            }
            if ($null -ne $e.PSObject.Properties['_template'] -or $null -ne $e.PSObject.Properties['_not_a_result']) {
                $schemaProblems += 'the file is a template, not an observation'
            }
            $nestedKeys = @{
                windows   = @('edition', 'build', 'architecture', 'clean_install')
                sac       = @('state', 'source')
                defender  = @('realtime_protection', 'antivirus_enabled', 'signature_age_hours')
                installer = @('signature_status', 'publisher', 'timestamped')
            }
            foreach ($section in $nestedKeys.Keys) {
                $sectionValue = $e.$section
                if ($null -eq $sectionValue -or $sectionValue -isnot [pscustomobject]) { continue }
                foreach ($nestedKey in $nestedKeys[$section]) {
                    if ($null -eq $sectionValue.PSObject.Properties[$nestedKey]) { $schemaProblems += "$section.$nestedKey is missing" }
                }
            }
            if ([string]$e.release -notmatch '^\d+\.\d+\.\d+$') { $schemaProblems += "release is not a version: '$($e.release)'" }
            if ([string]$e.artifact_sha256 -notmatch '^[0-9a-f]{64}$') { $schemaProblems += 'artifact_sha256 is not 64 lowercase hex characters' }
            if ([string]::IsNullOrWhiteSpace([string]$e.tester)) { $schemaProblems += 'tester is empty' }
            if ([string]$e.tester -match '^(?i)(tbd|todo|unknown|n/a|tester|placeholder|<.*>)$') { $schemaProblems += "tester is a placeholder: '$($e.tester)'" }
            $dateParses = $false
            try { [void][datetime]::Parse([string]$e.date, [System.Globalization.CultureInfo]::InvariantCulture); $dateParses = $true } catch { }
            if (-not $dateParses) { $schemaProblems += "date does not parse: '$($e.date)'" }
            if ($null -eq $e.windows -or $e.windows -isnot [pscustomobject]) { $schemaProblems += 'windows is not an object' }
            else {
                if ($null -ne $e.windows.build -and $e.windows.build -isnot [int] -and $e.windows.build -isnot [long]) { $schemaProblems += 'windows.build is not an integer or null' }
                if ($null -ne $e.windows.clean_install -and $e.windows.clean_install -isnot [bool]) { $schemaProblems += 'windows.clean_install is not a boolean or null' }
                if ($null -ne $e.windows.edition -and $e.windows.edition -isnot [string]) { $schemaProblems += 'windows.edition is not a string or null' }
                if ($null -ne $e.windows.architecture -and $e.windows.architecture -isnot [string]) { $schemaProblems += 'windows.architecture is not a string or null' }
            }
            if ($null -eq $e.sac -or $e.sac -isnot [pscustomobject]) { $schemaProblems += 'sac is not an object' }
            else {
                if ($null -ne $e.sac.state -and $e.sac.state -isnot [int] -and $e.sac.state -isnot [long]) { $schemaProblems += 'sac.state is not an integer or null' }
                if ($null -ne $e.sac.source -and $e.sac.source -isnot [string]) { $schemaProblems += 'sac.source is not a string or null' }
            }
            if ($null -eq $e.defender -or $e.defender -isnot [pscustomobject]) { $schemaProblems += 'defender is not an object' }
            else {
                foreach ($key in @('realtime_protection', 'antivirus_enabled')) {
                    $value = $e.defender.$key
                    if ($null -ne $value -and $value -isnot [bool]) { $schemaProblems += "defender.$key is not a boolean or null" }
                }
                if ($null -ne $e.defender.signature_age_hours -and $e.defender.signature_age_hours -isnot [int] -and $e.defender.signature_age_hours -isnot [long] -and $e.defender.signature_age_hours -isnot [double] -and $e.defender.signature_age_hours -isnot [decimal]) { $schemaProblems += 'defender.signature_age_hours is not a number or null' }
            }
            if ($null -eq $e.installer -or $e.installer -isnot [pscustomobject]) { $schemaProblems += 'installer is not an object' }
            else {
                if ($null -ne $e.installer.signature_status -and $e.installer.signature_status -isnot [string]) { $schemaProblems += 'installer.signature_status is not a string or null' }
                if ($null -ne $e.installer.publisher -and $e.installer.publisher -isnot [string]) { $schemaProblems += 'installer.publisher is not a string or null' }
                if ($null -ne $e.installer.timestamped -and $e.installer.timestamped -isnot [bool]) { $schemaProblems += 'installer.timestamped is not a boolean or null' }
            }
            $testKeys = @('installed', 'launched', 'backend_healthy', 'first_run_completed', 'uninstall_preserved_data')
            if ($null -eq $e.tests -or $e.tests -isnot [pscustomobject]) { $schemaProblems += 'tests is not an object' }
            else {
                foreach ($key in $testKeys) {
                    if ($e.tests.PSObject.Properties[$key] -eq $null) { $schemaProblems += "tests.$key is missing" }
                    elseif ($e.tests.$key -isnot [bool]) { $schemaProblems += "tests.$key is not a boolean" }
                }
                foreach ($property in $e.tests.PSObject.Properties) {
                    if ($testKeys -notcontains $property.Name) { $schemaProblems += "tests has unexpected '$($property.Name)'" }
                }
            }
            Add-Gate 'evidence.schema' ($schemaProblems.Count -eq 0) $(if ($schemaProblems.Count -eq 0) { 'matches docs\evidence\SCHEMA.md' } else { $schemaProblems -join '; ' })
            Add-Gate 'clean.release' ($e.release -eq $ReleaseVersion) "evidence release: $($e.release)"
            Add-Gate 'clean.artifact-hash' ($e.artifact_sha256 -and $e.artifact_sha256.ToLowerInvariant() -eq $artifactHash) "evidence hash matches the artifact"
            Add-Gate 'clean.tester' ([bool]$e.tester) "tester: $($e.tester)"
            Add-Gate 'clean.windows-11' ([int]$e.windows.build -ge 22000) "Windows build $($e.windows.build)"
            Add-Gate 'clean.clean-install' ([bool]$e.windows.clean_install) "clean installation: $($e.windows.clean_install)"
            Add-Gate 'clean.sac-enforced' ([int]$e.sac.state -eq 1) "Smart App Control state: $($e.sac.state) (1=enforced)"
            Add-Gate 'clean.defender-realtime' ([bool]$e.defender.realtime_protection) "Defender real-time protection: $($e.defender.realtime_protection)"
            Add-Gate 'clean.defender-antivirus' ([bool]$e.defender.antivirus_enabled) "Defender antivirus: $($e.defender.antivirus_enabled)"
            Add-Gate 'clean.signature' ($e.installer.signature_status -eq 'Valid') "installer signature: $($e.installer.signature_status)"
            Add-Gate 'clean.publisher' ($ExpectedPublisher -and $e.installer.publisher -like "*$ExpectedPublisher*") "installer publisher: $($e.installer.publisher)"
            Add-Gate 'clean.timestamp' ([bool]$e.installer.timestamped) "installer timestamped: $($e.installer.timestamped)"
            Add-Gate 'clean.install' ([bool]$e.tests.installed) "installer ran to completion"
            Add-Gate 'clean.launch' ([bool]$e.tests.launched) "installed app launched"
            Add-Gate 'clean.health' ([bool]$e.tests.backend_healthy) "backend health check passed"
            Add-Gate 'clean.first-run' ([bool]$e.tests.first_run_completed) "first-run setup completed"
            Add-Gate 'clean.data-preserved' ([bool]$e.tests.uninstall_preserved_data) "uninstall preserved user data"
            $dateOk = $false
            try {
                $parsedDate = [datetime]::Parse($e.date, [System.Globalization.CultureInfo]::InvariantCulture)
                $dateOk = $parsedDate -gt (Get-Date).AddDays(-30)
            } catch { }
            Add-Gate 'clean.evidence-date' $dateOk "evidence date within 30 days: $($e.date)"
        } catch {
            Add-Gate 'clean.evidence-json' $false "cannot parse evidence JSON: $($_.Exception.Message)"
        }
    }
}

# --------------------------------------------------------------------------------- tag, CI, publication
if ($Stage -eq 'Release') {
    $tag = "v$ReleaseVersion"
    $tagsAtHead = @(& git -C $repo tag --points-at HEAD)
    Add-Gate 'git.tag' ($tagsAtHead -contains $tag) "HEAD carries tag $tag"

    if ($SkipNetwork) {
        Write-Host '  SKIP  github.*                           -SkipNetwork was requested' -ForegroundColor Yellow
    } else {
        $gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
        if (-not $gh) {
            $knownGh = 'C:\Program Files\GitHub CLI\gh.exe'
            if (Test-Path -LiteralPath $knownGh) { $gh = $knownGh }
        }
        if (-not $gh) {
            Add-Gate 'github.cli' $false 'gh is not installed; cannot verify CI, branch protection or the published asset'
        }
        $remote = if ($gh) { (& git -C $repo remote get-url origin).Trim() } else { '' }
        if ($remote -match 'github\.com[:/]([^/]+)/([^/.]+)') {
            $owner = $Matches[1]
            $repoName = $Matches[2] -replace '\.git$', ''
            $slug = "$owner/$repoName"

            $runJson = & $gh run list --repo $slug --workflow CI --branch main --limit 1 --json conclusion,headSha,status,workflowName 2>$null
            if ($LASTEXITCODE -eq 0 -and $runJson) {
                $run = @($runJson | ConvertFrom-Json)[0]
                Add-Gate 'github.ci' ($run.status -eq 'completed' -and $run.conclusion -eq 'success' -and $run.headSha -eq $head) "latest main CI: $($run.workflowName) $($run.status)/$($run.conclusion) at $($run.headSha)"
            } else {
                Add-Gate 'github.ci' $false 'could not read the latest main CI run'
            }

            $protectionJson = & $gh api "repos/$slug/branches/main/protection" 2>$null
            if ($LASTEXITCODE -eq 0 -and $protectionJson) {
                $protection = $protectionJson | ConvertFrom-Json
                $checkNames = @($protection.required_status_checks.contexts)
                $required = @('Backend tests (Windows)', 'Desktop checks (Windows)')
                $missing = @($required | Where-Object { $checkNames -notcontains $_ })
                Add-Gate 'github.branch-protection' ($missing.Count -eq 0) $(if ($missing.Count -eq 0) { 'required checks are enforced' } else { 'missing required checks: ' + ($missing -join ', ') })
            } else {
                Add-Gate 'github.branch-protection' $false 'could not read main branch protection'
            }

            $releaseJson = & $gh release view $tag --repo $slug --json tagName,isDraft,isPrerelease,assets 2>$null
            if ($LASTEXITCODE -eq 0 -and $releaseJson) {
                $release = $releaseJson | ConvertFrom-Json
                $asset = @($release.assets | Where-Object { $_.name -eq "CreatorOS-Setup-$ReleaseVersion.exe" }) | Select-Object -First 1
                $digest = if ($asset.digest) { $asset.digest -replace '^sha256:', '' } else { '' }
                $artifactHash = if ($artifactExists) { (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant() } else { '' }
                Add-Gate 'github.release' ($release.tagName -eq $tag -and -not $release.isDraft -and -not $release.isPrerelease) "release $tag published (draft=$($release.isDraft), prerelease=$($release.isPrerelease))"
                Add-Gate 'github.release-asset' ([bool]$asset -and $digest -eq $artifactHash) "published installer digest matches the local artifact"
            } else {
                Add-Gate 'github.release' $false "no published GitHub release $tag"
            }
        } else {
            Add-Gate 'github.remote' $false "origin is not a GitHub repository: $remote"
        }
    }
}

Write-Host ''
Write-Host ("Result: {0} passed, {1} failed" -f $passes.Count, $failures.Count) -ForegroundColor $(if ($failures.Count -eq 0) { 'Green' } else { 'Red' })
if ($failures.Count -gt 0) {
    Write-Host 'RELEASE GATE: NOT READY' -ForegroundColor Red
    Write-Host ''
    foreach ($failure in $failures) { Write-Host "  - $failure" -ForegroundColor Red }
    exit 1
}

Write-Host 'RELEASE GATE: PASSED' -ForegroundColor Green
exit 0
