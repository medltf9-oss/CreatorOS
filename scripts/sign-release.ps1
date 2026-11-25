# CreatorOS release signing.
#
# The only thing standing between this project and a distributable installer is a code-signing
# certificate. Smart App Control is ON on the development machine, so a *newly built unsigned installer*
# is refused at launch with CodeIntegrity 3077 - that is a Windows policy decision, and this script never
# changes a Windows security setting to work around it.
#
#   powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Check
#       Report only: is there a certificate, is signtool present, is the timestamp server reachable,
#       what do the current artefacts look like. Changes nothing. Always exits 0.
#
#   powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Sign
#       Sign the installer (and the uninstaller electron-builder produced), then verify signature and
#       chain. Reads $env:CSC_LINK and $env:CSC_KEY_PASSWORD. Never prints, copies or commits either.
#
#   powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -Verify -Path <file>
#       Verify an existing signed artefact: signature status, signer subject, chain, timestamp.
#
#   powershell -ExecutionPolicy Bypass -File scripts\sign-release.ps1 -SelfTest
#       Prove the signing and verification code path works using a throwaway self-signed certificate in
#       CurrentUser\My, on a COPY of a real artefact under %TEMP%, then delete it. This proves the
#       machinery, NOT that Smart App Control will accept a self-signed binary - it will not.
#
# What is never done here:
#   * CreatorOS.exe (the packaged app) is left alone by default: it is byte-identical to the Electron
#     vendor binary and carries Electron's Authenticode signature. Re-signing it would replace that
#     signature, which is what lets the installed app start at all. Pass -IncludeAppExe if you have a
#     CA-issued certificate and have decided that trade is worth it.
#   * no certificate, password or .pfx is copied into the repository, and the script refuses to run when
#     CSC_LINK points inside it.
#   * no trust store, policy or registry value is modified.

param(
    [switch]$Check,
    [switch]$Sign,
    [switch]$Verify,
    [switch]$SelfTest,
    [switch]$IncludeAppExe,
    [string]$Path = '',
    [string]$TimestampUrl = 'http://timestamp.digicert.com',
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $Version) {
    $package = Get-Content (Join-Path $repo 'app\desktop\package.json') -Raw | ConvertFrom-Json
    $Version = $package.version
}
$releaseDir = Join-Path $repo "releases\$Version"

# --------------------------------------------------------------------------------- observations
function Find-SignTool {
    $candidates = @()
    $kits = 'C:\Program Files (x86)\Windows Kits\10\bin'
    if (Test-Path $kits) {
        $candidates += Get-ChildItem $kits -Directory |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'x64\signtool.exe' }
    }
    $candidates += @(
        (Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\App Certification Kit\signtool.exe'),
        (Join-Path $env:ProgramFiles 'Windows Kits\10\App Certification Kit\signtool.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

function Get-SmartAppControlState {
    # Read only. This script never writes this key, and neither should anyone else: the policy is the
    # user's security decision, not a build setting.
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy'
    try {
        $value = (Get-ItemProperty -Path $key -Name 'VerifiedAndReputablePolicyState' -ErrorAction Stop).VerifiedAndReputablePolicyState
    } catch { return 'unknown (key not readable)' }
    switch ($value) {
        0 { return 'off' }
        1 { return 'enforced (ON)' }
        2 { return 'evaluation mode' }
        default { return "value $value" }
    }
}

function Get-CscLinkKind {
    $link = $env:CSC_LINK
    if (-not $link) { return 'none' }
    if (Test-Path $link) {
        $resolved = (Resolve-Path $link).Path
        if ($resolved -like (Join-Path $repo '*')) {
            return "refused: the certificate file lives inside the repository ($resolved)"
        }
        return "file: $resolved"
    }
    if ($link -match '^[A-Za-z]:\\' -or $link -match '^\\\\') {
        return "refused: CSC_LINK points at $link, which does not exist"
    }
    if ($link.Length -gt 2000) { return 'inline base64 blob (not printed)' }
    $thumb = $link -replace '[^0-9A-Fa-f]', ''
    if ($thumb.Length -eq 40) {
        $found = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
            Where-Object { $_.Thumbprint -eq $thumb }
        if ($found) { return "store thumbprint: $($found[0].Subject)" }
        return 'store thumbprint given, but no matching certificate in CurrentUser\My or LocalMachine\My'
    }
    return 'unrecognised value (not printed)'
}

function Test-TimestampServer {
    if (-not $TimestampUrl) { return 'disabled' }
    try {
        $uri = [Uri]$TimestampUrl
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync($uri.Host, 80)
        if (-not $task.Wait(4000)) { $client.Close(); return "unreachable (>4s): $TimestampUrl" }
        $client.Close()
        return "reachable: $TimestampUrl"
    } catch { return "unreachable: $TimestampUrl ($($_.Exception.Message))" }
}

function Get-SignatureReport {
    param([string]$File)
    $signature = Get-AuthenticodeSignature -FilePath $File
    $signer = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { $null }
    $chain = 'n/a'
    if ($signature.SignerCertificate) {
        try {
            $x509 = New-Object System.Security.Cryptography.X509Certificates.X509Chain
            $x509.ChainPolicy.RevocationMode = 'NoCheck'
            $built = $x509.Build($signature.SignerCertificate)
            $chain = if ($built) { 'valid' } else { ($x509.ChainStatus | ForEach-Object { $_.Status }) -join ',' }
        } catch { $chain = "unavailable ($($_.Exception.Message))" }
    }
    [pscustomobject]@{
        File       = (Split-Path -Leaf $File)
        Status     = [string]$signature.Status
        Signer     = $signer
        Chain      = $chain
        Timestamp  = if ($signature.TimeStamperCertificate) { 'timestamped' } else { 'no timestamp' }
    }
}

# ----------------------------------------------------------------------------------------- -Check
if ($Check) {
    Write-Host 'CreatorOS code-signing status' -ForegroundColor Cyan
    Write-Host ''
    Write-Host "  Smart App Control : $(Get-SmartAppControlState)"
    Write-Host "  signtool          : $(if ($tool = Find-SignTool) { $tool } else { 'NOT FOUND (install the Windows SDK signing tools)' })"
    Write-Host "  CSC_LINK          : $(Get-CscLinkKind)"
    Write-Host "  CSC_KEY_PASSWORD  : $(if ($env:CSC_KEY_PASSWORD) { "set ($($env:CSC_KEY_PASSWORD.Length) characters, not printed)" } else { 'not set' })"
    Write-Host "  timestamp server  : $(Test-TimestampServer)"
    Write-Host ''
    $artefacts = @()
    if (Test-Path $releaseDir) {
        $artefacts += Get-ChildItem $releaseDir -File -Filter '*.exe'
        $appExe = Join-Path $releaseDir 'win-unpacked\CreatorOS.exe'
        if (Test-Path $appExe) { $artefacts += Get-Item $appExe }
    }
    if ($artefacts.Count -eq 0) {
        Write-Host '  no artefacts: build with scripts\build-release.ps1 first' -ForegroundColor Yellow
    } else {
        Write-Host '  current signatures:' -ForegroundColor Cyan
        foreach ($artefact in $artefacts) {
            $report = Get-SignatureReport -File $artefact.FullName
            $colour = if ($report.Status -eq 'Valid') { 'Green' } elseif ($report.Status -eq 'NotSigned') { 'Yellow' } else { 'Red' }
            Write-Host ("    {0,-32} {1,-12} {2,-14} {3}" -f $report.File, $report.Status, $report.Timestamp, $report.Chain) -ForegroundColor $colour
            if ($report.Signer) { Write-Host "      signer: $($report.Signer)" }
            if ($report.File -eq 'CreatorOS.exe' -and $report.Status -eq 'NotSigned') {
                Write-Host '      note: the packaged app is byte-identical to the Electron npm binary, which is unsigned' -ForegroundColor Yellow
                Write-Host '            (nothing is overwritten by signing it: see docs\CODE_SIGNING.md)' -ForegroundColor Yellow
            }
        }
    }
    Write-Host ''
    Write-Host '  What signing does and does not buy: see docs\CODE_SIGNING.md' -ForegroundColor Cyan
    exit 0
}

# ------------------------------------------------------------------------------------------ -Verify
if ($Verify) {
    $targets = if ($Path) { @(Get-Item $Path) } else { @(Get-ChildItem $releaseDir -File -Filter '*.exe') }
    if ($targets.Count -eq 0) { Write-Host 'nothing to verify' -ForegroundColor Yellow; exit 1 }
    $bad = 0
    foreach ($target in $targets) {
        $report = Get-SignatureReport -File $target.FullName
        Write-Host ("{0}: {1}" -f $report.File, $report.Status)
        if ($report.Signer) { Write-Host "  signer  : $($report.Signer)" }
        if ($report.Status -ne 'Valid') { $bad++ }
        Write-Host "  chain   : $($report.Chain); $($report.Timestamp)"
    }
    if ($bad -gt 0) {
        Write-Host "`n$bad artefact(s) are not validly signed" -ForegroundColor Red
        exit 1
    }
    Write-Host "`nall artefacts are validly signed" -ForegroundColor Green
    exit 0
}

# -------------------------------------------------------------------------------------------- -Sign
if ($Sign) {
    $tool = Find-SignTool
    if (-not $tool) { throw 'signtool.exe was not found. Install the Windows SDK signing tools, or keep the unsigned installer and document it.' }
    $link = $env:CSC_LINK
    if (-not $link) { throw 'CSC_LINK is not set: there is no certificate to sign with. Use -Check to see the current state.' }
    if ($link -match '^[A-Za-z]:\\' -and (Test-Path $link)) {
        $resolved = (Resolve-Path $link).Path
        if ($resolved -like (Join-Path $repo '*')) {
            throw "refusing to sign with a certificate that lives inside the repository ($resolved): move it outside the repository first"
        }
    }

    $targets = @()
    $setup = Join-Path $releaseDir "CreatorOS-Setup-$Version.exe"
    if (Test-Path $setup) { $targets += $setup }
    # NSIS writes its uninstaller into the packaged tree; signing it means the uninstall step is also
    # attributable to the same publisher.
    foreach ($uninstaller in (Get-ChildItem $releaseDir -Recurse -File -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue)) {
        $targets += $uninstaller.FullName
    }
    $appExe = Join-Path $releaseDir 'win-unpacked\CreatorOS.exe'
    if (Test-Path $appExe) {
        $existing = Get-AuthenticodeSignature -FilePath $appExe
        if ($existing.Status -eq 'Valid' -and -not $IncludeAppExe) {
            # electron.exe from npm is in fact unsigned (see docs/CODE_SIGNING.md), but if a future
            # vendor binary is signed, replacing that signature is a decision, not a default.
            Write-Host "skipping CreatorOS.exe: it already carries a valid signature ($($existing.SignerCertificate.Subject))" -ForegroundColor Yellow
            Write-Host '  pass -IncludeAppExe to sign it with your certificate instead' -ForegroundColor Yellow
        } else {
            $targets += $appExe
            Write-Host "including CreatorOS.exe (its current signature: $($existing.Status))" -ForegroundColor Cyan
        }
    }
    if ($targets.Count -eq 0) { throw "no artefacts to sign under ${releaseDir}: build first" }

    $signArgs = @('sign', '/fd', 'sha256', '/a')
    if (Test-Path $link) {
        $signArgs += @('/f', (Resolve-Path $link).Path)
        if ($env:CSC_KEY_PASSWORD) { $signArgs += @('/p', $env:CSC_KEY_PASSWORD) }
    } else {
        # A store thumbprint: signtool selects it by /sha1.
        $signArgs += @('/sha1', ($link -replace '[^0-9A-Fa-f]', ''))
    }
    if ($TimestampUrl) { $signArgs += @('/tr', $TimestampUrl, '/td', 'sha256') }

    foreach ($target in $targets) {
        Write-Host "signing $(Split-Path -Leaf $target)"
        # The password is passed in the argument array of a native call: it is never echoed, logged or
        # written to a file by this script. Windows itself may show it in a process listing during the
        # call, which is the documented limitation of signtool's /p flag.
        & $tool @signArgs $target
        if ($LASTEXITCODE -ne 0) { throw "signtool failed for $target (exit $LASTEXITCODE)" }
    }

    Write-Host ''
    $bad = 0
    foreach ($target in $targets) {
        $report = Get-SignatureReport -File $target
        $colour = if ($report.Status -eq 'Valid') { 'Green' } else { 'Red' }
        Write-Host ("{0}: {1} ({2}, {3})" -f $report.File, $report.Status, $report.Chain, $report.Timestamp) -ForegroundColor $colour
        if ($report.Status -ne 'Valid') { $bad++ }
    }
    if ($bad -gt 0) { throw "$bad artefact(s) did not verify after signing" }

    Write-Host ''
    Write-Host 'Signed. The SHA256SUMS.txt manifest is now stale for those files: rebuild it with' -ForegroundColor Yellow
    Write-Host '  powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1  (or re-hash by hand)' -ForegroundColor Yellow
    Write-Host 'A valid signature does NOT guarantee Smart App Control acceptance: test the installer.' -ForegroundColor Yellow
    exit 0
}

# ----------------------------------------------------------------------------------------- -SelfTest
if ($SelfTest) {
    $tool = Find-SignTool
    if (-not $tool) { throw 'signtool.exe was not found; the self-test signs a real portable executable' }
    $source = Join-Path $releaseDir "CreatorOS-Setup-$Version.exe"
    if (-not (Test-Path $source)) {
        # Fall back to any signed portable executable on this machine, so the test does not depend on a
        # release build having been made in this session.
        $source = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    }
    Write-Host "self-test: signing a COPY of $source" -ForegroundColor Cyan

    $work = Join-Path $env:TEMP ("creatoros-signtest-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    $cert = $null
    try {
        $password = [Guid]::NewGuid().ToString('N') + 'Aa1!'
        $secure = ConvertTo-SecureString -String $password -AsPlainText -Force
        $cert = New-SelfSignedCertificate `
            -Subject 'CN=CreatorOS Self-Test (throwaway)' `
            -CertStoreLocation 'Cert:\CurrentUser\My' `
            -Type CodeSigningCert `
            -KeyUsage DigitalSignature `
            -KeyExportPolicy Exportable `
            -NotAfter (Get-Date).AddDays(1)
        Write-Host "  created throwaway certificate $($cert.Thumbprint)"

        $pfx = Join-Path $work 'selftest.pfx'
        Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $secure | Out-Null

        $copy = Join-Path $work ("copy-" + (Split-Path -Leaf $source))
        Copy-Item $source $copy -Force
        $before = Get-SignatureReport -File $copy

        & $tool sign /fd sha256 /f $pfx /p $password /tr $TimestampUrl /td sha256 $copy | Out-Null
        if ($LASTEXITCODE -ne 0) {
            # A self-signed certificate with an unreachable timestamp server still proves the local path.
            Write-Host '  timestamping failed; retrying without a timestamp' -ForegroundColor Yellow
            & $tool sign /fd sha256 /f $pfx /p $password $copy | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'signtool could not sign the copy at all' }
        }
        $after = Get-SignatureReport -File $copy
        Write-Host "  before: $($before.Status) / $($before.Timestamp)"
        Write-Host "  after : $($after.Status) / $($after.Timestamp) - signer $($after.Signer)"

        if (-not $after.Signer) { throw 'the copy has no signer certificate after signing' }
        if ($after.Signer -notlike '*CreatorOS Self-Test*') { throw "unexpected signer: $($after.Signer)" }
        $afterChain = $after.Chain
        Write-Host "  chain : $afterChain"
        if ($afterChain -eq 'valid') {
            Write-Host '  note  : this machine already trusts the throwaway certificate somehow; expected UntrustedRoot' -ForegroundColor Yellow
        } elseif ($afterChain -notmatch 'UntrustedRoot') {
            throw "unexpected chain result for a self-signed certificate: $afterChain"
        } else {
            Write-Host '  chain : UntrustedRoot, which is exactly what a self-signed certificate should produce' -ForegroundColor Green
        }

        # Change one byte in the middle of the image: verification must notice. The Authenticode hash
        # excludes the checksum field and the appended signature blob, so tampering near the end of the
        # file can silently pass - which is exactly why the test tampers with the image body.
        $bytes = [System.IO.File]::ReadAllBytes($copy)
        $middle = [int]($bytes.Length / 2)
        $bytes[$middle] = [byte]($bytes[$middle] -bxor 0xFF)
        $tampered = Join-Path $work 'tampered.exe'
        [System.IO.File]::WriteAllBytes($tampered, $bytes)
        $tamperReport = Get-SignatureReport -File $tampered
        Write-Host "  tampered copy: $($tamperReport.Status)"
        if ($tamperReport.Status -eq 'Valid') { throw 'a tampered copy still verified as Valid: verification is not working' }
        if ($tamperReport.Status -ne 'HashMismatch') {
            Write-Host "  note  : tampering produced $($tamperReport.Status) rather than HashMismatch" -ForegroundColor Yellow
        }

        Write-Host ''
        Write-Host 'SELF-TEST PASSED: signing, verification, chain building and tamper detection all work.' -ForegroundColor Green
        Write-Host 'This does NOT mean Smart App Control accepts a self-signed installer - it does not.' -ForegroundColor Yellow
        Write-Host 'Only a CA-issued certificate, and an installation test, can establish that.' -ForegroundColor Yellow
        exit 0
    } finally {
        if ($cert) { Remove-Item -Path ("Cert:\CurrentUser\My\" + $cert.Thumbprint) -Force -ErrorAction SilentlyContinue }
        Remove-Item -Path $work -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  cleaned up: certificate removed from CurrentUser\My, temporary files deleted"
    }
}

Write-Host 'Nothing to do: pass -Check, -Sign, -Verify or -SelfTest.' -ForegroundColor Yellow
exit 2
