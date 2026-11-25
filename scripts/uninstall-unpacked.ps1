<#
.SYNOPSIS
    Removes a CreatorOS installation made by scripts\install-unpacked.ps1.

.DESCRIPTION
    Deletes the install directory, both shortcuts and the "Installed apps" registry entry.

    It does NOT delete your data root (default %USERPROFILE%\CreatorOS). That directory holds the database, media
    library, models, credentials and logs; removing it would destroy work that the application cannot
    regenerate. Delete it by hand if that is really what you want - the path is printed at the end.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $scriptDir '..')).Path
$target = Join-Path $env:LOCALAPPDATA 'Programs\CreatorOS'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CreatorOS.lnk'
$startLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\CreatorOS.lnk'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\CreatorOS'
$dataRoot = if ($env:CREATOROS_ROOT) { $env:CREATOROS_ROOT } else { Join-Path $env:USERPROFILE 'CreatorOS' }

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }

Write-Step 'closing CreatorOS'
Get-Process CreatorOS -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Write-Step 'removing shortcuts'
foreach ($link in @($desktopLink, $startLink)) {
    if (Test-Path $link) { Remove-Item $link -Force; Write-Host "    removed $link" }
}

Write-Step 'removing the registry entry'
if (Test-Path $uninstallKey) { Remove-Item $uninstallKey -Recurse -Force }

Write-Step 'removing the install directory'
if (Test-Path $target) {
    Remove-Item $target -Recurse -Force
    Write-Host "    removed $target"
}

Write-Host ''
Write-Host "Your data is untouched: $dataRoot" -ForegroundColor Green
Write-Host 'Delete that folder yourself only if you intend to lose the database, media, models and credentials.'
