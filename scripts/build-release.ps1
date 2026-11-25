<#
.SYNOPSIS
    Builds the complete CreatorOS release: icons, backend bundle, renderer, installer.

.DESCRIPTION
    One command, in dependency order, with a verification gate after every stage so a broken release
    fails here rather than on the user's machine:

      1. icons            re-render assets/brand/*.svg -> PNG set + build/icon.ico   (optional, -SkipIcons)
      2. backend bundle   signed interpreter + pure-Python payload -> installer_builds\backend
      3. renderer         vite build (typecheck first)
      4. installer        electron-builder -> releases\<version>\CreatorOS-Setup-<version>.exe
      5. signing          when CSC_LINK or -CertificateThumbprint is supplied: sign-release.ps1 signs and
                          verifies the artefacts; -RequireSignature makes an unsigned build fail
      6. manifest         SHA-256 + size of every artefact into releases\<version>\SHA256SUMS.txt

    It does not install. Installing is `installer_builds\verify-install.ps1` (or double-clicking the
    setup exe); keeping the two apart means a failed packaging run never leaves a half-installed app
    on the machine.

.PARAMETER Version
    Overrides the version read from app\desktop\package.json. Must match the backend (see
    app\backend\pyproject.toml) or the build stops.

.PARAMETER SkipIcons
    Reuse the committed icon set instead of re-rendering it (~2 minutes).

.PARAMETER SkipBackend
    Reuse an existing installer_builds\backend (useful when only the renderer changed).

.PARAMETER CertificateThumbprint
    SHA-1 thumbprint of a CA-issued code-signing certificate in the Windows certificate store.
    Alternative to CSC_LINK; the private key stays in the token/HSM.

.PARAMETER RequireSignature
    Fail before building if neither CSC_LINK nor -CertificateThumbprint is supplied. Use this for a
    release build; an unsigned build is only for local testing.
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [switch]$SkipIcons,
    [switch]$SkipBackend,
    [string]$CertificateThumbprint = '',
    [switch]$RequireSignature
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $scriptDir '..')).Path
$desktop = Join-Path $repo 'app\desktop'
$backend = Join-Path $repo 'app\backend'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Assert-Exists($path, $what) {
    if (-not (Test-Path $path)) { throw "$what missing: $path" }
}

# --- versions ------------------------------------------------------------------------------
$desktopVersion = (Get-Content (Join-Path $desktop 'package.json') -Raw | ConvertFrom-Json).version
$backendVersion = ((Select-String -Path (Join-Path $backend 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value)
$settingsVersion = ((Select-String -Path (Join-Path $backend 'src\creatoros\core\settings.py') -Pattern 'version: str = "([^"]+)"').Matches[0].Groups[1].Value)
if ($Version) {
    if ($Version -ne $desktopVersion) { throw "-Version $Version does not match app\desktop\package.json ($desktopVersion)" }
}
Write-Step "release $desktopVersion"
if ($RequireSignature -and -not $env:CSC_LINK -and -not $CertificateThumbprint) {
    throw '-RequireSignature was set, but no CSC_LINK or -CertificateThumbprint is available; refusing to build an unsigned release'
}
if ($backendVersion -ne $desktopVersion -or $settingsVersion -ne $desktopVersion) {
    throw "version mismatch: desktop=$desktopVersion backend/pyproject=$backendVersion backend/settings=$settingsVersion"
}

# --- 1. icons -------------------------------------------------------------------------------
if ($SkipIcons) {
    Write-Step 'icons: reusing the committed set'
    Assert-Exists (Join-Path $desktop 'build\icon.ico') 'icon'
} else {
    Write-Step 'icons: rendering from assets/brand'
    Push-Location $desktop
    try {
        & node 'scripts/electron-run.cjs' 'scripts/make-icons.cjs'
        if ($LASTEXITCODE -ne 0) { throw 'icon rendering failed' }
    } finally { Pop-Location }
}
Assert-Exists (Join-Path $desktop 'build\installerIcon.ico') 'installer icon'

# --- 2. backend bundle ----------------------------------------------------------------------
if ($SkipBackend) {
    Write-Step 'backend: reusing installer_builds\backend'
    Assert-Exists (Join-Path $repo 'installer_builds\backend\python.exe') 'backend bundle'
} else {
    Write-Step 'backend: assembling the interpreter bundle and verifying it'
    & (Join-Path $scriptDir 'build-backend.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'backend bundle build failed' }
}

# --- 3. renderer ----------------------------------------------------------------------------
Write-Step 'renderer: typecheck'
Push-Location $desktop
try {
    & (Join-Path $desktop 'node_modules\.bin\tsc.cmd') --noEmit
    if ($LASTEXITCODE -ne 0) { throw 'typecheck failed' }

    Write-Step 'renderer: unit tests'
    & node --test 'tests/'
    if ($LASTEXITCODE -ne 0) { throw 'frontend tests failed' }

    Write-Step 'renderer: vite build'
    & (Join-Path $desktop 'node_modules\.bin\vite.cmd') build
    if ($LASTEXITCODE -ne 0) { throw 'vite build failed' }

    # --- 4. installer -----------------------------------------------------------------------
    Write-Step 'installer: electron-builder'
    & (Join-Path $desktop 'node_modules\.bin\electron-builder.cmd') --config electron-builder.yml --win --x64
    if ($LASTEXITCODE -ne 0) { throw 'electron-builder failed' }
} finally { Pop-Location }

$releaseDir = Join-Path $repo "releases\$desktopVersion"
$setup = Join-Path $releaseDir "CreatorOS-Setup-$desktopVersion.exe"
Assert-Exists $setup 'installer'
Assert-Exists (Join-Path $releaseDir 'win-unpacked\CreatorOS.exe') 'packaged app'
Assert-Exists (Join-Path $releaseDir 'win-unpacked\resources\backend\python.exe') 'bundled backend'
Assert-Exists (Join-Path $releaseDir 'win-unpacked\resources\icon.ico') 'brand icon resource'

# --- 5. signing (optional) --------------------------------------------------------------------
# No certificate means no signature, and the manifest says so instead of implying one. With a
# certificate, the signature is applied here - before the digests are taken - so SHA256SUMS.txt
# describes the bytes that actually ship.
$signatureLine = "# installer is unsigned (no code-signing certificate); see docs/CODE_SIGNING.md"
if ($env:CSC_LINK -or $CertificateThumbprint) {
    Write-Step 'signing: sign-release.ps1'
    $signArguments = @('-Sign', '-Version', $desktopVersion)
    if ($CertificateThumbprint) { $signArguments += @('-Thumbprint', $CertificateThumbprint) }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repo 'scripts\sign-release.ps1') @signArguments
    if ($LASTEXITCODE -ne 0) { throw 'signing failed' }
    $signatureLine = "# signed and timestamped; verified with signtool and Get-AuthenticodeSignature"
} else {
    Write-Warning 'no certificate supplied: the installer will be unsigned (docs\CODE_SIGNING.md)'
}

# --- 6. manifest ----------------------------------------------------------------------------
Write-Step 'manifest: sizes and digests'
$lines = @()
foreach ($file in (Get-ChildItem $releaseDir -File | Where-Object { $_.Extension -in '.exe', '.blockmap' })) {
    $hash = (Get-FileHash $file.FullName -Algorithm SHA256).Hash.ToLower()
    $mb = [math]::Round($file.Length / 1MB, 1)
    $lines += "$hash  $($file.Name)  ($mb MB)"
    Write-Host "    $($file.Name)  $mb MB  $($hash.Substring(0,16))..."
}
$lines += ''
$lines += "# built $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') on $env:COMPUTERNAME"
$lines += $signatureLine
$lines += "# Smart App Control state on the build machine: $(try { (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop).VerifiedAndReputablePolicyState } catch { 'unknown' }) (0=off, 1=on, 2=evaluation)"
Set-Content -Path (Join-Path $releaseDir 'SHA256SUMS.txt') -Value $lines -Encoding ascii

# --- 7. distribution SBOM --------------------------------------------------------------------
# Generated after the digests so the document binds the installer hash that is actually shipping.
# Without --artifact the SBOM is provenance for a build, not for the released bytes.
Write-Step 'sbom: CycloneDX bound to the installer'
$sbomPython = Join-Path $repo 'appackend.venvScriptspython.exe'
if (-not (Test-Path -LiteralPath $sbomPython)) {
    throw "the backend virtualenv is required to generate the SBOM: $sbomPython"
}
$sbomPath = Join-Path $releaseDir 'sbom.cdx.json'
$prerequisites = Join-Path $repo 'appackendsrccreatorosesourcesprerequisites.v1.json'
& $sbomPython (Join-Path $repo 'scriptsgenerate_sbom.py') --output $sbomPath --repo-root $repo --version $desktopVersion --prerequisites $prerequisites --artifact $setup --check
if ($LASTEXITCODE -ne 0) { throw 'SBOM generation failed' }

Write-Step 'done'
Write-Host "Installer: $setup"
Write-Host "Verify with: powershell -ExecutionPolicy Bypass -File scripts\verify-install.ps1"
