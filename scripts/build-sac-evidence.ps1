# CreatorOS clean-machine evidence assembler (fail-closed).
#
# Turns two read-only captures and the tester's install observations into the JSON evidence
# that scripts\release-gate.ps1 -Stage Release consumes. It does not install, sign or run
# anything: it hashes the artifact, reads its Authenticode signature, copies tests.* from
# -InstallResults, and refuses when any precondition is not observed.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-sac-evidence.ps1 `
#       -ReleaseVersion 1.0.0 -Artifact C:\downloads\CreatorOS-Setup-1.0.0.exe `
#       -Before C:\evidence\before.json -After C:\evidence\after.json `
#       -InstallResults C:\evidence\install-results.json -Tester 'Jane Doe' `
#       -Date 2026-11-25 -Output docs\evidence\v1.0.0-clean-sac.json
#
# Once every input parses, the evidence file is always written so the gate can see the
# honest state; the process exits 1 when any precondition fails, 0 only when all hold.
# Exit 2 when the evidence file cannot be written.
#
# tests.* is copied only from -InstallResults. The value must be a real JSON boolean; a
# string, a number or null is refused, because a guess in that field is a false attestation.

[CmdletBinding()]
param(
    [string]$ReleaseVersion = '',
    [string]$Artifact = '',
    [string]$Before = '',
    [string]$After = '',
    [string]$InstallResults = '',
    [string]$Tester = '',
    [string]$Date = '',
    [string]$Output = '',
    [double]$AllowStaleHours = 24
)

$ErrorActionPreference = 'Stop'
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$inputErrors = New-Object System.Collections.ArrayList
$script:checks = New-Object System.Collections.ArrayList

function Format-Value {
    param($Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) { if ($Value) { return 'true' } else { return 'false' } }
    if ($Value -is [datetimeoffset]) { return $Value.ToString('yyyy-MM-ddTHH:mm:sszzz', $invariant) }
    if ($Value -is [datetime]) { return ([datetimeoffset]$Value).ToString('yyyy-MM-ddTHH:mm:sszzz', $invariant) }
    if ($Value -is [array]) { return (@($Value) -join ',') }
    return [string]$Value
}

function Test-IsTrue {
    param($Value)
    return (($Value -is [bool]) -and ($Value -eq $true))
}

function Get-BooleanOrNull {
    param($Value)
    if ($Value -is [bool]) { return [bool]$Value }
    return $null
}

function ConvertTo-IntegerOrNull {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [int] -or $Value -is [long]) { return [int]$Value }
    if ($Value -is [string] -and $Value.Trim() -match '^-?[0-9]+$') { return [int]$Value.Trim() }
    return $null
}

function ConvertTo-NumberOrNull {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal]) { return [double]$Value }
    if ($Value -is [string] -and $Value.Trim() -match '^[0-9]+([.][0-9]+)?$') { return [double]$Value.Trim() }
    return $null
}

# Reads a dotted path from a parsed JSON document; any missing segment yields null.
function Get-JsonValue {
    param($Object, [string]$Path)
    $current = $Object
    foreach ($segment in $Path.Split('.')) {
        if ($null -eq $current) { return $null }
        $property = $current.PSObject.Properties[$segment]
        if ($null -eq $property) { return $null }
        $current = $property.Value
    }
    return $current
}

# PowerShell 7 turns ISO 8601 strings into DateTime objects while parsing JSON; 5.1 leaves
# them as strings. Both shapes must produce the same instant, including a date with no offset.
function ConvertTo-CaptureTime {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetimeoffset]) { return $Value }
    if ($Value -is [datetime]) { return [datetimeoffset]$Value }
    try { return [datetimeoffset]::Parse([string]$Value, $invariant) } catch { return $null }
}

function Read-JsonInput {
    param([string]$Label, [string]$Path)
    $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
    if ([string]::IsNullOrWhiteSpace($raw)) { throw ($Label + ' is empty: ' + $Path) }
    return ($raw | ConvertFrom-Json -ErrorAction Stop)
}

function Add-Precondition {
    param([string]$Id, [bool]$Ok, [string]$Detail)
    [void]$script:checks.Add([pscustomobject][ordered]@{ id = $Id; ok = [bool]$Ok; detail = $Detail })
}

function Resolve-InputPath {
    param([string]$Flag, [string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        [void]$inputErrors.Add($Flag + ' is required')
        return $null
    }
    try { $resolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path) }
    catch {
        [void]$inputErrors.Add($Flag + ' is not a valid path: ' + $Path)
        return $null
    }
    if (-not (Test-Path -LiteralPath $resolved)) {
        [void]$inputErrors.Add($Flag + ' does not exist: ' + $resolved)
        return $null
    }
    if (Test-Path -LiteralPath $resolved -PathType Container) {
        [void]$inputErrors.Add($Flag + ' is a directory, not a file: ' + $resolved)
        return $null
    }
    return $resolved
}

function Stop-WithInputErrors {
    param([string]$Reason)
    Write-Host ''
    Write-Host ('REFUSED  ' + $Reason) -ForegroundColor Red
    foreach ($message in $inputErrors) { Write-Host ('  ' + $message) -ForegroundColor Red }
    Write-Host 'No evidence file was written.'
    exit 1
}

# --------------------------------------------------------------------------- inputs
if ([string]::IsNullOrWhiteSpace($ReleaseVersion)) {
    [void]$inputErrors.Add('-ReleaseVersion is required')
}
if ([string]::IsNullOrWhiteSpace($Tester)) {
    [void]$inputErrors.Add('-Tester is required (a name or role; no personal address)')
} elseif ($Tester.Trim() -match '^(?i)(tbd|todo|unknown|n/?a|tester|placeholder|<[^>]*>)$') {
    [void]$inputErrors.Add("-Tester is a placeholder value ('" + $Tester + "'); record the actual tester name or role")
}
if ($AllowStaleHours -le 0) {
    [void]$inputErrors.Add('-AllowStaleHours must be greater than zero')
}

$artifactPath = Resolve-InputPath '-Artifact' $Artifact
$beforePath = Resolve-InputPath '-Before' $Before
$afterPath = Resolve-InputPath '-After' $After
$installResultsPath = Resolve-InputPath '-InstallResults' $InstallResults

$outputPath = $null
if ([string]::IsNullOrWhiteSpace($Output)) {
    [void]$inputErrors.Add('-Output is required')
} else {
    try { $outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output) }
    catch { [void]$inputErrors.Add('-Output is not a valid path: ' + $Output) }
    if ($null -ne $outputPath) {
        if (Test-Path -LiteralPath $outputPath -PathType Container) {
            [void]$inputErrors.Add('-Output is a directory: ' + $outputPath)
        }
        $outputDirectory = Split-Path -Parent $outputPath
        if (-not [string]::IsNullOrWhiteSpace($outputDirectory) -and -not (Test-Path -LiteralPath $outputDirectory)) {
            [void]$inputErrors.Add('-Output directory does not exist: ' + $outputDirectory)
        }
    }
}

$dateWasDefaulted = $false
if ([string]::IsNullOrWhiteSpace($Date)) {
    $Date = ([datetimeoffset](Get-Date)).ToString('yyyy-MM-ddTHH:mm:sszzz', $invariant)
    $dateWasDefaulted = $true
}
$parsedDate = $null
try { $parsedDate = [datetime]::Parse($Date.Trim(), $invariant) }
catch { [void]$inputErrors.Add('-Date is not a parseable date (yyyy-MM-dd or ISO 8601): ' + $Date) }

if ($inputErrors.Count -gt 0) {
    Stop-WithInputErrors 'required input is missing or unusable.'
}

$beforeDocument = $null
$afterDocument = $null
$installResultsDocument = $null
$parseProblems = New-Object System.Collections.ArrayList
try { $beforeDocument = Read-JsonInput '-Before' $beforePath }
catch { [void]$parseProblems.Add('-Before cannot be read as JSON: ' + $_.Exception.Message) }
try { $afterDocument = Read-JsonInput '-After' $afterPath }
catch { [void]$parseProblems.Add('-After cannot be read as JSON: ' + $_.Exception.Message) }
try { $installResultsDocument = Read-JsonInput '-InstallResults' $installResultsPath }
catch { [void]$parseProblems.Add('-InstallResults cannot be read as JSON: ' + $_.Exception.Message) }

foreach ($pair in @(
    [pscustomobject]@{ label = '-Before'; document = $beforeDocument },
    [pscustomobject]@{ label = '-After'; document = $afterDocument }
)) {
    if ($null -eq $pair.document) { continue }
    $version = Get-JsonValue $pair.document 'capture_version'
    if ($null -eq $version) {
        [void]$parseProblems.Add($pair.label + ' is not a capture from scripts\capture-security-state.ps1: capture_version is missing')
    }
}
if ($parseProblems.Count -gt 0) {
    foreach ($problem in $parseProblems) { [void]$inputErrors.Add($problem) }
    Stop-WithInputErrors 'an input file is unreadable or is not a capture.'
}

# --------------------------------------------------------------------------- install results
# The five observations are the tester's, not this script's. They are copied only when every
# value is a real JSON boolean. A missing key, a string such as "true" or a null is refused.
$testKeys = @('installed', 'launched', 'backend_healthy', 'first_run_completed', 'uninstall_preserved_data')
$tests = [ordered]@{
    installed = $null
    launched = $null
    backend_healthy = $null
    first_run_completed = $null
    uninstall_preserved_data = $null
}
$testsProblem = $null
foreach ($key in $testKeys) {
    $property = $null
    if ($null -ne $installResultsDocument) { $property = $installResultsDocument.PSObject.Properties[$key] }
    if ($null -eq $property) {
        $testsProblem = "install results is missing the key '" + $key + "'"
        break
    }
    if ($property.Value -isnot [bool]) {
        $observedType = 'null'
        if ($null -ne $property.Value) { $observedType = $property.Value.GetType().Name + ' (' + [string]$property.Value + ')' }
        $testsProblem = "install results key '" + $key + "' must be a real boolean, found " + $observedType
        break
    }
    $tests[$key] = [bool]$property.Value
}
if ($null -ne $testsProblem) {
    [void]$inputErrors.Add($testsProblem)
    Stop-WithInputErrors 'the install results file is not well-formed.'
}

# --------------------------------------------------------------------------- artifact
$artifactHash = $null
$artifactHashError = $null
try {
    $artifactHash = (Get-FileHash -LiteralPath $artifactPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
} catch { $artifactHashError = $_.Exception.Message }

$signatureStatus = $null
$publisher = $null
$timestamped = $null
$signatureReadError = $null
try {
    $signature = Get-AuthenticodeSignature -LiteralPath $artifactPath -ErrorAction Stop
    $signatureStatus = [string]$signature.Status
    $signerProperty = $signature.PSObject.Properties['SignerCertificate']
    if ($null -ne $signerProperty -and $null -ne $signerProperty.Value) {
        $publisher = [string]$signerProperty.Value.Subject
    }
    $timestampProperty = $signature.PSObject.Properties['TimeStamperCertificate']
    $timestamped = [bool]($null -ne $timestampProperty -and $null -ne $timestampProperty.Value)
} catch { $signatureReadError = $_.Exception.Message }

# --------------------------------------------------------------------------- capture values
$beforeBuild = Get-JsonValue $beforeDocument 'windows.build'
$afterBuild = Get-JsonValue $afterDocument 'windows.build'
$build = ConvertTo-IntegerOrNull $afterBuild
if ($null -eq $build) { $build = ConvertTo-IntegerOrNull $beforeBuild }

$afterEdition = Get-JsonValue $afterDocument 'windows.edition'
$edition = $afterEdition
if ($null -eq $edition) { $edition = Get-JsonValue $beforeDocument 'windows.edition' }
if ($null -ne $edition -and [string]::IsNullOrWhiteSpace([string]$edition)) { $edition = $null }

$afterArchitecture = Get-JsonValue $afterDocument 'windows.architecture'
$architecture = $afterArchitecture
if ($null -eq $architecture) { $architecture = Get-JsonValue $beforeDocument 'windows.architecture' }
if ($null -ne $architecture -and [string]::IsNullOrWhiteSpace([string]$architecture)) { $architecture = $null }

$beforeSac = Get-JsonValue $beforeDocument 'sac.state'
$afterSac = Get-JsonValue $afterDocument 'sac.state'
$sacValueSource = $afterSac
if ($null -eq $sacValueSource) { $sacValueSource = $beforeSac }
$sacState = ConvertTo-IntegerOrNull $sacValueSource

$sacRegistryPath = Get-JsonValue $afterDocument 'sac.registry_path'
if ($null -eq $sacRegistryPath) { $sacRegistryPath = Get-JsonValue $beforeDocument 'sac.registry_path' }
$sacValueName = Get-JsonValue $afterDocument 'sac.value_name'
if ($null -eq $sacValueName) { $sacValueName = Get-JsonValue $beforeDocument 'sac.value_name' }
$sacSource = $null
if (-not [string]::IsNullOrWhiteSpace([string]$sacRegistryPath) -and -not [string]::IsNullOrWhiteSpace([string]$sacValueName)) {
    $sacSource = ([string]$sacRegistryPath).TrimEnd('\') + '\' + [string]$sacValueName
}

$beforeRealtime = Get-JsonValue $beforeDocument 'defender.realtime_protection'
$afterRealtime = Get-JsonValue $afterDocument 'defender.realtime_protection'
$realtimeSource = $afterRealtime
if ($null -eq $realtimeSource) { $realtimeSource = $beforeRealtime }
$realtimeEvidence = Get-BooleanOrNull $realtimeSource

$beforeAntivirus = Get-JsonValue $beforeDocument 'defender.antivirus_enabled'
$afterAntivirus = Get-JsonValue $afterDocument 'defender.antivirus_enabled'
$antivirusSource = $afterAntivirus
if ($null -eq $antivirusSource) { $antivirusSource = $beforeAntivirus }
$antivirusEvidence = Get-BooleanOrNull $antivirusSource

$afterSignatureAge = Get-JsonValue $afterDocument 'defender.signature_age_hours'
$signatureAge = ConvertTo-NumberOrNull $afterSignatureAge
if ($null -eq $signatureAge) { $signatureAge = ConvertTo-NumberOrNull (Get-JsonValue $beforeDocument 'defender.signature_age_hours') }

$beforeCleanInstall = Get-JsonValue $beforeDocument 'clean_install.value'
$cleanInstallValue = Get-BooleanOrNull $beforeCleanInstall
$cleanInstallConfidence = Get-JsonValue $beforeDocument 'clean_install.confidence'

function Get-ExclusionSummary {
    param($Capture)
    $summary = [ordered]@{ readable = $false; path = $null; extension = $null; process = $null; total = $null }
    $exclusions = Get-JsonValue $Capture 'defender.exclusions'
    if ($null -eq $exclusions) { return [pscustomobject]$summary }
    $path = Get-JsonValue $exclusions 'path'
    $extension = Get-JsonValue $exclusions 'extension'
    $process = Get-JsonValue $exclusions 'process'
    if (($null -eq $path) -or ($null -eq $extension) -or ($null -eq $process)) { return [pscustomobject]$summary }
    $summary.path = @($path).Count
    $summary.extension = @($extension).Count
    $summary.process = @($process).Count
    $summary.readable = $true
    $summary.total = $summary.path + $summary.extension + $summary.process
    return [pscustomobject]$summary
}

function Format-ExclusionSummary {
    param($Summary)
    if (-not $Summary.readable) { return 'unreadable (null, fails closed)' }
    return 'path=' + $Summary.path + ' extension=' + $Summary.extension + ' process=' + $Summary.process
}

$beforeExclusions = Get-ExclusionSummary $beforeDocument
$afterExclusions = Get-ExclusionSummary $afterDocument

$beforeCertificates = ConvertTo-IntegerOrNull (Get-JsonValue $beforeDocument 'certificates.recent_third_party.total')
$afterCertificates = ConvertTo-IntegerOrNull (Get-JsonValue $afterDocument 'certificates.recent_third_party.total')

$beforeMachine = Get-JsonValue $beforeDocument 'machine.name'
$afterMachine = Get-JsonValue $afterDocument 'machine.name'
if ($null -ne $beforeMachine) { $beforeMachine = ([string]$beforeMachine).Trim() } else { $beforeMachine = '' }
if ($null -ne $afterMachine) { $afterMachine = ([string]$afterMachine).Trim() } else { $afterMachine = '' }

$beforeCapturedAt = Get-JsonValue $beforeDocument 'captured_at'
$afterCapturedAt = Get-JsonValue $afterDocument 'captured_at'
$beforeTime = ConvertTo-CaptureTime $beforeCapturedAt
$afterTime = ConvertTo-CaptureTime $afterCapturedAt
$beforeAgeHours = $null
if ($null -ne $beforeTime) {
    $beforeAgeHours = [math]::Round(((Get-Date).ToUniversalTime() - $beforeTime.UtcDateTime).TotalHours, 2)
}

# --------------------------------------------------------------------------- preconditions
$buildOk = ($null -ne $build) -and ($build -ge 22000)
Add-Precondition 'windows.build' $buildOk ('observed build ' + (Format-Value $build) + ' (Windows 11 is build 22000 or later)')

$cleanInstallOk = ($cleanInstallValue -eq $true)
Add-Precondition 'windows.clean_install' $cleanInstallOk ('before capture clean_install=' + (Format-Value $cleanInstallValue) + ' confidence=' + (Format-Value $cleanInstallConfidence))

$beforeSacState = ConvertTo-IntegerOrNull $beforeSac
$afterSacState = ConvertTo-IntegerOrNull $afterSac
$sacOk = ($beforeSacState -eq 1) -and ($afterSacState -eq 1)
Add-Precondition 'sac.enforced' $sacOk ('before=' + (Format-Value $beforeSac) + ' after=' + (Format-Value $afterSac) + ' (1=enforced)')

$realtimeOk = (Test-IsTrue $beforeRealtime) -and (Test-IsTrue $afterRealtime)
Add-Precondition 'defender.realtime_protection' $realtimeOk ('before=' + (Format-Value $beforeRealtime) + ' after=' + (Format-Value $afterRealtime))

$antivirusOk = (Test-IsTrue $beforeAntivirus) -and (Test-IsTrue $afterAntivirus)
Add-Precondition 'defender.antivirus_enabled' $antivirusOk ('before=' + (Format-Value $beforeAntivirus) + ' after=' + (Format-Value $afterAntivirus))

$exclusionsOk = $beforeExclusions.readable -and $afterExclusions.readable -and ($beforeExclusions.total -eq 0) -and ($afterExclusions.total -eq 0)
Add-Precondition 'defender.exclusions' $exclusionsOk ('before: ' + (Format-ExclusionSummary $beforeExclusions) + '; after: ' + (Format-ExclusionSummary $afterExclusions) + ' (0 required)')

$certificatesOk = ($beforeCertificates -eq 0) -and ($afterCertificates -eq 0)
Add-Precondition 'certificates.recent_third_party' $certificatesOk ('before=' + (Format-Value $beforeCertificates) + ' after=' + (Format-Value $afterCertificates) + ' recent third-party root/trusted-publisher certificate(s) (0 required)')

$sameMachine = (-not [string]::IsNullOrWhiteSpace($beforeMachine)) -and (-not [string]::IsNullOrWhiteSpace($afterMachine)) -and ($beforeMachine -eq $afterMachine)
Add-Precondition 'captures.same_machine' $sameMachine ('before=' + (Format-Value $beforeMachine) + ' after=' + (Format-Value $afterMachine))

$freshnessOk = ($null -ne $beforeAgeHours) -and ($beforeAgeHours -le $AllowStaleHours) -and ($beforeAgeHours -ge -1)
Add-Precondition 'captures.before_freshness' $freshnessOk ('before captured_at=' + (Format-Value $beforeCapturedAt) + ' age=' + (Format-Value $beforeAgeHours) + ' h, limit ' + $AllowStaleHours + ' h')

$orderingOk = ($null -ne $beforeTime) -and ($null -ne $afterTime) -and ($afterTime.UtcDateTime -ge $beforeTime.UtcDateTime)
Add-Precondition 'captures.ordering' $orderingOk ('before=' + (Format-Value $beforeCapturedAt) + ' after=' + (Format-Value $afterCapturedAt) + ' (after must not predate before)')

$afterAgeHours = $null
if ($null -ne $afterTime) {
    $afterAgeHours = [math]::Round(((Get-Date).ToUniversalTime() - $afterTime.UtcDateTime).TotalHours, 2)
}
$afterFreshnessOk = ($null -ne $afterAgeHours) -and ($afterAgeHours -le $AllowStaleHours) -and ($afterAgeHours -ge -1)
Add-Precondition 'captures.after_freshness' $afterFreshnessOk ('after captured_at=' + (Format-Value $afterCapturedAt) + ' age=' + (Format-Value $afterAgeHours) + ' h, limit ' + $AllowStaleHours + ' h')

$failedTests = New-Object System.Collections.ArrayList
foreach ($key in $testKeys) { if ($tests[$key] -ne $true) { [void]$failedTests.Add($key) } }
$testsOk = ($failedTests.Count -eq 0)
$testsDetail = 'all five observations are true'
if (-not $testsOk) { $testsDetail = 'not true: ' + (@($failedTests) -join ', ') }
Add-Precondition 'tests.observed' $testsOk $testsDetail

$hashOk = ($null -ne $artifactHash)
$hashDetail = 'sha256=' + (Format-Value $artifactHash)
if ($null -ne $artifactHashError) { $hashDetail = $hashDetail + ' (' + $artifactHashError + ')' }
Add-Precondition 'artifact.sha256' $hashOk $hashDetail

$signatureOk = ($signatureStatus -eq 'Valid')
$signatureDetail = 'Windows returns signature status ' + (Format-Value $signatureStatus)
if ($null -ne $signatureReadError) { $signatureDetail = $signatureDetail + ' (' + $signatureReadError + ')' }
Add-Precondition 'installer.signature_status' $signatureOk $signatureDetail

$timestampOk = ($timestamped -eq $true)
Add-Precondition 'installer.timestamped' $timestampOk ('timestamp present=' + (Format-Value $timestamped))

$publisherOk = -not [string]::IsNullOrWhiteSpace($publisher)
Add-Precondition 'installer.publisher' $publisherOk ('publisher=' + (Format-Value $publisher))

$dateAgeDays = $null
if ($null -ne $parsedDate) { $dateAgeDays = [math]::Round(((Get-Date) - $parsedDate).TotalDays, 2) }
$dateOk = ($null -ne $parsedDate) -and ($dateAgeDays -le 30) -and ($dateAgeDays -ge -1)
Add-Precondition 'date.recent' $dateOk ('date=' + $Date + ' age=' + (Format-Value $dateAgeDays) + ' days (within 30 days, not in the future)')

# --------------------------------------------------------------------------- evidence
$evidence = [ordered]@{
    release = $ReleaseVersion.Trim()
    artifact_sha256 = $artifactHash
    date = $Date.Trim()
    tester = $Tester.Trim()
    windows = [ordered]@{
        edition = $edition
        build = $build
        architecture = $architecture
        clean_install = $cleanInstallValue
    }
    sac = [ordered]@{
        state = $sacState
        source = $sacSource
    }
    defender = [ordered]@{
        realtime_protection = $realtimeEvidence
        antivirus_enabled = $antivirusEvidence
        signature_age_hours = $signatureAge
    }
    installer = [ordered]@{
        signature_status = $signatureStatus
        publisher = $publisher
        timestamped = $timestamped
    }
    tests = $tests
}

$writeError = $null
try {
    $json = ConvertTo-Json -InputObject $evidence -Depth 6
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, $encoding)
} catch { $writeError = $_.Exception.Message }
if ($null -ne $writeError) {
    Write-Host ('FAIL  cannot write ' + $outputPath + ': ' + $writeError) -ForegroundColor Red
    exit 2
}

# --------------------------------------------------------------------------- report
$beforeGapCount = 0
$beforeGaps = Get-JsonValue $beforeDocument 'collection_errors'
if ($null -ne $beforeGaps) { $beforeGapCount = @($beforeGaps).Count }
$afterGapCount = 0
$afterGaps = Get-JsonValue $afterDocument 'collection_errors'
if ($null -ne $afterGaps) { $afterGapCount = @($afterGaps).Count }

Write-Host ''
Write-Host ('clean-machine evidence ' + $outputPath) -ForegroundColor Cyan
Write-Host ('  artifact sha256  ' + (Format-Value $artifactHash))
Write-Host ('  capture gaps     before=' + $beforeGapCount + ' after=' + $afterGapCount + ' (collection_errors inside each capture; gate fields are checked below)')
if ($dateWasDefaulted) { Write-Host ('  note             -Date was not supplied; recorded the assembly time ' + $Date) -ForegroundColor Yellow }
Write-Host ''
foreach ($check in $script:checks) {
    if ($check.ok) {
        Write-Host ('  PASS  ' + $check.id.PadRight(34) + ' ' + $check.detail) -ForegroundColor Green
    } else {
        Write-Host ('  FAIL  ' + $check.id.PadRight(34) + ' ' + $check.detail) -ForegroundColor Red
    }
}
$failedChecks = New-Object System.Collections.ArrayList
foreach ($check in $script:checks) { if (-not $check.ok) { [void]$failedChecks.Add($check) } }
Write-Host ''
if ($failedChecks.Count -gt 0) {
    Write-Host ('REFUSED  ' + $failedChecks.Count + ' precondition(s) unmet. The evidence file records the honest state: ' + $outputPath) -ForegroundColor Red
    exit 1
}
Write-Host ('OK  every precondition holds. Evidence written to ' + $outputPath) -ForegroundColor Green
exit 0
