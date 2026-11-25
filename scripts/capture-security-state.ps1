# CreatorOS security-state capture (read-only).
#
# Collects the machine facts that a clean-machine release attestation depends on and
# writes them as UTF-8 JSON. It is an observation tool, not an attestation:
# scripts\build-sac-evidence.ps1 reads this file and refuses the release when a fact
# is missing or pessimistic.
#
#   powershell -ExecutionPolicy Bypass -File scripts\capture-security-state.ps1 -Output C:\evidence\before.json
#
# Read-only by construction. It reads CIM, the registry, the certificate stores and
# PATH. It never writes the registry, never changes Smart App Control, SmartScreen,
# Defender or any exclusion, never installs anything and never executes the artifact
# (or any other external program). A field it cannot read is null and is listed in
# collection_errors with its field path and the exception type; there is no default.
#
# Exit 0: the file was written and every field was read.
# Exit 1: the file was written but at least one field could not be read.
# Exit 2: -Output is missing or cannot be written.
#
# The output records the machine name and public certificate subjects. It is evidence:
# review it before committing it anywhere.

[CmdletBinding()]
param(
    [string]$Output = ''
)

$ErrorActionPreference = 'Stop'
$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$script:collectionErrors = New-Object System.Collections.ArrayList

function Add-CollectionError {
    param([string]$Field, [System.Exception]$Exception)
    $errorType = $Exception.GetType().Name
    $detail = $Exception.Message
    if ($null -ne $Exception.InnerException) {
        $errorType = $errorType + '/' + $Exception.InnerException.GetType().Name
        $detail = $Exception.InnerException.Message
    }
    [void]$script:collectionErrors.Add([pscustomobject][ordered]@{
        field      = $Field
        error_type = $errorType
        message    = $detail
    })
    Write-Warning ('could not read ' + $Field + ': ' + $errorType + ': ' + $detail)
}

function New-CollectionError {
    param([string]$Field, [string]$Message)
    Add-CollectionError -Field $Field -Exception ([System.InvalidOperationException]::new($Message))
}

function Format-Value {
    param($Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) { if ($Value) { return 'true' } else { return 'false' } }
    if ($Value -is [array]) { return (@($Value) -join ',') }
    return [string]$Value
}

# Redact the user profile prefix so the capture does not carry a personal path.
function Format-EvidencePath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $Path }
    $result = $Path
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $result = $result -replace [regex]::Escape($env:USERPROFILE), '%USERPROFILE%'
    }
    return $result
}

function Get-IsoLocal {
    param([datetime]$Time)
    return ([datetimeoffset]$Time).ToString('yyyy-MM-ddTHH:mm:sszzz', $invariant)
}

function Get-IsoUtc {
    param([datetime]$Time)
    return $Time.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', $invariant)
}

function Write-JsonFile {
    param([string]$Path, [object]$Value)
    $json = ConvertTo-Json -InputObject $Value -Depth 12
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $encoding)
}

function Get-SafeProperty {
    param([object]$Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Read-RegistryValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject][ordered]@{ key_exists = $false; value_present = $false; value = $null }
    }
    $properties = Get-ItemProperty -LiteralPath $Path -ErrorAction Stop
    $property = $properties.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return [pscustomobject][ordered]@{ key_exists = $true; value_present = $false; value = $null }
    }
    return [pscustomobject][ordered]@{ key_exists = $true; value_present = $true; value = $property.Value }
}

# Walks a registry tree without stopping at the first unreadable key. Registry policy trees
# contain keys a normal user cannot enumerate (for example ...\Windows\IPSec), so the walk
# records them instead of failing, and every failure is also added to collection_errors.
function Get-RegistryTree {
    param([string]$Root, [int]$MaxDepth, [string]$Field)
    $items = New-Object System.Collections.ArrayList
    $inaccessible = New-Object System.Collections.ArrayList
    $queue = New-Object System.Collections.Queue
    $rootItem = $null
    try {
        $rootItem = Get-Item -LiteralPath $Root -ErrorAction Stop
    } catch {
        Add-CollectionError $Field $_.Exception
        [void]$inaccessible.Add([pscustomobject][ordered]@{ key = $Root; error_type = $_.Exception.GetType().Name; message = $_.Exception.Message })
        return [pscustomobject][ordered]@{ items = @(); inaccessible = @($inaccessible) }
    }
    $queue.Enqueue([pscustomobject]@{ item = $rootItem; depth = 0 })
    while ($queue.Count -gt 0) {
        $entry = $queue.Dequeue()
        $key = $entry.item
        [void]$items.Add([pscustomobject][ordered]@{ name = [string]$key.Name; ps_path = [string]$key.PSPath; depth = $entry.depth })
        if ($entry.depth -ge $MaxDepth) { continue }
        try {
            foreach ($child in @(Get-ChildItem -LiteralPath $key.PSPath -ErrorAction Stop)) {
                $queue.Enqueue([pscustomobject]@{ item = $child; depth = ($entry.depth + 1) })
            }
        } catch {
            Add-CollectionError $Field $_.Exception
            [void]$inaccessible.Add([pscustomobject][ordered]@{ key = [string]$key.Name; error_type = $_.Exception.GetType().Name; message = $_.Exception.Message })
        }
    }
    return [pscustomobject][ordered]@{ items = @($items); inaccessible = @($inaccessible) }
}

# Collects an evidence row for virtualization detection; empty values are skipped.
$script:vmEvidence = New-Object System.Collections.ArrayList
function Add-VmEvidence {
    param([string]$Source, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return }
    [void]$script:vmEvidence.Add([pscustomobject][ordered]@{ source = $Source; value = $Value.Trim() })
}

function Test-VmEvidence {
    param([string]$Pattern)
    foreach ($row in $script:vmEvidence) {
        if ($row.value -match $Pattern) { return $true }
    }
    return $false
}

# --------------------------------------------------------------------------- output path
if ([string]::IsNullOrWhiteSpace($Output)) {
    Write-Host 'FAIL  -Output is required: pass the JSON file to write, e.g. -Output C:\evidence\before.json' -ForegroundColor Red
    exit 2
}
try {
    $outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
} catch {
    Write-Host ('FAIL  -Output cannot be resolved to a file path: ' + $Output) -ForegroundColor Red
    exit 2
}
if (Test-Path -LiteralPath $outputPath -PathType Container) {
    Write-Host ('FAIL  -Output is a directory: ' + $outputPath) -ForegroundColor Red
    exit 2
}
$outputDirectory = Split-Path -Parent $outputPath
if (-not [string]::IsNullOrWhiteSpace($outputDirectory) -and -not (Test-Path -LiteralPath $outputDirectory)) {
    try {
        New-Item -ItemType Directory -Path $outputDirectory -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Host ('FAIL  cannot create the output directory: ' + $outputDirectory) -ForegroundColor Red
        exit 2
    }
}

$now = Get-Date
$capturedAt = Get-IsoLocal $now
$capturedAtUtc = Get-IsoUtc $now

# --------------------------------------------------------------------------- machine and OS
$computerSystem = $null
try { $computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop }
catch { Add-CollectionError 'machine.computer_system' $_.Exception }

$machine = [ordered]@{
    name = $null
    domain = $null
    domain_joined = $null
    workgroup = $null
    session_elevated = $null
    powershell_version = $PSVersionTable.PSVersion.ToString()
}
if (-not [string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) {
    $machine.name = $env:COMPUTERNAME
} elseif ($null -ne $computerSystem) {
    $machine.name = [string]$computerSystem.Name
}
if ([string]::IsNullOrWhiteSpace($machine.name)) {
    New-CollectionError 'machine.name' 'neither COMPUTERNAME nor Win32_ComputerSystem.Name was readable'
}
if ($null -ne $computerSystem) {
    try {
        $machine.domain = [string]$computerSystem.Domain
        $machine.domain_joined = [bool]$computerSystem.PartOfDomain
        if ($machine.domain_joined) { $machine.workgroup = $null } else { $machine.workgroup = [string]$computerSystem.Domain }
    } catch { Add-CollectionError 'machine.domain' $_.Exception }
}
try {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    $machine.session_elevated = $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { Add-CollectionError 'machine.session_elevated' $_.Exception }

$windows = [ordered]@{
    caption = $null
    edition = $null
    version = $null
    build = $null
    architecture = $null
    install_date = $null
    install_age_days = $null
    product_type = $null
    system_drive = $null
}
$osInfo = $null
try { $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop }
catch { Add-CollectionError 'windows' $_.Exception }
if ($null -ne $osInfo) {
    try {
        $windows.caption = [string]$osInfo.Caption
        # edition is the OS caption with the vendor prefix removed; the caption is localized.
        $windows.edition = ([string]$osInfo.Caption) -replace '^Microsoft\s+', ''
        $windows.version = [string]$osInfo.Version
        $windows.architecture = [string]$osInfo.OSArchitecture
        $windows.product_type = $osInfo.ProductType
        $windows.system_drive = [string]$osInfo.SystemDrive
    } catch { Add-CollectionError 'windows.describe' $_.Exception }
    try {
        $parsedBuild = 0
        if (-not [int]::TryParse([string]$osInfo.BuildNumber, [ref]$parsedBuild)) {
            throw ('Win32_OperatingSystem.BuildNumber is not an integer: ' + [string]$osInfo.BuildNumber)
        }
        $windows.build = $parsedBuild
    } catch { Add-CollectionError 'windows.build' $_.Exception }
    try {
        if ($null -ne $osInfo.InstallDate) {
            $windows.install_date = Get-IsoLocal $osInfo.InstallDate
            $windows.install_age_days = [math]::Round(($now - $osInfo.InstallDate).TotalDays, 1)
        }
    } catch { Add-CollectionError 'windows.install_date' $_.Exception }
}

# --------------------------------------------------------------------------- virtualization
# HypervisorPresent is recorded but never used by itself: VBS and the Hyper-V platform set
# it on physical machines. Guest detection uses the SMBIOS/BIOS identity, the disk model and
# controller enumeration, and guest integration service names.
$csManufacturer = $null
$csModel = $null
$hypervisorPresent = $null
if ($null -ne $computerSystem) {
    try {
        $csManufacturer = [string]$computerSystem.Manufacturer
        $csModel = [string]$computerSystem.Model
        $hypervisorPresent = [bool]$computerSystem.HypervisorPresent
    } catch { Add-CollectionError 'virtualization.computer_system' $_.Exception }
}
Add-VmEvidence 'Win32_ComputerSystem.Manufacturer' $csManufacturer
Add-VmEvidence 'Win32_ComputerSystem.Model' $csModel

$bios = $null
try { $bios = Get-CimInstance -ClassName Win32_BIOS -ErrorAction Stop }
catch { Add-CollectionError 'virtualization.bios' $_.Exception }
if ($null -ne $bios) {
    Add-VmEvidence 'Win32_BIOS.Manufacturer' ([string]$bios.Manufacturer)
    Add-VmEvidence 'Win32_BIOS.SerialNumber' ([string]$bios.SerialNumber)
    Add-VmEvidence 'Win32_BIOS.SMBIOSBIOSVersion' ([string]$bios.SMBIOSBIOSVersion)
}

$diskDrives = $null
try { $diskDrives = @(Get-CimInstance -ClassName Win32_DiskDrive -ErrorAction Stop) }
catch { Add-CollectionError 'virtualization.disk_drives' $_.Exception }
foreach ($disk in @($diskDrives)) {
    if ($null -ne $disk) { Add-VmEvidence 'Win32_DiskDrive.Model' ([string]$disk.Model) }
}

try {
    $diskEnumKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\Disk\Enum'
    if (Test-Path -LiteralPath $diskEnumKey) {
        $diskEnum = Get-ItemProperty -LiteralPath $diskEnumKey -ErrorAction Stop
        foreach ($property in $diskEnum.PSObject.Properties) {
            if ($property.Name -like 'PS*' -or $property.Name -eq '(default)') { continue }
            Add-VmEvidence ('registry:' + $diskEnumKey + ':' + $property.Name) ([string]$property.Value)
        }
    }
} catch { Add-CollectionError 'virtualization.disk_enum' $_.Exception }

foreach ($serviceName in @('VBoxService', 'VBoxGuest', 'vmtools', 'vmhgfs', 'QEMU-GA', 'qemu-ga', 'vmicheartbeat')) {
    try {
        $guestService = Get-Service -Name $serviceName -ErrorAction Stop
        Add-VmEvidence ('service:' + $serviceName) ([string]$guestService.Status)
    } catch { }
}

$vmVendors = New-Object System.Collections.ArrayList
$hasVmwareService = $false
foreach ($row in $script:vmEvidence) {
    if ($row.source -match '(?i)^service:(vmtools|vmhgfs)$') { $hasVmwareService = $true }
}
if ((Test-VmEvidence '(?i)vmware') -or $hasVmwareService) { [void]$vmVendors.Add('VMware') }
if (Test-VmEvidence '(?i)virtualbox|innotek|vbox') { [void]$vmVendors.Add('VirtualBox') }
if (Test-VmEvidence '(?i)qemu|bochs|virtio') { [void]$vmVendors.Add('QEMU') }
if ((($csManufacturer -match '(?i)microsoft') -and ($csModel -match '(?i)virtual machine')) -or (Test-VmEvidence '(?i)hyper-v|virtual hd|msft virtual disk')) { [void]$vmVendors.Add('Hyper-V') }

$virtualization = [ordered]@{
    detected = ($vmVendors.Count -gt 0)
    vendors = @($vmVendors)
    hypervisor_present = $hypervisorPresent
    evidence = @($script:vmEvidence)
    method = 'SMBIOS manufacturer/model, BIOS strings, disk model and disk controller enumeration, guest integration service names. HypervisorPresent alone is not treated as virtualization because VBS sets it on physical machines.'
}

# --------------------------------------------------------------------------- developer tools
# Presence only: no external program is executed, so PATH and the known install
# locations are the evidence. A tool present but not on PATH still counts.
function Join-IfRooted {
    param([string]$Base, [string]$Relative)
    if ([string]::IsNullOrWhiteSpace($Base)) { return $null }
    return (Join-Path $Base $Relative)
}

$localPrograms = $env:LOCALAPPDATA
$toolSpecs = @(
    [pscustomobject]@{ name = 'git'; commands = @('git'); known = @('C:\Program Files\Git\cmd\git.exe', 'C:\Program Files (x86)\Git\cmd\git.exe', (Join-IfRooted $localPrograms 'Programs\Git\cmd\git.exe')) },
    [pscustomobject]@{ name = 'python'; commands = @('python', 'python3', 'py'); known = @('C:\Python*\python.exe', (Join-IfRooted $localPrograms 'Programs\Python\Python*\python.exe'), 'C:\Program Files\Python*\python.exe') },
    [pscustomobject]@{ name = 'node'; commands = @('node'); known = @('C:\Program Files\nodejs\node.exe', 'C:\Program Files (x86)\nodejs\node.exe') },
    [pscustomobject]@{ name = 'code'; commands = @('code'); known = @((Join-IfRooted $localPrograms 'Programs\Microsoft VS Code\Code.exe'), 'C:\Program Files\Microsoft VS Code\Code.exe') },
    [pscustomobject]@{ name = 'signtool'; commands = @('signtool'); known = @('C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe', 'C:\Program Files\Windows Kits\10\bin\*\x64\signtool.exe') }
)

$toolResults = New-Object System.Collections.ArrayList
$foundTools = New-Object System.Collections.ArrayList
foreach ($spec in $toolSpecs) {
    $record = [ordered]@{ name = $spec.name; found = $false; on_path = $false; path = $null; source = $null; known_locations = @() }
    try {
        foreach ($commandName in $spec.commands) {
            $command = Get-Command -Name $commandName -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($null -ne $command) {
                $record.on_path = $true
                $record.path = Format-EvidencePath ([string]$command.Source)
                $record.source = 'PATH'
                break
            }
        }
    } catch { Add-CollectionError ('developer_tools.' + $spec.name) $_.Exception }
    $knownLocations = New-Object System.Collections.ArrayList
    foreach ($pattern in @($spec.known)) {
        if ([string]::IsNullOrWhiteSpace([string]$pattern)) { continue }
        try {
            foreach ($match in @(Get-Item -Path ([string]$pattern) -ErrorAction SilentlyContinue)) {
                if ($null -ne $match) { [void]$knownLocations.Add((Format-EvidencePath ([string]$match.FullName))) }
            }
        } catch { Add-CollectionError ('developer_tools.' + $spec.name + '.known_locations') $_.Exception }
    }
    $record.known_locations = @($knownLocations)
    if (-not $record.on_path -and $knownLocations.Count -gt 0) {
        $record.found = $true
        $record.source = 'known location'
        $record.path = $knownLocations[0]
    } elseif ($record.on_path) {
        $record.found = $true
    }
    if ($record.found) { [void]$foundTools.Add($spec.name) }
    [void]$toolResults.Add([pscustomobject]$record)
}

$developerTools = [ordered]@{
    present = @($foundTools)
    none_found = ($foundTools.Count -eq 0)
    tools = @($toolResults)
    method = 'Get-Command on PATH plus literal and wildcard known install locations; no tool is executed.'
}

# --------------------------------------------------------------------------- Smart App Control
# VerifiedAndReputablePolicyState: 0 = off, 1 = enforced, 2 = evaluation, absent = not configured.
# 1 is the only acceptable value for clean-machine release evidence.
$sac = [ordered]@{
    registry_path = 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy'
    value_name = 'VerifiedAndReputablePolicyState'
    key_exists = $null
    configured = $null
    raw_value = $null
    state = $null
    interpretation = $null
}
try {
    $sacKey = $sac.registry_path
    $sac.key_exists = Test-Path -LiteralPath $sacKey
    if ($sac.key_exists) {
        $sacPolicyValues = Get-ItemProperty -LiteralPath $sacKey -ErrorAction Stop
        $sacProperty = $sacPolicyValues.PSObject.Properties[$sac.value_name]
        if ($null -ne $sacProperty) {
            $sac.configured = $true
            $sac.raw_value = $sacProperty.Value
            try {
                $sacState = [int]$sacProperty.Value
                $sac.state = $sacState
                if ($sacState -eq 0) { $sac.interpretation = 'off' }
                elseif ($sacState -eq 1) { $sac.interpretation = 'enforced' }
                elseif ($sacState -eq 2) { $sac.interpretation = 'evaluation' }
                else { $sac.interpretation = 'unrecognised_value_' + $sacState }
            } catch { Add-CollectionError 'sac.state' $_.Exception }
        } else {
            $sac.configured = $false
            $sac.interpretation = 'not_configured'
        }
    } else {
        $sac.configured = $false
        $sac.interpretation = 'not_configured'
    }
} catch { Add-CollectionError 'sac' $_.Exception }

# A policy or MDM value is evidence of central management. The scan is best effort: keys
# this session cannot enumerate are recorded in the capture and in collection_errors, and
# managed is null when nothing was found but part of the tree was unreadable, false only
# when the whole scanned tree was readable and held no such value.
$sacPolicy = [ordered]@{
    managed = $null
    scan_complete = $null
    scanned_roots = @()
    evidence = @()
    inaccessible_keys = @()
    note = 'Policy or MDM values whose name or key mentions Smart App Control or AppHVSI. Absence means no such value was found under the scanned roots; it does not prove the machine is unmanaged.'
}
try {
    $policyRoots = @(
        'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AppHVSI',
        'HKLM:\SOFTWARE\Policies\Microsoft\Windows\SmartAppControl',
        'HKLM:\SOFTWARE\Policies\Microsoft\Windows',
        'HKLM:\SOFTWARE\Microsoft\PolicyManager\current\device'
    )
    $scannedRoots = New-Object System.Collections.ArrayList
    $sacPolicyEvidence = New-Object System.Collections.ArrayList
    $inaccessibleKeys = New-Object System.Collections.ArrayList
    foreach ($root in $policyRoots) {
        if (-not (Test-Path -LiteralPath $root)) {
            [void]$scannedRoots.Add($root + ' (absent)')
            continue
        }
        [void]$scannedRoots.Add($root)
        $tree = Get-RegistryTree -Root $root -MaxDepth 3 -Field 'sac.policy_managed'
        foreach ($inaccessible in @($tree.inaccessible)) {
            if ($null -ne $inaccessible) { [void]$inaccessibleKeys.Add($inaccessible) }
        }
        foreach ($item in @($tree.items)) {
            if ($null -eq $item) { continue }
            try {
                $keyProperties = Get-ItemProperty -LiteralPath $item.ps_path -ErrorAction Stop
            } catch {
                Add-CollectionError 'sac.policy_managed' $_.Exception
                [void]$inaccessibleKeys.Add([pscustomobject][ordered]@{ key = $item.name; error_type = $_.Exception.GetType().Name; message = $_.Exception.Message })
                continue
            }
            foreach ($property in $keyProperties.PSObject.Properties) {
                if ($property.Name -like 'PS*' -or $property.Name -eq '(default)') { continue }
                if (($property.Name -match '(?i)(verifiedandreputable|smartappcontrol|apphvsi|smart.?app.?control)') -or ($item.name -match '(?i)(apphvsi|smartappcontrol)')) {
                    [void]$sacPolicyEvidence.Add([pscustomobject][ordered]@{ key = $item.name; name = $property.Name; value = $property.Value })
                }
            }
        }
    }
    $sacPolicy.scanned_roots = @($scannedRoots)
    $sacPolicy.evidence = @($sacPolicyEvidence)
    $sacPolicy.inaccessible_keys = @($inaccessibleKeys)
    $sacPolicy.scan_complete = ($inaccessibleKeys.Count -eq 0)
    if ($sacPolicyEvidence.Count -gt 0) { $sacPolicy.managed = $true }
    elseif ($inaccessibleKeys.Count -gt 0) { $sacPolicy.managed = $null }
    else { $sacPolicy.managed = $false }
} catch {
    Add-CollectionError 'sac.policy_managed' $_.Exception
}

# --------------------------------------------------------------------------- SmartScreen
# Raw values only. Windows interprets SmartScreenEnabled differently by context, so this
# script does not translate the strings into a claim.
$smartScreen = [ordered]@{
    hkcu = [ordered]@{ key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer'; value_name = 'SmartScreenEnabled'; key_exists = $null; value_present = $null; value = $null }
    hklm = [ordered]@{ key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer'; value_name = 'SmartScreenEnabled'; key_exists = $null; value_present = $null; value = $null }
}
try {
    $smartScreenHkcu = Read-RegistryValue $smartScreen.hkcu.key $smartScreen.hkcu.value_name
    $smartScreen.hkcu.key_exists = $smartScreenHkcu.key_exists
    $smartScreen.hkcu.value_present = $smartScreenHkcu.value_present
    $smartScreen.hkcu.value = $smartScreenHkcu.value
} catch { Add-CollectionError 'smartscreen.hkcu' $_.Exception }
try {
    $smartScreenHklm = Read-RegistryValue $smartScreen.hklm.key $smartScreen.hklm.value_name
    $smartScreen.hklm.key_exists = $smartScreenHklm.key_exists
    $smartScreen.hklm.value_present = $smartScreenHklm.value_present
    $smartScreen.hklm.value = $smartScreenHklm.value
} catch { Add-CollectionError 'smartscreen.hklm' $_.Exception }

# --------------------------------------------------------------------------- Microsoft Defender
# When the Defender module is unavailable, every Defender field stays null and the reason is
# recorded. Nothing here assumes protection is on.
function Copy-DefenderValue {
    param([object]$Source, [string]$Property, [string]$Field, [scriptblock]$Convert)
    $propertyInfo = $null
    if ($null -ne $Source) { $propertyInfo = $Source.PSObject.Properties[$Property] }
    if ($null -eq $propertyInfo) {
        New-CollectionError ('defender.' + $Field) ('Get-MpComputerStatus returned no ' + $Property + ' property')
        return $null
    }
    if ($null -eq $propertyInfo.Value) { return $null }
    if ($null -ne $Convert) { return (& $Convert $propertyInfo.Value) }
    return $propertyInfo.Value
}

function ConvertTo-CleanStringArray {
    param($Value)
    $list = New-Object System.Collections.ArrayList
    foreach ($item in @($Value)) {
        if ($null -eq $item) { continue }
        $text = [string]$item
        if ([string]::IsNullOrWhiteSpace($text)) { continue }
        [void]$list.Add($text)
    }
    return ,$list
}

$defender = [ordered]@{
    module_available = $null
    unavailable_reason = $null
    realtime_protection = $null
    antivirus_enabled = $null
    am_service_enabled = $null
    am_service_state = $null
    signature_version = $null
    signature_last_updated = $null
    signature_age_hours = $null
    antivirus_signature_age_days = $null
    tamper_protection = $null
    exclusions = [ordered]@{ path = $null; extension = $null; process = $null; reason = $null }
}
$hasStatusCommand = $null -ne (Get-Command -Name Get-MpComputerStatus -ErrorAction SilentlyContinue)
$hasPreferenceCommand = $null -ne (Get-Command -Name Get-MpPreference -ErrorAction SilentlyContinue)
$defender.module_available = ($hasStatusCommand -and $hasPreferenceCommand)
if (-not $hasStatusCommand) {
    New-CollectionError 'defender.Get-MpComputerStatus' 'the Defender module is not available on this system, so Defender values are null (no default is assumed)'
    $defender.unavailable_reason = 'Get-MpComputerStatus is not available: the Defender module is not present'
}
if (-not $hasPreferenceCommand) {
    New-CollectionError 'defender.Get-MpPreference' 'Get-MpPreference is not available on this system, so exclusion lists are null (no default is assumed)'
    if ($null -eq $defender.unavailable_reason) {
        $defender.unavailable_reason = 'Get-MpPreference is not available: the Defender module is not present'
    } else {
        $defender.unavailable_reason = $defender.unavailable_reason + '; Get-MpPreference is not available'
    }
}

$mpStatus = $null
if ($hasStatusCommand) {
    try { $mpStatus = Get-MpComputerStatus -ErrorAction Stop }
    catch {
        Add-CollectionError 'defender.Get-MpComputerStatus' $_.Exception
        $defender.unavailable_reason = 'Get-MpComputerStatus failed: ' + $_.Exception.Message
    }
}
if ($null -ne $mpStatus) {
    $defender.realtime_protection = Copy-DefenderValue $mpStatus 'RealTimeProtectionEnabled' 'realtime_protection' { param($v) [bool]$v }
    $defender.antivirus_enabled = Copy-DefenderValue $mpStatus 'AntivirusEnabled' 'antivirus_enabled' { param($v) [bool]$v }
    $defender.am_service_enabled = Copy-DefenderValue $mpStatus 'AMServiceEnabled' 'am_service_enabled' { param($v) [bool]$v }
    $defender.tamper_protection = Copy-DefenderValue $mpStatus 'IsTamperProtected' 'tamper_protection' { param($v) [bool]$v }
    $defender.signature_version = Copy-DefenderValue $mpStatus 'AntivirusSignatureVersion' 'signature_version' $null
    $defender.signature_last_updated = Copy-DefenderValue $mpStatus 'AntivirusSignatureLastUpdated' 'signature_last_updated' { param($v) Get-IsoUtc ([datetime]$v) }
    $defender.antivirus_signature_age_days = Copy-DefenderValue $mpStatus 'AntivirusSignatureAge' 'antivirus_signature_age_days' $null
    try {
        $lastUpdated = Get-SafeProperty $mpStatus 'AntivirusSignatureLastUpdated'
        if ($null -ne $lastUpdated) {
            $defender.signature_age_hours = [math]::Round(($now - ([datetime]$lastUpdated)).TotalHours, 1)
        } elseif ($null -ne $defender.antivirus_signature_age_days) {
            $defender.signature_age_hours = [math]::Round(([double]$defender.antivirus_signature_age_days) * 24, 1)
        }
    } catch { Add-CollectionError 'defender.signature_age_hours' $_.Exception }
}
try {
    $winDefend = Get-Service -Name WinDefend -ErrorAction Stop
    $defender.am_service_state = [string]$winDefend.Status
} catch { Add-CollectionError 'defender.am_service_state' $_.Exception }

if ($hasPreferenceCommand) {
    try {
        $mpPreference = Get-MpPreference -ErrorAction Stop
        $exclusionPath = Get-SafeProperty $mpPreference 'ExclusionPath'
        $exclusionExtension = Get-SafeProperty $mpPreference 'ExclusionExtension'
        $exclusionProcess = Get-SafeProperty $mpPreference 'ExclusionProcess'
        # A non-administrator session makes Get-MpPreference return the literal sentinel
        # 'N/A: Must be an administrator to view exclusions'. That is not an exclusion and
        # not an empty list: the values are unknown, so they stay null and fail closed.
        $exclusionUnavailable = $false
        foreach ($value in @($exclusionPath, $exclusionExtension, $exclusionProcess)) {
            foreach ($item in @($value)) {
                if ($null -ne $item -and ([string]$item) -match '^\s*N/A') { $exclusionUnavailable = $true }
            }
        }
        if ($exclusionUnavailable) {
            $defender.exclusions = [ordered]@{
                path = $null
                extension = $null
                process = $null
                reason = 'Defender returned its non-administrator sentinel instead of exclusion data; exclusions are only reported to an elevated session'
            }
            New-CollectionError 'defender.exclusions' 'Defender only reports exclusions to an administrator; exclusion lists are null, not assumed empty'
        } else {
            $defender.exclusions = [ordered]@{
                path = (ConvertTo-CleanStringArray $exclusionPath)
                extension = (ConvertTo-CleanStringArray $exclusionExtension)
                process = (ConvertTo-CleanStringArray $exclusionProcess)
                reason = $null
            }
        }
    } catch { Add-CollectionError 'defender.exclusions' $_.Exception }
}

$defenderPolicy = [ordered]@{
    key_exists = $null
    scan_complete = $null
    values = @()
    inaccessible_keys = @()
    note = 'Values under HKLM\SOFTWARE\Policies\Microsoft\Windows Defender. Their presence is evidence of central management; absence does not prove the machine is unmanaged.'
}
try {
    $defenderPolicyKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender'
    $defenderPolicy.key_exists = Test-Path -LiteralPath $defenderPolicyKey
    if ($defenderPolicy.key_exists) {
        $defenderPolicyValues = New-Object System.Collections.ArrayList
        $defenderPolicyInaccessible = New-Object System.Collections.ArrayList
        $tree = Get-RegistryTree -Root $defenderPolicyKey -MaxDepth 2 -Field 'defender.policy_managed'
        foreach ($inaccessible in @($tree.inaccessible)) {
            if ($null -ne $inaccessible) { [void]$defenderPolicyInaccessible.Add($inaccessible) }
        }
        foreach ($item in @($tree.items)) {
            if ($null -eq $item) { continue }
            try {
                $keyProperties = Get-ItemProperty -LiteralPath $item.ps_path -ErrorAction Stop
            } catch {
                Add-CollectionError 'defender.policy_managed' $_.Exception
                [void]$defenderPolicyInaccessible.Add([pscustomobject][ordered]@{ key = $item.name; error_type = $_.Exception.GetType().Name; message = $_.Exception.Message })
                continue
            }
            foreach ($property in $keyProperties.PSObject.Properties) {
                if ($property.Name -like 'PS*' -or $property.Name -eq '(default)') { continue }
                [void]$defenderPolicyValues.Add([pscustomobject][ordered]@{ key = $item.name; name = $property.Name; value = $property.Value })
            }
        }
        $defenderPolicy.values = @($defenderPolicyValues)
        $defenderPolicy.inaccessible_keys = @($defenderPolicyInaccessible)
        $defenderPolicy.scan_complete = ($defenderPolicyInaccessible.Count -eq 0)
    } else {
        $defenderPolicy.scan_complete = $true
    }
} catch { Add-CollectionError 'defender.policy_managed' $_.Exception }
$defender.policy_managed = $defenderPolicy

# --------------------------------------------------------------------------- certificate stores
# Third-party certificate filter, stated exactly because it gates the release:
#
#   LocalMachine\Root and LocalMachine\TrustedPublisher are enumerated. A certificate is
#   reported only when ALL of these hold:
#     1. NotBefore (converted to UTC) falls within the last 30 days;
#     2. NotBefore is not more than 24 hours in the future (clock-skew guard; a
#        forward-dated certificate is not reported as a recent import);
#     3. its Subject does not match the word Microsoft case-insensitively, which excludes
#        Microsoft's own root and publisher subjects.
#   Issuer is reported but is deliberately NOT an exclusion reason: a third-party publisher
#   certificate issued by a Microsoft CA is still a third-party trust change.
#   'Issued' means the certificate NotBefore. Windows does not record when a certificate was
#   imported into a store, so import time cannot be read and is not claimed.
#   Both stores must be readable for the recent-import precondition to hold; an unreadable
#   store yields null counts, which fails closed in build-sac-evidence.ps1.
$certificateWindowDays = 30
$certificateCutoff = $now.ToUniversalTime().AddDays(-$certificateWindowDays)
$certificateFutureLimit = $now.ToUniversalTime().AddHours(24)
$certificateStores = @(
    [pscustomobject]@{ name = 'root'; label = 'LocalMachine\Root'; path = 'Cert:\LocalMachine\Root' },
    [pscustomobject]@{ name = 'trusted_publisher'; label = 'LocalMachine\TrustedPublisher'; path = 'Cert:\LocalMachine\TrustedPublisher' }
)
$certificateStoreResults = [ordered]@{}
foreach ($store in $certificateStores) {
    $entries = $null
    try {
        $storeCertificates = @(Get-ChildItem -Path $store.path -ErrorAction Stop)
        $storeEntries = New-Object System.Collections.ArrayList
        foreach ($certificate in @($storeCertificates)) {
            if ($null -eq $certificate) { continue }
            $notBefore = ([datetime]$certificate.NotBefore).ToUniversalTime()
            if ($notBefore -lt $certificateCutoff) { continue }
            if ($notBefore -gt $certificateFutureLimit) { continue }
            if ([string]$certificate.Subject -match '(?i)\bmicrosoft\b') { continue }
            [void]$storeEntries.Add([pscustomobject][ordered]@{
                store = $store.label
                subject = [string]$certificate.Subject
                issuer = [string]$certificate.Issuer
                thumbprint = [string]$certificate.Thumbprint
                serial_number = [string]$certificate.SerialNumber
                not_before = Get-IsoUtc ([datetime]$certificate.NotBefore)
                not_after = Get-IsoUtc ([datetime]$certificate.NotAfter)
                self_issued = ([string]$certificate.Subject -eq [string]$certificate.Issuer)
            })
        }
        $entries = @($storeEntries)
    } catch {
        Add-CollectionError ('certificates.' + $store.name) $_.Exception
    }
    $certificateStoreResults[$store.name] = $entries
}
$rootEntries = $certificateStoreResults['root']
$trustedPublisherEntries = $certificateStoreResults['trusted_publisher']
$recentThirdPartyTotal = $null
if ($null -ne $rootEntries -and $null -ne $trustedPublisherEntries) {
    $recentThirdPartyTotal = @($rootEntries).Count + @($trustedPublisherEntries).Count
}
$certificates = [ordered]@{
    window_days = $certificateWindowDays
    filter = 'NotBefore within the last 30 days, NotBefore not more than 24 hours in the future, Subject without the word Microsoft; stores LocalMachine\Root and LocalMachine\TrustedPublisher. Windows does not record import time, so NotBefore is the only date used.'
    stores = $certificateStoreResults
    recent_third_party = [ordered]@{
        root_count = $(if ($null -ne $rootEntries) { @($rootEntries).Count } else { $null })
        trusted_publisher_count = $(if ($null -ne $trustedPublisherEntries) { @($trustedPublisherEntries).Count } else { $null })
        total = $recentThirdPartyTotal
    }
}

# --------------------------------------------------------------------------- clean-install heuristic
# value is true only when every checked indicator is negative: no developer tooling, no
# CreatorOS source or build tree, no existing CreatorOS installation, no existing data-root
# database, and a readable operating system install date no older than 90 days.
# Contamination makes it false. Anything else is null. It is a heuristic: it cannot prove a
# fresh installation, so the confidence is never higher than medium for true.
$cleanInstallChecked = New-Object System.Collections.ArrayList
$cleanInstallContradictions = New-Object System.Collections.ArrayList
$cleanInstallSupporting = New-Object System.Collections.ArrayList

if ($foundTools.Count -gt 0) {
    [void]$cleanInstallContradictions.Add('developer tooling found: ' + (@($foundTools) -join ', '))
} else {
    [void]$cleanInstallSupporting.Add('no git, python, node, code or signtool on PATH or in the known install locations')
}
[void]$cleanInstallChecked.Add('developer tools on PATH and in known install locations')

$sourceTreeCandidates = New-Object System.Collections.ArrayList
if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) { [void]$sourceTreeCandidates.Add((Split-Path -Parent $PSScriptRoot)) }
[void]$sourceTreeCandidates.Add('D:\CreatorOS')
[void]$sourceTreeCandidates.Add('C:\CreatorOS')
if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) { [void]$sourceTreeCandidates.Add((Join-Path $env:USERPROFILE 'CreatorOS')) }
$sourceTreesFound = New-Object System.Collections.ArrayList
foreach ($candidate in $sourceTreeCandidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    try {
        $hasPackage = Test-Path -LiteralPath (Join-Path $candidate 'app\desktop\package.json')
        $hasGit = Test-Path -LiteralPath (Join-Path $candidate '.git')
        $hasGate = Test-Path -LiteralPath (Join-Path $candidate 'scripts\release-gate.ps1')
        if ($hasPackage -and ($hasGit -or $hasGate)) {
            if ($sourceTreesFound -notcontains $candidate) { [void]$sourceTreesFound.Add($candidate) }
        }
    } catch { Add-CollectionError 'clean_install.source_tree' $_.Exception }
}
if ($sourceTreesFound.Count -gt 0) {
    [void]$cleanInstallContradictions.Add('CreatorOS source or build tree present: ' + (@($sourceTreesFound) -join ', '))
} else {
    [void]$cleanInstallSupporting.Add('no CreatorOS source or build tree at the checked locations')
}
[void]$cleanInstallChecked.Add('CreatorOS source and build trees at the checked locations')

$installationMarkers = New-Object System.Collections.ArrayList
if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { [void]$installationMarkers.Add((Join-Path $env:LOCALAPPDATA 'Programs\CreatorOS\CreatorOS.exe')) }
$installationFound = New-Object System.Collections.ArrayList
foreach ($marker in $installationMarkers) {
    try {
        if (Test-Path -LiteralPath $marker) {
            $display = Format-EvidencePath $marker
            if ($installationFound -notcontains $display) { [void]$installationFound.Add($display) }
        }
    } catch { Add-CollectionError 'clean_install.existing_installation' $_.Exception }
}
if ($installationFound.Count -gt 0) {
    [void]$cleanInstallContradictions.Add('an existing CreatorOS installation was found: ' + (@($installationFound) -join ', '))
} else {
    [void]$cleanInstallSupporting.Add('no existing CreatorOS installation under %LOCALAPPDATA%\Programs\CreatorOS')
}
[void]$cleanInstallChecked.Add('existing CreatorOS installation')

$dataRootCandidates = New-Object System.Collections.ArrayList
if (-not [string]::IsNullOrWhiteSpace($env:CREATOROS_ROOT)) { [void]$dataRootCandidates.Add($env:CREATOROS_ROOT) }
if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) { [void]$dataRootCandidates.Add((Join-Path $env:USERPROFILE 'CreatorOS')) }
$dataRootMarkersFound = New-Object System.Collections.ArrayList
foreach ($dataRoot in $dataRootCandidates) {
    if ([string]::IsNullOrWhiteSpace($dataRoot)) { continue }
    foreach ($relative in @('data\database\creatoros.db', 'config\bootstrap.json')) {
        try {
            $marker = Join-Path $dataRoot $relative
            if (Test-Path -LiteralPath $marker) {
                $display = Format-EvidencePath $marker
                if ($dataRootMarkersFound -notcontains $display) { [void]$dataRootMarkersFound.Add($display) }
            }
        } catch { Add-CollectionError 'clean_install.existing_data' $_.Exception }
    }
}
if ($dataRootMarkersFound.Count -gt 0) {
    [void]$cleanInstallContradictions.Add('an existing CreatorOS data root was found: ' + (@($dataRootMarkersFound) -join ', '))
} else {
    [void]$cleanInstallSupporting.Add('no existing CreatorOS data-root database or bootstrap file at the checked locations')
}
[void]$cleanInstallChecked.Add('existing CreatorOS data root')

if ($null -ne $windows.install_age_days) {
    [void]$cleanInstallSupporting.Add('operating system install date is readable: ' + (Format-Value $windows.install_date) + ' (' + (Format-Value $windows.install_age_days) + ' days ago)')
} else {
    [void]$cleanInstallChecked.Add('operating system install date could not be read')
}

$cleanInstallValue = $null
$cleanInstallConfidence = 'none'
$cleanInstallReason = $null
if ($cleanInstallContradictions.Count -gt 0) {
    $cleanInstallValue = $false
    $cleanInstallConfidence = 'high'
    $cleanInstallReason = 'contamination found: ' + (@($cleanInstallContradictions) -join '; ')
} elseif ($null -eq $windows.install_age_days) {
    $cleanInstallReason = 'no contamination was found, but the operating system install date could not be read, so a clean installation cannot be established'
} elseif ($windows.install_age_days -gt 90) {
    $cleanInstallReason = 'no contamination was found, but the operating system was installed ' + (Format-Value $windows.install_age_days) + ' days ago; a machine that old is not treated as a fresh installation from this heuristic. Provision a fresh Windows 11 x64 VM and capture again.'
} else {
    $cleanInstallValue = $true
    $cleanInstallConfidence = 'medium'
    $cleanInstallReason = 'no contamination was found and the operating system install date is readable and recent; this is a heuristic, not proof of a fresh installation'
}
$cleanInstall = [ordered]@{
    value = $cleanInstallValue
    confidence = $cleanInstallConfidence
    reason = $cleanInstallReason
    checked = @($cleanInstallChecked)
    contradictions = @($cleanInstallContradictions)
    supporting = @($cleanInstallSupporting)
    rule = 'false when contamination is found; true only when no contamination is found and the OS install date is readable and at most 90 days old; null otherwise. Heuristic only.'
}

# --------------------------------------------------------------------------- write the capture
$capture = [ordered]@{
    capture_version = 1
    generator = 'scripts/capture-security-state.ps1'
    captured_at = $capturedAt
    captured_at_utc = $capturedAtUtc
    machine = $machine
    windows = $windows
    virtualization = $virtualization
    developer_tools = $developerTools
    sac = $sac
    sac_policy = $sacPolicy
    smartscreen = $smartScreen
    defender = $defender
    certificates = $certificates
    clean_install = $cleanInstall
    collection_errors = @($script:collectionErrors)
}

$writeError = $null
try { Write-JsonFile -Path $outputPath -Value $capture }
catch { $writeError = $_.Exception.Message }
if ($null -ne $writeError) {
    Write-Host ('FAIL  cannot write ' + $outputPath + ': ' + $writeError) -ForegroundColor Red
    exit 2
}

$exclusionSummary = 'unreadable'
if ($null -ne $defender.exclusions -and $null -ne $defender.exclusions.path) {
    $exclusionSummary = 'path=' + @($defender.exclusions.path).Count + ' extension=' + @($defender.exclusions.extension).Count + ' process=' + @($defender.exclusions.process).Count
} elseif ($null -ne $defender.exclusions -and $null -ne $defender.exclusions.reason) {
    $exclusionSummary = 'unreadable (' + $defender.exclusions.reason + ')'
}
Write-Host ''
Write-Host ('capture      ' + $outputPath)
Write-Host ('windows      ' + (Format-Value $windows.caption) + ' build ' + (Format-Value $windows.build) + ' ' + (Format-Value $windows.architecture))
Write-Host ('sac          ' + (Format-Value $sac.interpretation) + ' (raw ' + (Format-Value $sac.raw_value) + ', state ' + (Format-Value $sac.state) + ')')
Write-Host ('defender     realtime=' + (Format-Value $defender.realtime_protection) + ' antivirus=' + (Format-Value $defender.antivirus_enabled) + ' tamper=' + (Format-Value $defender.tamper_protection) + ' module=' + (Format-Value $defender.module_available))
Write-Host ('exclusions   ' + $exclusionSummary)
Write-Host ('certificates ' + (Format-Value $certificates.recent_third_party.total) + ' recent third-party (root=' + (Format-Value $certificates.recent_third_party.root_count) + ', trusted_publisher=' + (Format-Value $certificates.recent_third_party.trusted_publisher_count) + ')')
Write-Host ('clean-install value=' + (Format-Value $cleanInstallValue) + ' confidence=' + $cleanInstallConfidence)
Write-Host ('collection   ' + $script:collectionErrors.Count + ' field(s) could not be read')

if ($script:collectionErrors.Count -gt 0) {
    Write-Host ('INCOMPLETE  ' + $script:collectionErrors.Count + ' field(s) are null; see collection_errors in ' + $outputPath) -ForegroundColor Yellow
    exit 1
}
Write-Host ('OK          capture written to ' + $outputPath) -ForegroundColor Green
exit 0
