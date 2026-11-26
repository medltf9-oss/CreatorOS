<#
CreatorOS Studio - bootstrap (Phase 1 / P1.T13)

Idempotent setup for a machine that should run CreatorOS entirely from one data root.
Safe to re-run: every step checks before acting.

  powershell -ExecutionPolicy Bypass -File bootstrap.ps1            # report only (default)
  powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -Apply     # make changes
  powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -SkipEnv   # leave env vars alone

Does NOT install Node/Python/git (system prerequisites) and does not change
machine-wide TEMP/TMP (ADR-05).
#>
[CmdletBinding()]
param(
  [switch]$Apply,
  [switch]$SkipEnv,
  [switch]$SkipVerify,
  [string]$Root = (Join-Path $env:USERPROFILE 'CreatorOS')
)

$ErrorActionPreference = 'Stop'
function Step($msg) { Write-Host "== $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "   $msg" -ForegroundColor Gray }
function Act($msg) { if ($Apply) { Write-Host "   + $msg" -ForegroundColor Green } else { Write-Host "   would: $msg" -ForegroundColor DarkGray } }

if (-not $Apply) { Write-Host 'REPORT MODE - nothing will be changed. Re-run with -Apply.' -ForegroundColor Yellow }

Step "1/7 verify data root $Root"
if (-not (Test-Path $Root)) {
  Act "create $Root"
  if ($Apply) { New-Item -ItemType Directory -Force -Path $Root | Out-Null }
} else { Info 'exists' }

$folders = @(
  'app\desktop', 'app\backend', 'app\shared', 'app\installer',
  'data\database', 'data\media\incoming', 'data\media\working', 'data\media\exports',
  'data\media\thumbnails', 'data\media\subtitles', 'data\media\archive',
  'data\agent_memory', 'data\backups',
  'models\ollama', 'tools\ffmpeg\bin', 'tools\pnpm', 'tools\npm-global', 'Programs\Ollama',
  'cache\pip', 'cache\huggingface', 'cache\npm', 'cache\pnpm-store', 'cache\electron',
  'cache\electron-builder', 'logs\backend', 'logs\desktop', 'temp', 'config\runtime',
  'config\secrets', 'docs\ADR', 'docs\evidence', 'installer_builds', 'scripts'
)
Step '2/7 folder tree'
$missing = @()
foreach ($f in $folders) {
  $p = Join-Path $Root $f
  if (-not (Test-Path $p)) { $missing += $f; Act "mkdir $f"; if ($Apply) { New-Item -ItemType Directory -Force -Path $p | Out-Null } }
}
Info ("{0} of {1} folders present" -f ($folders.Count - $missing.Count), $folders.Count)

Step '3/7 write marker + free space guard'
if ($Apply) {
  $marker = Join-Path $Root '.creatoros-root'
  if (-not (Test-Path $marker)) {
    @{ root = $Root; version = '0.1.0'; created_at = (Get-Date).ToUniversalTime().ToString('o') } |
      ConvertTo-Json | Set-Content -Path $marker -Encoding UTF8
  }
  $drive = Get-PSDrive -Name ($Root.Substring(0, 1))
  if (($drive.Free / 1GB) -lt 20) { throw ("Only {0:N1} GB free on {1}: - refusing to continue" -f ($drive.Free / 1GB), $drive.Name) }
}
Info 'ok'

Step '4/7 environment variables'
if ($SkipEnv) { Info 'skipped' } else {
  $envScript = Join-Path $Root 'scripts\set_env.ps1'
  if (-not (Test-Path $envScript)) { Info "missing $envScript - copy it from the repo first" }
  elseif ($Apply) { & $envScript -Apply -Root $Root }
  else { & $envScript -Root $Root }
}

Step '5/7 backend virtualenv'
$venvPython = Join-Path $Root 'app\backend\.venv\Scripts\python.exe'
if (Test-Path $venvPython) { Info ((& $venvPython -V) -join ' ') }
else {
  Act 'create .venv and install backend dependencies'
  if ($Apply) {
    Push-Location (Join-Path $Root 'app\backend')
    try {
      py -3.12 -m venv .venv
      & .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
      # The committed lock pins the exact runtime closure, so a clean clone builds the same
      # environment as the release machine. Editable install brings only first-party code.
      & .\.venv\Scripts\python.exe -m pip install -r requirements-runtime.lock -q
      & .\.venv\Scripts\python.exe -m pip install --no-deps -e . -q
    } finally { Pop-Location }
  }
}

Step '6/7 desktop dependencies'
$electron = Join-Path $Root 'app\desktop\node_modules\electron\dist\electron.exe'
if (Test-Path $electron) { Info 'electron binary present' }
else {
  Act 'pnpm install + pnpm rebuild electron'
  if ($Apply) {
    Push-Location (Join-Path $Root 'app\desktop')
    try {
      $pnpm = Join-Path $Root 'tools\npm-global\pnpm.cmd'
      if (-not (Test-Path $pnpm)) { $pnpm = 'pnpm' }
      & $pnpm install
      & $pnpm rebuild electron
    } finally { Pop-Location }
  }
}

Step '7/7 diagnostics'
if ($SkipVerify) { Info 'skipped' } else {
  $diag = Join-Path $Root 'scripts\diagnose.ps1'
  if (Test-Path $diag) { & $diag -Root $Root } else { Info 'diagnose.ps1 missing' }
}

Write-Host ''
Write-Host 'Next: cd <PROJECT_ROOT>\app\desktop ; pnpm dev' -ForegroundColor Cyan

