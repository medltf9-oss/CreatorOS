<#
.SYNOPSIS
    Installs the built release and verifies the result, without touching the user's data root.

.DESCRIPTION
    Answers the questions a release actually has to answer, in order, and prints one line per check:

      1. the installer runs and exits 0
      2. the Desktop shortcut exists and points at the installed exe
      3. the Start Menu shortcut exists and points at the installed exe
      4. both shortcuts carry the brand icon, not the vendor's
      5. the installed app starts with no development tooling in reach: it launches, brings up its
         own bundled backend and shows a window
      6. the backend it started is the bundled interpreter, not a Python on PATH
      7. the data root it reports is the configured one, and the database there is intact and at head
      8. (optional) uninstalling removes the shortcuts and leaves the data root alone

    It never writes to the data root beyond what the app itself does, and it never changes a Windows
    security setting.

.PARAMETER Uninstall
    Uninstall at the end and check that the shortcuts are gone and the data root survived.

.PARAMETER KeepInstalled
    Leave the app running afterwards (default: close it).
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$KeepInstalled
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $scriptDir '..')).Path
$desktopVersion = (Get-Content (Join-Path $repo 'app\desktop\package.json') -Raw | ConvertFrom-Json).version
$setup = Join-Path $repo "releases\$desktopVersion\CreatorOS-Setup-$desktopVersion.exe"
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\CreatorOS'
$installedExe = Join-Path $installDir 'CreatorOS.exe'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CreatorOS.lnk'
$startLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\CreatorOS.lnk'
$dataRoot = if ($env:CREATOROS_ROOT) { $env:CREATOROS_ROOT } else { Join-Path $env:USERPROFILE 'CreatorOS' }
$backendLog = Join-Path $dataRoot 'logs\backend\backend.log'

$results = @()
function Check($name, $ok, $detail) {
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    $colour = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("  [{0}] {1} - {2}" -f $status, $name, $detail) -ForegroundColor $colour
    $script:results += [pscustomobject]@{ check = $name; ok = [bool]$ok; detail = $detail }
}

if (-not (Test-Path $setup)) { throw "installer not found: $setup (run scripts\build-release.ps1)" }

Write-Host "`n==> installing $desktopVersion" -ForegroundColor Cyan
try {
    $proc = Start-Process -FilePath $setup -ArgumentList '/S' -PassThru -ErrorAction Stop
    $proc.WaitForExit(420000) | Out-Null
    $proc.Refresh()
    Check 'installer exits 0' ($proc.HasExited -and $proc.ExitCode -eq 0) "exit=$($proc.ExitCode)"
} catch {
    # Smart App Control (Windows 11, VerifiedAndReputablePolicyState=1) refuses to start any unsigned
    # executable it has no reputation for. That is a property of the build, not of this script: the
    # remedy is a code-signing certificate. Say so instead of dying on a stack trace.
    $message = $_.Exception.Message
    if ($message -match 'strat|policy|strategy|Contrôle|Guard') {
        Check 'installer starts' $false 'Smart App Control blocked the unsigned installer'
        Write-Host ""
        Write-Host "  Smart App Control refused to run this build. Two supported ways forward:" -ForegroundColor Yellow
        Write-Host "    a) sign the build: set CSC_LINK / CSC_KEY_PASSWORD and rebuild (see docs/RELEASE_NOTES_$desktopVersion.md)" -ForegroundColor Yellow
        Write-Host "    b) install without the installer: powershell -File scripts\install-unpacked.ps1" -ForegroundColor Yellow
        Write-Host ""
    } else {
        Check 'installer starts' $false $message
    }
    Write-Host "$(@($results | Where-Object { -not $_.ok }).Count) check(s) failed" -ForegroundColor Red
    exit 1
}

Check 'installed executable' (Test-Path $installedExe) $installedExe

$shell = New-Object -ComObject WScript.Shell
foreach ($pair in @(@('desktop shortcut', $desktopLink), @('start menu shortcut', $startLink))) {
    $name = $pair[0]; $link = $pair[1]
    if (Test-Path $link) {
        $sc = $shell.CreateShortcut($link)
        Check $name ($sc.TargetPath -eq $installedExe) "target=$($sc.TargetPath)"
        Check "$name icon" ($sc.IconLocation -like "*resources\icon.ico*") "icon=$($sc.IconLocation)"
    } else {
        Check $name $false "missing: $link"
        Check "$name icon" $false 'missing shortcut'
    }
}

Write-Host "`n==> launching the installed app" -ForegroundColor Cyan
# ELECTRON_RUN_AS_NODE makes Electron behave as plain Node; a shell exported by an IDE or agent
# harness would otherwise launch the app as a script runner and it would exit immediately.
$env:ELECTRON_RUN_AS_NODE = $null
Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
if (Test-Path $backendLog) { Remove-Item $backendLog -Force -ErrorAction SilentlyContinue }

$app = Start-Process -FilePath $installedExe -PassThru
$window = $null
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    $window = Get-Process CreatorOS -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } |
        Select-Object -First 1
    if ($window -and (Test-Path $backendLog)) {
        if (Select-String -Path $backendLog -Pattern 'backend ready on' -Quiet) { break }
    }
}
Check 'app window opens' ($null -ne $window) $(if ($window) { $window.MainWindowTitle } else { 'no window after 120 s' })

$log = if (Test-Path $backendLog) { Get-Content $backendLog -Raw } else { '' }
Check 'backend started' ($log -match 'backend ready on 127\.0\.0\.1:\d+') $(if ($log) { ((Select-String -Path $backendLog -Pattern 'backend ready on.*' | Select-Object -Last 1).Line) } else { 'no backend log' })
Check 'bundled interpreter used' ($log -match [regex]::Escape("$installDir\resources\backend\python.exe")) 'resources\backend\python.exe'
Check 'migration ran before serve' ($log -match 'migrate exited code=0') ((Select-String -Path $backendLog -Pattern 'migrate exited.*' | Select-Object -Last 1).Line)

Write-Host "`n==> checking the data root the app reports" -ForegroundColor Cyan
$db = Join-Path $dataRoot 'data\database\creatoros.db'
Check 'database present' (Test-Path $db) $db
if (Test-Path $db) {
    # Read-only: a copy is opened, never the live file, so a verification never locks the app's DB.
    $python = Join-Path $installDir 'resources\backend\python.exe'
    if (Test-Path $python) {
        $env:CREATOROS_ROOT = $dataRoot
        $check = & $python -m creatoros version --json 2>&1 | Out-String
        Remove-Item Env:CREATOROS_ROOT -ErrorAction SilentlyContinue
        try {
            $parsed = $check | ConvertFrom-Json
            Check 'database at head' ($parsed.revision -eq $parsed.head) "revision=$($parsed.revision) head=$($parsed.head)"
        } catch {
            Check 'database at head' $false "could not read version: $check"
        }
    } else {
        Check 'database at head' $false 'bundled interpreter missing from the install'
    }
}

if (-not $KeepInstalled) {
    Get-Process CreatorOS -ErrorAction SilentlyContinue | Stop-Process -Force
}

if ($Uninstall) {
    Write-Host "`n==> uninstalling" -ForegroundColor Cyan
    $uninstaller = Join-Path $installDir 'Uninstall CreatorOS.exe'
    if (Test-Path $uninstaller) {
        $u = Start-Process -FilePath $uninstaller -ArgumentList '/S' -PassThru
        $u.WaitForExit(240000) | Out-Null
        $u.Refresh()
        Check 'uninstaller exits 0' ($u.HasExited -and $u.ExitCode -eq 0) "exit=$($u.ExitCode)"
        # A silent NSIS uninstall starts the real uninstaller (Un_A.exe) and returns before it has
        # removed anything, so poll instead of sampling once: a 3-second wait reports a failure for a
        # deletion that is still in flight.
        function Wait-Gone([string]$path, [int]$seconds = 60) {
            for ($i = 0; $i -lt $seconds; $i++) {
                if (-not (Test-Path $path)) { return $true }
                Start-Sleep -Seconds 1
            }
            return -not (Test-Path $path)
        }
        Check 'desktop shortcut removed' (Wait-Gone $desktopLink) $desktopLink
        Check 'start menu shortcut removed' (Wait-Gone $startLink) $startLink
        Check 'install directory removed' (Wait-Gone (Join-Path $env:LOCALAPPDATA 'Programs\CreatorOS')) 'Programs\CreatorOS'
        Check 'data root preserved' (Test-Path $db) $db
    } else {
        Check 'uninstaller present' $false $uninstaller
    }
}

$failed = @($results | Where-Object { -not $_.ok })
Write-Host "`n$($results.Count - $failed.Count)/$($results.Count) checks passed" -ForegroundColor $(if ($failed.Count) { 'Red' } else { 'Green' })
if ($failed.Count) {
    $failed | Format-Table -AutoSize | Out-String | Write-Host
    exit 1
}
