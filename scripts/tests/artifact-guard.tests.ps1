<#
.SYNOPSIS
    Dependency-free self-test for scripts\lib\artifact-guard.ps1 (Pester is not required).

.DESCRIPTION
    Creates throwaway artifacts and SHA256SUMS.txt files under %TEMP% and proves the guard's contract:
    a valid hash with an allowed unsigned artifact passes, tampered bytes are refused, a missing sums
    file or entry is refused, a duplicate entry is refused, -RequireSignature refuses an unsigned
    artifact, and unsigned artifacts are refused unless -AllowUnsigned is passed.

    The unsigned fixture is a minimal but structurally valid PE image with no certificate table, so
    Get-AuthenticodeSignature genuinely reports NotSigned. No signature is ever fabricated: the signed
    case copies a real signed file that already exists on the machine, and is reported NOT RUN when no
    such file is found.

    Exit code 1 when any case fails, 0 otherwise.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\tests\artifact-guard.tests.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptsDir = Split-Path -Parent $testsDir
$guardPath = Join-Path $scriptsDir 'lib\artifact-guard.ps1'
if (-not (Test-Path -LiteralPath $guardPath)) {
    Write-Host ("  [FAIL] guard library - not found: {0}" -f $guardPath) -ForegroundColor Red
    Write-Host 'artifact guard self-test: 0 passed, 1 failed, 0 NOT RUN'
    exit 1
}
. $guardPath

$script:passed = 0
$script:failed = 0
$script:notRun = 0
$script:failures = @()
$script:tempRoot = Join-Path $env:TEMP ('creatoros-artifact-guard-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))

function Pass([string]$Name, [string]$Detail) {
    $script:passed = $script:passed + 1
    Write-Host ("  [PASS] {0} - {1}" -f $Name, $Detail) -ForegroundColor Green
}
function Fail([string]$Name, [string]$Detail) {
    $script:failed = $script:failed + 1
    $script:failures += ("{0}: {1}" -f $Name, $Detail)
    Write-Host ("  [FAIL] {0} - {1}" -f $Name, $Detail) -ForegroundColor Red
}
function NotRun([string]$Name, [string]$Detail) {
    $script:notRun = $script:notRun + 1
    Write-Host ("  [NOT RUN] {0} - {1}" -f $Name, $Detail) -ForegroundColor Yellow
}
function New-CaseDir([string]$Label) {
    $dir = Join-Path $script:tempRoot $Label
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    return $dir
}
function Get-Sha256([string]$FilePath) {
    return (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Set-Sums([string]$Dir, [string[]]$Lines) {
    Set-Content -Path (Join-Path $Dir 'SHA256SUMS.txt') -Value $Lines -Encoding ascii
}
function Invoke-PassCase([string]$Name, [scriptblock]$Action) {
    try {
        $result = & $Action
        Pass $Name 'accepted as expected'
        return $result
    } catch {
        Fail $Name ('expected a pass, got: ' + $_.Exception.Message)
        return $null
    }
}
function Invoke-RefusalCase([string]$Name, [string]$Pattern, [scriptblock]$Action) {
    try {
        & $Action | Out-Null
    } catch {
        $message = $_.Exception.Message
        if ($message -match $Pattern) {
            Pass $Name ('refused: ' + $message)
        } else {
            Fail $Name ('refused with a message that does not match the contract: ' + $message)
        }
        return
    }
    Fail $Name 'the guard accepted the artifact instead of refusing it'
}

# A minimal PE32+ image (DOS header, PE signature, COFF header, optional header, one section) with an
# all-zero certificate data directory. Windows therefore reports Status NotSigned for these bytes
# without anyone fabricating a signature. The fixture is never executed.
function New-UnsignedPe([string]$FilePath) {
    $bytes = New-Object byte[] 1024
    $bytes[0] = 0x4D
    $bytes[1] = 0x5A
    [BitConverter]::GetBytes([int]64).CopyTo($bytes, 0x3C)
    $bytes[0x40] = 0x50
    $bytes[0x41] = 0x45
    [BitConverter]::GetBytes([uint16]0x8664).CopyTo($bytes, 0x44)
    [BitConverter]::GetBytes([uint16]1).CopyTo($bytes, 0x46)
    [BitConverter]::GetBytes([uint16]0xF0).CopyTo($bytes, 0x54)
    [BitConverter]::GetBytes([uint16]0x22).CopyTo($bytes, 0x56)
    $optional = 0x58
    [BitConverter]::GetBytes([uint16]0x20B).CopyTo($bytes, $optional)
    [BitConverter]::GetBytes([uint32]0x200).CopyTo($bytes, $optional + 4)
    [BitConverter]::GetBytes([uint32]0x1000).CopyTo($bytes, $optional + 16)
    [BitConverter]::GetBytes([uint32]0x1000).CopyTo($bytes, $optional + 20)
    [BitConverter]::GetBytes([uint64]0x140000000).CopyTo($bytes, $optional + 24)
    [BitConverter]::GetBytes([uint32]0x1000).CopyTo($bytes, $optional + 32)
    [BitConverter]::GetBytes([uint32]0x200).CopyTo($bytes, $optional + 36)
    [BitConverter]::GetBytes([uint16]6).CopyTo($bytes, $optional + 40)
    [BitConverter]::GetBytes([uint16]0).CopyTo($bytes, $optional + 42)
    [BitConverter]::GetBytes([uint16]6).CopyTo($bytes, $optional + 48)
    [BitConverter]::GetBytes([uint16]0).CopyTo($bytes, $optional + 50)
    [BitConverter]::GetBytes([uint32]0x2000).CopyTo($bytes, $optional + 56)
    [BitConverter]::GetBytes([uint32]0x200).CopyTo($bytes, $optional + 60)
    [BitConverter]::GetBytes([uint16]3).CopyTo($bytes, $optional + 68)
    [BitConverter]::GetBytes([uint64]0x100000).CopyTo($bytes, $optional + 72)
    [BitConverter]::GetBytes([uint64]0x1000).CopyTo($bytes, $optional + 80)
    [BitConverter]::GetBytes([uint64]0x100000).CopyTo($bytes, $optional + 88)
    [BitConverter]::GetBytes([uint64]0x1000).CopyTo($bytes, $optional + 96)
    [BitConverter]::GetBytes([uint32]0).CopyTo($bytes, $optional + 104)
    [BitConverter]::GetBytes([uint32]16).CopyTo($bytes, $optional + 108)
    $section = $optional + 0xF0
    $sectionName = [System.Text.Encoding]::ASCII.GetBytes('.text')
    $sectionName.CopyTo($bytes, $section)
    [BitConverter]::GetBytes([uint32]1).CopyTo($bytes, $section + 8)
    [BitConverter]::GetBytes([uint32]0x1000).CopyTo($bytes, $section + 12)
    [BitConverter]::GetBytes([uint32]0x200).CopyTo($bytes, $section + 16)
    [BitConverter]::GetBytes([uint32]0x200).CopyTo($bytes, $section + 20)
    [BitConverter]::GetBytes([uint32]0x60000020).CopyTo($bytes, $section + 36)
    $bytes[0x200] = 0xC3
    [System.IO.File]::WriteAllBytes($FilePath, $bytes)
}

Write-Host ''
Write-Host '==> artifact guard self-test' -ForegroundColor Cyan
Write-Host ("    guard: {0}" -f $guardPath)
Write-Host ("    temp:  {0}" -f $script:tempRoot)
New-Item -ItemType Directory -Path $script:tempRoot -Force | Out-Null

try {
    # 1. valid hash, unsigned artifact accepted with -AllowUnsigned; annotation and comments ignored.
    $case = New-CaseDir 'valid-unsigned'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    New-UnsignedPe $artifact
    Set-Sums $case @('# built by the artifact guard self-test', '', ("{0}  {1}  (0.0 MB)" -f (Get-Sha256 $artifact), (Split-Path -Leaf $artifact)))
    Invoke-PassCase 'valid hash + unsigned allowed passes' {
        $result = Assert-ArtifactVerified -Path $artifact -AllowUnsigned
        if (-not $result.Unsigned) { throw 'the guard did not report the artifact as unsigned' }
        if ($result.Signed) { throw 'the guard reported the unsigned artifact as signed' }
        if ($result.Sha256 -ne (Get-Sha256 $artifact)) { throw 'the reported hash does not match Get-FileHash' }
        $result
    } | Out-Null

    # 2. GNU binary marker plus an explicit -ChecksumFile outside the artifact directory.
    $case = New-CaseDir 'binary-marker'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    New-UnsignedPe $artifact
    $sumsCase = New-CaseDir 'binary-marker-sums'
    $sums = Join-Path $sumsCase 'SHA256SUMS.txt'
    Set-Content -Path $sums -Value @(("{0} *{1}" -f (Get-Sha256 $artifact), (Split-Path -Leaf $artifact))) -Encoding ascii
    Invoke-PassCase 'binary marker + explicit -ChecksumFile accepted' {
        $result = Assert-ArtifactVerified -Path $artifact -AllowUnsigned -ChecksumFile $sums
        if ($result.ChecksumFile -ne $sums) { throw 'the guard did not record the explicit checksum file' }
        $result
    } | Out-Null

    # 3. tampered bytes: the manifest still describes the original build.
    $case = New-CaseDir 'tampered'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    New-UnsignedPe $artifact
    Set-Sums $case @(("{0}  {1}" -f (Get-Sha256 $artifact), (Split-Path -Leaf $artifact)))
    $bytes = [System.IO.File]::ReadAllBytes($artifact)
    $bytes[$bytes.Length - 1] = $bytes[$bytes.Length - 1] -bxor 0xFF
    [System.IO.File]::WriteAllBytes($artifact, $bytes)
    Invoke-RefusalCase 'tampered bytes refused' 'SHA-256 mismatch for CreatorOS-Setup-9\.9\.9\.exe' {
        Assert-ArtifactVerified -Path $artifact -AllowUnsigned
    }

    # 4. no SHA256SUMS.txt next to the artifact.
    $case = New-CaseDir 'missing-sums'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    New-UnsignedPe $artifact
    Invoke-RefusalCase 'missing SHA256SUMS refused' 'ARTIFACT GUARD: SHA256SUMS\.txt not found for CreatorOS-Setup-9\.9\.9\.exe' {
        Assert-ArtifactVerified -Path $artifact -AllowUnsigned
    }

    # 5. a sums file that does not mention this artifact.
    $case = New-CaseDir 'missing-entry'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    New-UnsignedPe $artifact
    Set-Sums $case @(('0000000000000000000000000000000000000000000000000000000000000000  someone-else.exe'))
    Invoke-RefusalCase 'missing entry refused' 'no SHA-256 entry for CreatorOS-Setup-9\.9\.9\.exe' {
        Assert-ArtifactVerified -Path $artifact -AllowUnsigned
    }

    # 6. two entries for the same basename: refuse instead of choosing.
    $case = New-CaseDir 'duplicate-entry'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    New-UnsignedPe $artifact
    $hash = Get-Sha256 $artifact
    $name = Split-Path -Leaf $artifact
    Set-Sums $case @(("{0}  {1}" -f $hash, $name), ("{0}  {1}" -f $hash, $name))
    Invoke-RefusalCase 'duplicate entry refused' 'duplicate SHA-256 entries for CreatorOS-Setup-9\.9\.9\.exe' {
        Assert-ArtifactVerified -Path $artifact -AllowUnsigned
    }

    # 7. release evidence mode refuses an unsigned artifact.
    $case = New-CaseDir 'require-signature'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    New-UnsignedPe $artifact
    Set-Sums $case @(("{0}  {1}" -f (Get-Sha256 $artifact), (Split-Path -Leaf $artifact)))
    Invoke-RefusalCase '-RequireSignature on unsigned fails' '-RequireSignature.*is not validly signed.*status=NotSigned' {
        Assert-ArtifactVerified -Path $artifact -RequireSignature
    }

    # 8. the default is safe: unsigned is refused unless -AllowUnsigned is explicit.
    Invoke-RefusalCase 'unsigned without -AllowUnsigned refused' 'is unsigned; pass -AllowUnsigned' {
        Assert-ArtifactVerified -Path $artifact
    }

    # 9. a non-PE file has no usable Authenticode status and is never accepted by -AllowUnsigned.
    $case = New-CaseDir 'not-a-pe'
    $artifact = Join-Path $case 'CreatorOS-Setup-9.9.9.exe'
    Set-Content -Path $artifact -Value 'this is not a portable executable' -Encoding ascii
    Set-Sums $case @(("{0}  {1}" -f (Get-Sha256 $artifact), (Split-Path -Leaf $artifact)))
    Invoke-RefusalCase 'non-PE artifact with -AllowUnsigned refused' 'invalid Authenticode signature|could not read the Authenticode signature' {
        Assert-ArtifactVerified -Path $artifact -AllowUnsigned
    }

    # 10. signed release path: only a real signed, timestamped file is used; never fabricate one.
    $signedCandidates = @(
        (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'),
        (Join-Path $env:WINDIR 'System32\notepad.exe'),
        (Join-Path $env:WINDIR 'System32\cmd.exe')
    )
    $signedSource = $null
    foreach ($candidate in $signedCandidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $candidateSignature = Get-AuthenticodeSignature -LiteralPath $candidate -ErrorAction Stop
        } catch {
            continue
        }
        if ($candidateSignature.Status -eq 'Valid' -and $null -ne $candidateSignature.SignerCertificate -and $null -ne $candidateSignature.TimeStamperCertificate) {
            $signedSource = $candidate
            break
        }
    }
    if (-not $signedSource) {
        NotRun 'signed release path accepted (-RequireSignature)' 'no validly signed, timestamped file exists on this machine'
    } else {
        $case = New-CaseDir 'signed-require-signature'
        $artifact = Join-Path $case 'Signed-Artifact.exe'
        Copy-Item -LiteralPath $signedSource -Destination $artifact -Force
        Set-Sums $case @(("{0}  {1}" -f (Get-Sha256 $artifact), (Split-Path -Leaf $artifact)))
        $signedName = Split-Path -Leaf $signedSource
        Invoke-PassCase ("signed release path accepted (-RequireSignature, real signed source: {0})" -f $signedName) {
            $result = Assert-ArtifactVerified -Path $artifact -RequireSignature
            if (-not $result.Signed) { throw 'the guard did not report the artifact as signed' }
            if (-not $result.Timestamped) { throw 'the guard did not report the timestamp' }
            $result
        } | Out-Null
    }
} finally {
    Remove-Item -LiteralPath $script:tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$total = $script:passed + $script:failed + $script:notRun
Write-Host ''
Write-Host ("artifact guard self-test: {0} passed, {1} failed, {2} NOT RUN ({3} cases)" -f $script:passed, $script:failed, $script:notRun, $total)
if ($script:failed -gt 0) {
    foreach ($failure in $script:failures) { Write-Host ("  - {0}" -f $failure) -ForegroundColor Red }
    exit 1
}
exit 0
