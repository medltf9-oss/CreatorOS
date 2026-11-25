# CreatorOS release gate.
#
# Checks the mandatory gates for a Windows release and fails closed. It does not build, sign or
# publish anything. Run it before tagging, after signing, and again before publishing.
#
#   powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Source
#   powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Artifact -ExpectedPublisher "Jane Doe"
#   powershell -ExecutionPolicy Bypass -File scripts\release-gate.ps1 -Stage Release -ExpectedPublisher "Jane Doe" -EvidencePath docs\evidence\v1.0.0-clean-sac.json
#
# Stage Source    version, documentation, SBOM, tracked-secret scan, clean git tree
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
$releaseDir = Join-Path $repo "releases\$ReleaseVersion"
$sbomCandidates = @(
    (Join-Path $releaseDir 'sbom.cdx.json'),
    (Join-Path $releaseDir 'sbom.spdx.json'),
    (Join-Path $repo "docs\release\sbom-$ReleaseVersion.cdx.json")
)
$sbomPath = $sbomCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$sbomOk = $false
$sbomDetail = 'no SBOM found'
if ($sbomPath) {
    try {
        $sbom = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json
        $sbomOk = $true
        $sbomDetail = "SBOM: $sbomPath"
    } catch {
        $sbomDetail = "SBOM is not valid JSON: $sbomPath"
    }
}
Add-Gate 'release.sbom' $sbomOk $sbomDetail

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
        $hash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $name = $item.Name
        $manifestPath = Join-Path $releaseDir 'SHA256SUMS.txt'
        $manifestText = if (Test-Path -LiteralPath $manifestPath) { Get-Content -LiteralPath $manifestPath -Raw } else { '' }
        $manifestOk = $manifestText -match ('(?im)^' + [regex]::Escape($hash) + '\s+' + [regex]::Escape($name) + '\b')
        Add-Gate 'artifact.manifest' $manifestOk "SHA256SUMS.txt records $hash for $name"

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
