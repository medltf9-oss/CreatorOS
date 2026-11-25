<#
.SYNOPSIS
    Installs CreatorOS from the built, unpacked application directory.

.DESCRIPTION
    This is the documented fallback for a machine where Smart App Control refuses to start a newly
    built unsigned installer (see docs/RELEASE_NOTES_0.8.0.md). It performs exactly what the installer
    does, minus the NSIS executable:

      1. copy releases\<version>\win-unpacked to %LOCALAPPDATA%\Programs\CreatorOS
      2. create the Desktop shortcut (always) and the Start Menu shortcut, both with the brand icon
      3. register an entry under HKCU\...\Uninstall so the app appears in "Installed apps"
      4. print the uninstall command

    It never touches the data root, never needs administrator rights, and never changes a Windows
    security setting.

    The result is the same directory layout the installer produces, so the app, its bundled backend and
    its shortcuts are identical; only the NSIS wrapper is absent.

.PARAMETER Version
    Release to install. Defaults to the version in app\desktop\package.json.

.PARAMETER Launch
    Start the app when the installation finishes.
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [switch]$Launch
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $scriptDir '..')).Path
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = (Get-Content (Join-Path $repo 'app\desktop\package.json') -Raw | ConvertFrom-Json).version
}

$source = Join-Path $repo "releases\$Version\win-unpacked"
$target = Join-Path $env:LOCALAPPDATA 'Programs\CreatorOS'
$exe = Join-Path $target 'CreatorOS.exe'
$icon = Join-Path $target 'resources\icon.ico'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CreatorOS.lnk'
$startLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\CreatorOS.lnk'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\CreatorOS'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }

if (-not (Test-Path (Join-Path $source 'CreatorOS.exe'))) {
    throw "packaged app not found: $source (run scripts\build-release.ps1)"
}

Write-Step "installing $Version to $target"
Get-Process CreatorOS -ErrorAction SilentlyContinue | Stop-Process -Force
if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $target -Force | Out-Null
Copy-Item (Join-Path $source '*') -Destination $target -Recurse -Force

if (-not (Test-Path $exe)) { throw "copy failed: $exe missing" }
if (-not (Test-Path (Join-Path $target 'resources\backend\python.exe'))) { throw 'copy failed: bundled backend missing' }

Write-Step 'creating shortcuts'
$shell = New-Object -ComObject WScript.Shell
$iconLocation = if (Test-Path $icon) { "$icon,0" } else { "$exe,0" }
foreach ($link in @($desktopLink, $startLink)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $link) -Force | Out-Null
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $target
    $shortcut.IconLocation = $iconLocation
    $shortcut.Description = 'CreatorOS'
    $shortcut.Save()
    Write-Host "    $link"
}

Write-Step 'registering in Installed apps'
$sizeKb = [int](((Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum) / 1KB)
New-Item -Path $uninstallKey -Force | Out-Null
$entry = @{
    DisplayName     = 'CreatorOS'
    DisplayVersion  = $Version
    DisplayIcon     = $iconLocation
    InstallLocation = $target
    Publisher       = 'CreatorOS'
    NoModify        = 1
    NoRepair        = 1
    EstimatedSize   = $sizeKb
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$scriptDir\uninstall-unpacked.ps1`""
}
foreach ($name in $entry.Keys) {
    # PowerShell 5.1 has no `if` expression: pick the registry type first.
    $type = 'String'
    if ($entry[$name] -is [int]) { $type = 'DWord' }
    New-ItemProperty -Path $uninstallKey -Name $name -Value $entry[$name] -PropertyType $type -Force | Out-Null
}

Write-Step 'done'
Write-Host "Installed: $exe"
Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File `"$scriptDir\uninstall-unpacked.ps1`""
Write-Host "Your data (default %USERPROFILE%\CreatorOS) was not touched."

if ($Launch) {
    Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    Start-Process -FilePath $exe | Out-Null
    Write-Host 'Launched.'
}
