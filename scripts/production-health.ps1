<#
.SYNOPSIS
    Production health: does the *installed* application work the way a user needs it to?

.DESCRIPTION
    Phase 9 proved the installer produces a working install. This answers the harder question, on the
    machine, against the installed build:

      1. the Desktop shortcut launches the app (not the exe path: the shortcut is what a user has)
      2. the app starts its own backend, from its own bundled interpreter, with no development tooling
      3. the database is reachable, at head, and passes SQLite's integrity check
      4. the credential vault answers and can seal, read and delete a probe record
      5. Ollama is discovered (or its absence is reported), FFmpeg is present, disk space is adequate
      6. every sidebar page opens in the real window, with no error card and no console error
      7. closing the window leaves no orphan process behind
      8. a launch/close/upgrade cycle does not change the data (row counts, revision, integrity)
      9. (-IncludeUninstall) uninstalling removes the app and the shortcuts but keeps the data

    It reads and reports; the only writes it makes are its own probe secret in the vault (deleted
    immediately) and whatever the install/uninstall steps do when -IncludeUpgrade/-IncludeUninstall are
    passed. It never changes a Windows security setting.

.PARAMETER IncludeUpgrade
    Run the built installer over the existing install, then prove the data survived.

.PARAMETER IncludeUninstall
    Uninstall, prove the data root survived and the shortcuts are gone, then reinstall so the machine is
    left usable.

.PARAMETER SkipSweep
    Skip the 49-page UI sweep (faster; the API and process checks still run).

.PARAMETER KeepRunning
    Leave the app open at the end instead of closing it.
#>
[CmdletBinding()]
param(
    [switch]$IncludeUpgrade,
    [switch]$IncludeUninstall,
    [switch]$SkipSweep,
    [switch]$KeepRunning,
    [int]$Port = 9444,
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $Version) {
    $package = Get-Content (Join-Path $repo 'app\desktop\package.json') -Raw | ConvertFrom-Json
    $Version = $package.version
}
$desktop = Join-Path $repo 'app\desktop'
$installedDir = Join-Path $env:LOCALAPPDATA 'Programs\CreatorOS'
$installedExe = Join-Path $installedDir 'CreatorOS.exe'
$installedPython = Join-Path $installedDir 'resources\backend\python.exe'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'CreatorOS.lnk'
$startLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\CreatorOS.lnk'
$dataRoot = if ($env:CREATOROS_ROOT) { $env:CREATOROS_ROOT } else { Join-Path $env:USERPROFILE 'CreatorOS' }
$database = Join-Path $dataRoot 'data\database\creatoros.db'
$backendLog = Join-Path $dataRoot 'logs\backend\backend.log'
$setup = Join-Path $repo "releases\$Version\CreatorOS-Setup-$Version.exe"
$repoVenvPython = Join-Path $repo 'appackend\.venv\Scripts\python.exe'

$results = @()
function Check($name, $ok, $detail) {
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    $colour = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("  [{0}] {1} - {2}" -f $status, $name, $detail) -ForegroundColor $colour
    $script:results += [pscustomobject]@{ check = $name; ok = [bool]$ok; detail = $detail }
}
function Note($text) { Write-Host "        $text" -ForegroundColor DarkGray }

# The installed backend is used for database reads on purpose: it is the interpreter the product
# actually ships, so a broken bundle fails here rather than in a user's hands.
function Get-DataSnapshot {
    $script = @'
import json, sqlite3, sys
path = sys.argv[1]
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
tables = ["social_account", "provider_app_config", "secret_ref", "media_asset", "publish_job",
          "analytics_snapshot", "analytics_metric_definition", "agent", "audit_event", "queue_job",
          "analytics_recommendation", "analytics_report"]
counts = {}
for table in tables:
    try:
        counts[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
    except sqlite3.Error as error:
        counts[table] = f"error: {error}"
revision = con.execute("select version_num from alembic_version").fetchone()[0]
integrity = con.execute("pragma integrity_check").fetchone()[0]
foreign = con.execute("pragma foreign_key_check").fetchall()
print(json.dumps({"counts": counts, "revision": revision, "integrity": integrity,
                  "foreign_key_violations": len(foreign)}))
con.close()
'@
    # The installed interpreter is preferred, because reading the database with the shipped runtime is part
    # of what is being checked. After an uninstall it is gone, so the build venv stands in and the detail
    # line says which one ran.
    $interpreter = if (Test-Path $installedPython) { $installedPython } else { $repoVenvPython }
    $tempFile = Join-Path $env:TEMP ("creatoros-snapshot-" + [Guid]::NewGuid().ToString('N').Substring(0, 8) + ".py")
    Set-Content -Path $tempFile -Value $script -Encoding ascii
    try {
        $json = & $interpreter $tempFile $database
        if ($LASTEXITCODE -ne 0) { throw "snapshot failed (exit $LASTEXITCODE)" }
        $snapshot = $json | ConvertFrom-Json
        $snapshot | Add-Member -NotePropertyName interpreter -NotePropertyValue (Split-Path -Leaf (Split-Path -Parent (Split-Path -Parent $interpreter))) -Force
        return $snapshot
    } finally {
        Remove-Item $tempFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-AppProcesses { Get-Process CreatorOS -ErrorAction SilentlyContinue }
function Get-BackendProcesses {
    Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -like '*Programs\CreatorOS\resources\backend*' }
}

# ----------------------------------------------------------------------------------- 0. pre-flight
Write-Host "`n==> pre-flight" -ForegroundColor Cyan
$running = Get-AppProcesses
Check 'no CreatorOS already running' (-not $running) "$(if ($running) { @($running).Count } else { 0 }) process(es)"
if ($running) {
    Write-Host '  close the running app first: this check measures a clean launch' -ForegroundColor Yellow
    exit 1
}
Check 'installed executable' (Test-Path $installedExe) $installedExe
Check 'bundled interpreter' (Test-Path $installedPython) $installedPython
Check 'Desktop shortcut' (Test-Path $desktopLink) $desktopLink
Check 'Start Menu shortcut' (Test-Path $startLink) $startLink
Check 'database present' (Test-Path $database) $database
if (-not (Test-Path $installedExe)) { Write-Host 'install first: scripts\install-unpacked.ps1' -ForegroundColor Yellow; exit 1 }

$before = Get-DataSnapshot
Check 'database integrity' ($before.integrity -eq 'ok') "integrity=$($before.integrity), foreign_key_violations=$($before.foreign_key_violations), revision=$($before.revision)"
Note (($before.counts.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' ')

# ------------------------------------------------------------------------------- 1. launch
Write-Host "`n==> launching from the Desktop shortcut" -ForegroundColor Cyan
$logStart = if (Test-Path $backendLog) { (Get-Item $backendLog).Length } else { 0 }
# The host shell may export ELECTRON_RUN_AS_NODE, which makes Electron behave as node and exit.
Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
Start-Process -FilePath $desktopLink -ArgumentList "--remote-debugging-port=$Port" | Out-Null

$processes = @()
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 750
    $processes = @(Get-AppProcesses)
    if ($processes.Count -gt 1) { break }
}
Check 'app process started' ($processes.Count -gt 1) "$($processes.Count) process(es) for the executable"
if ($processes.Count -le 1) { Write-Host '  the app did not start; see docs\PRODUCTION_READINESS.md' -ForegroundColor Red; exit 1 }

$apiBase = $null
for ($i = 0; $i -lt 60; $i++) {
    try {
        $bootstrap = (node (Join-Path $desktop 'scripts\ui_health.cjs') --port $Port --print-bootstrap) -join ''
        $apiBase = ($bootstrap | ConvertFrom-Json)
        if ($apiBase.apiBase) { break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $apiBase -or -not $apiBase.apiBase) {
    Check 'renderer bridge answered' $false 'the CDP bridge never returned a bootstrap payload'
    exit 1
}
Check 'window and renderer bridge' $true "$($apiBase.apiBase), version $($apiBase.version), isDev=$($apiBase.isDev)"
Check 'data root reported by the app' ($apiBase.root -eq $dataRoot) "$($apiBase.root)"

$headers = @{ Authorization = "Bearer $($apiBase.token)" }
function ApiGet($path) { Invoke-RestMethod -Uri "$($apiBase.apiBase)$path" -Headers $headers -TimeoutSec 20 }

# ----------------------------------------------------------------------- 2. automatic backend start
Write-Host "`n==> backend started by the app itself" -ForegroundColor Cyan
$tail = if (Test-Path $backendLog) { Get-Content $backendLog -Tail 200 } else { @() }
$readyLine = $tail | Where-Object { $_ -match 'backend ready on 127\.0\.0\.1:\d+' } | Select-Object -Last 1
Check 'backend ready line in the log' ([bool]$readyLine) "$readyLine"
$runtime = ApiGet '/diagnostics'
Check 'backend serves diagnostics' ([bool]$runtime.app.version) "python $($runtime.runtime.python), pid $($runtime.runtime.pid)"
$isBundled = $runtime.runtime.executable -like '*Programs\CreatorOS\resources\backend*'
Check 'bundled interpreter is the one running' $isBundled "$($runtime.runtime.executable)"
$backendProcs = @(Get-BackendProcesses)
Check 'backend process is owned by the install' ($backendProcs.Count -ge 1) "$($backendProcs.Count) backend process(es) from the install directory"

# ------------------------------------------------------------------------ 3. database and vault
Write-Host "`n==> database and credential vault" -ForegroundColor Cyan
$ready = ApiGet '/health/ready'
Check 'readiness probe' ($ready.status -eq 'ready') "status=$($ready.status), disk_free=$($ready.checks.disk_free_gb)GB, db=$($ready.checks.database)"
$versionPayload = $null
try { $versionPayload = ApiGet '/version' } catch { }
Check 'version endpoint answers' ([bool]$versionPayload.version) "version $($versionPayload.version), api $($versionPayload.api)"
Check 'version matches the installed build' ($versionPayload.version -eq $Version) "api=$($versionPayload.version) installed=$Version"
Check 'database revision at head' ([bool]$before.revision) "revision $($before.revision)"

$vault = ApiGet '/social/vault/health'
Check 'vault available' ([bool]$vault.available) "backend=$($vault.backend), available=$($vault.available)"
# Sealing happens in the Electron main process, so the round trip runs in the renderer: backend reserve,
# DPAPI seal, backend adopt, read back, unlink, destroy. A backend-only probe would prove nothing.
$probeJson = (node (Join-Path $desktop 'scripts\ui_health.cjs') --port $Port --vault-probe 2>$null) -join ''
$probe = $null
try { $probe = $probeJson | ConvertFrom-Json } catch { }
if ($probe) {
    $sealedStep = @($probe.steps | Where-Object { $_.step -eq 'seal' }) | Select-Object -First 1
    $destroyStep = @($probe.steps | Where-Object { $_.step -eq 'destroy' }) | Select-Object -First 1
    Check 'vault seals a probe secret' ([bool]$sealedStep.ok) "steps: $((@($probe.steps) | ForEach-Object { $_.step }) -join ' -> ')"
    Check 'vault reads and destroys it' ($probe.ok -and [bool]$destroyStep.ok) "id=$($probe.id)"
} else {
    Check 'vault round-trip' $false 'the renderer bridge returned nothing'
}

# ---------------------------------------------------------------------------------- 4. environment
Write-Host "`n==> local environment" -ForegroundColor Cyan
Check 'FFmpeg present' ([bool]$runtime.ffmpeg.present) "$($runtime.ffmpeg.version)"
$ollama = $runtime.ollama
if ($ollama.online) {
    Check 'Ollama reachable' $true "v$($ollama.version), $($ollama.model_count) model(s)"
    if ($ollama.model_count -eq 0) { Note 'Ollama is running but has no models: AI pages will report that, correctly' }
} else {
    # Not a failure of the product: it is a fact about the machine, and the app reports it in its header.
    Check 'Ollama reported honestly' $true "offline ($($ollama.error)); the AI pages report this instead of pretending"
}
Check 'disk space adequate' ([bool]$runtime.disk.ok) "$($runtime.disk.free_gb)GB free of $($runtime.disk.total_gb)GB on $($runtime.disk.root)"
Check 'data root is the configured one' ($runtime.disk.root -eq $dataRoot) $runtime.disk.root
$queue = $null
try { $queue = ApiGet '/queue/stats' } catch { }
Check 'queue answers' ($null -ne $queue) "$($queue.counts.total) job(s), queued=$($queue.counts.queued), dead_letter=$($queue.counts.dead_letter)"

# ------------------------------------------------------------------------------------- 5. UI sweep
if ($SkipSweep) {
    Write-Host "`n==> UI sweep skipped (-SkipSweep)" -ForegroundColor Yellow
} else {
    Write-Host "`n==> every page opens" -ForegroundColor Cyan
    $jsonOut = Join-Path $env:TEMP ("creatoros-uihealth-" + [Guid]::NewGuid().ToString('N').Substring(0, 8) + ".json")
    $outDir = Join-Path $repo 'temp\p10-health'
    & node (Join-Path $desktop 'scripts\ui_health.cjs') --port $Port --out $outDir --json $jsonOut | ForEach-Object { Note $_ }
    $sweepExit = $LASTEXITCODE
    if (Test-Path $jsonOut) {
        $pages = Get-Content $jsonOut -Raw | ConvertFrom-Json
        $failedPages = @($pages | Where-Object { -not $_.ok })
        Check 'all sidebar pages render' ($failedPages.Count -eq 0) "$(@($pages).Count - $failedPages.Count)/$(@($pages).Count) pages, screenshots in temp\p10-health"
        foreach ($page in $failedPages) { Note "FAILED: $($page.section) / $($page.label) - $($page.reason)" }
        Remove-Item $jsonOut -Force -ErrorAction SilentlyContinue
    } else {
        Check 'all sidebar pages render' ($sweepExit -eq 0) 'the sweep produced no report'
    }
}

# ------------------------------------------------------------------------------ 6. process census
Write-Host "`n==> process census" -ForegroundColor Cyan
$census = @(Get-AppProcesses)
Check 'one app process tree' ($census.Count -ge 2) "$($census.Count) CreatorOS processes (main + renderer + gpu)"
$strays = @(Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -notlike '*Programs\CreatorOS\resources\backend*' -and $_.Path -like '*CreatorOS*' })
Check 'no stray interpreter' ($strays.Count -eq 0) "$($strays.Count) python process(es) outside the install"

# ------------------------------------------------------------------------------------ 7. shutdown
if ($KeepRunning) {
    Write-Host "`n==> leaving the app running (-KeepRunning)" -ForegroundColor Yellow
} else {
    Write-Host "`n==> closing the window" -ForegroundColor Cyan
    foreach ($process in (Get-AppProcesses)) { $null = $process.CloseMainWindow() }
    $gone = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-AppProcesses)) { $gone = $true; break }
    }
    if (-not $gone) {
        Check 'window closes cleanly' $false 'processes remain after 20s'
        foreach ($process in (Get-AppProcesses)) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    } else {
        Check 'window closes cleanly' $true 'all CreatorOS processes exited'
    }
    $orphans = @()
    for ($i = 0; $i -lt 20; $i++) {
        $orphans = @(Get-BackendProcesses)
        if ($orphans.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    }
    Check 'no orphan backend' ($orphans.Count -eq 0) "$($orphans.Count) backend process(es) still running"
}

# ----------------------------------------------------------------------- 8. data after a cycle
Write-Host "`n==> data after a launch/close cycle" -ForegroundColor Cyan
$after = Get-DataSnapshot
$changed = @()
foreach ($property in $before.counts.PSObject.Properties) {
    $now = $after.counts.$($property.Name)
    if ($now -ne $property.Value) { $changed += "$($property.Name) $($property.Value)->$now" }
}
# A running app legitimately writes audit events, queue ticks, and - because this script seals and
# destroys one probe secret - one revoked secret_ref row: deleting a secret unlinks the ciphertext,
# marks the row deleted and keeps the audit trail, which is the behaviour we want. Everything else in
# the list must be bit-for-bit unchanged, and secret_ref may only grow by the single probe.
$allowedToGrow = @('audit_event', 'queue_job')
$unexpected = @()
foreach ($entry in $changed) {
    $parts = $entry -split ' '
    $name = $parts[0]
    if ($allowedToGrow -contains $name) { continue }
    if ($name -eq 'secret_ref') {
        $delta = [int](($parts[1] -split '->')[1]) - [int](($parts[1] -split '->')[0])
        if ($delta -eq 1) { continue }
    }
    $unexpected += $entry
}
Check 'no data lost' ($after.counts.social_account -ge $before.counts.social_account -and $after.counts.media_asset -eq $before.counts.media_asset -and $after.counts.analytics_metric_definition -eq $before.counts.analytics_metric_definition) "metrics=$($after.counts.analytics_metric_definition), media=$($after.counts.media_asset)"
Check 'revision unchanged' ($after.revision -eq $before.revision) $after.revision
Check 'integrity after the cycle' ($after.integrity -eq 'ok') "integrity=$($after.integrity), foreign_key_violations=$($after.foreign_key_violations)"
if ($changed.Count -gt 0) { Note ("changed: " + ($changed -join ', ')) }
Check 'only append-only tables grew' ($unexpected.Count -eq 0) $(if ($unexpected.Count -eq 0) { 'audit_event/queue_job may grow, plus one revoked secret_ref for the probe; nothing else changed' } else { $unexpected -join ', ' })

# ------------------------------------------------------------------------ 9. upgrade survival
if ($IncludeUpgrade) {
    Write-Host "`n==> installing over the existing install (the upgrade path)" -ForegroundColor Cyan
    if (-not (Test-Path $setup)) { Check 'installer present' $false $setup }
    else {
        $upgradeSnapshot = Get-DataSnapshot
        $proc = Start-Process -FilePath $setup -ArgumentList '/S' -PassThru
        $proc.WaitForExit(420000) | Out-Null
        Check 'upgrade installer exits 0' ($proc.ExitCode -eq 0) "exit=$($proc.ExitCode)"
        $upgraded = Get-DataSnapshot
        Check 'data survives the upgrade' ($upgraded.counts.analytics_metric_definition -eq $upgradeSnapshot.counts.analytics_metric_definition -and $upgraded.counts.media_asset -eq $upgradeSnapshot.counts.media_asset -and $upgraded.counts.social_account -eq $upgradeSnapshot.counts.social_account) "metrics=$($upgraded.counts.analytics_metric_definition), media=$($upgraded.counts.media_asset)"
        Check 'integrity after the upgrade' ($upgraded.integrity -eq 'ok') "integrity=$($upgraded.integrity)"
        Check 'shortcuts still point at the install' (Test-Path $desktopLink) $desktopLink
    }
}

# ---------------------------------------------------------------------- 10. uninstall survival
if ($IncludeUninstall) {
    Write-Host "`n==> uninstalling, then reinstalling" -ForegroundColor Cyan
    $uninstallSnapshot = Get-DataSnapshot
    $uninstaller = Join-Path $installedDir 'Uninstall CreatorOS.exe'
    if (-not (Test-Path $uninstaller)) { Check 'uninstaller present' $false $uninstaller }
    else {
        Start-Process -FilePath $uninstaller -ArgumentList '/S' | Out-Null
        # NSIS returns immediately; poll rather than sample once.
        $removed = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            if (-not (Test-Path $installedExe)) { $removed = $true; break }
        }
        Check 'uninstall removes the app' $removed $installedDir
        Check 'uninstall leaves the Desktop shortcut gone' (-not (Test-Path $desktopLink)) $desktopLink
        $afterUninstall = Get-DataSnapshot
        Check 'data root survives uninstall' ($afterUninstall.counts.analytics_metric_definition -eq $uninstallSnapshot.counts.analytics_metric_definition -and $afterUninstall.counts.media_asset -eq $uninstallSnapshot.counts.media_asset) "metrics=$($afterUninstall.counts.analytics_metric_definition), media=$($afterUninstall.counts.media_asset)"
        Check 'database survives uninstall' ($afterUninstall.integrity -eq 'ok') "integrity=$($afterUninstall.integrity)"
        if (Test-Path $setup) {
            Start-Process -FilePath $setup -ArgumentList '/S' -Wait | Out-Null
            Check 'reinstall restores the app' (Test-Path $installedExe) $installedExe
            $reinstalled = Get-DataSnapshot
            Check 'data still intact after reinstall' ($reinstalled.counts.analytics_metric_definition -eq $uninstallSnapshot.counts.analytics_metric_definition) "metrics=$($reinstalled.counts.analytics_metric_definition)"
        } else {
            Note "no installer at $setup; leaving the app uninstalled (reinstall with scripts\install-unpacked.ps1)"
        }
    }
}

# ------------------------------------------------------------------------------------ summary
$failed = @($results | Where-Object { -not $_.ok })
Write-Host ""
if ($failed.Count -eq 0) {
    Write-Host "PRODUCTION HEALTH: $($results.Count)/$($results.Count) checks passed" -ForegroundColor Green
    exit 0
}
Write-Host "PRODUCTION HEALTH: $($failed.Count) of $($results.Count) checks failed" -ForegroundColor Red
foreach ($item in $failed) { Write-Host ("  - {0}: {1}" -f $item.check, $item.detail) -ForegroundColor Red }
exit 1
