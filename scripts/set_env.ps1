<#
CreatorOS Studio — D-drive environment configuration  (Phase 1 / P1.T08)
Applies ONLY safe user-scope environment variables that keep caches, models and
tooling on D:. It deliberately does NOT change the machine-wide TEMP/TMP
variables (see ADR-05): a global TEMP redirect breaks Windows installers and
unrelated applications. Child processes (FFmpeg, whisper, pip) receive a
per-process TEMP pointing at <data root>\temp instead.

Usage:
  powershell -ExecutionPolicy Bypass -File set_env.ps1            # dry run (shows diff)
  powershell -ExecutionPolicy Bypass -File set_env.ps1 -Apply     # apply
  powershell -ExecutionPolicy Bypass -File set_env.ps1 -Revert    # restore previous values
#>
[CmdletBinding()]
param(
  [switch]$Apply,
  [switch]$Revert,
  [string]$Root = (Join-Path $env:USERPROFILE 'CreatorOS')
)

$ErrorActionPreference = 'Stop'
$backupPath = Join-Path $Root 'config\env_backup.json'

$vars = [ordered]@{
  CREATOROS_ROOT          = $Root
  OLLAMA_MODELS           = "$Root\models\ollama"
  PIP_CACHE_DIR           = "$Root\cache\pip"
  HF_HOME                 = "$Root\cache\huggingface"
  npm_config_cache        = "$Root\cache\npm"
  NPM_CONFIG_PREFIX       = "$Root\tools\npm-global"
  PNPM_HOME               = "$Root\tools\pnpm"
  ELECTRON_CACHE          = "$Root\cache\electron"
  ELECTRON_BUILDER_CACHE  = "$Root\cache\electron-builder"
}

$pathEntries = @(
  "$Root\tools\ffmpeg\bin",
  "$Root\tools\npm-global",
  "$Root\tools\pnpm"
)

function Get-UserVar($name) { [Environment]::GetEnvironmentVariable($name, 'User') }
function Set-UserVar($name, $value) { [Environment]::SetEnvironmentVariable($name, $value, 'User') }

if ($Revert) {
  if (-not (Test-Path $backupPath)) { throw "No backup found at $backupPath - cannot revert." }
  $backup = Get-Content $backupPath -Raw | ConvertFrom-Json
  foreach ($p in $backup.variables.PSObject.Properties) {
    Set-UserVar $p.Name $p.Value
    Write-Host ("restored {0} = {1}" -f $p.Name, $p.Value)
  }
  Set-UserVar 'PATH' $backup.path
  Write-Host 'PATH restored.'
  Write-Host 'Close and reopen terminals (and Explorer) for changes to take effect.'
  return
}

Write-Host "CreatorOS environment plan (root: $Root)"
Write-Host ('-' * 78)

$changes = @()
foreach ($kv in $vars.GetEnumerator()) {
  $current = Get-UserVar $kv.Key
  $mark = if ($current -eq $kv.Value) { 'unchanged' } elseif ([string]::IsNullOrEmpty($current)) { 'new' } else { "changed (was $current)" }
  Write-Host ("{0,-24} = {1,-52} [{2}]" -f $kv.Key, $kv.Value, $mark)
  if ($current -ne $kv.Value) { $changes += $kv.Key }
}

$userPath = Get-UserVar 'PATH'
if ([string]::IsNullOrEmpty($userPath)) { $userPath = '' }
$missingPath = @()
foreach ($e in $pathEntries) { if (-not ($userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $e.TrimEnd('\') })) { $missingPath += $e } }
Write-Host ('-' * 78)
if ($missingPath.Count) { $missingPath | ForEach-Object { Write-Host "PATH will gain: $_" } } else { Write-Host 'PATH already contains all D: tool folders.' }
Write-Host 'TEMP/TMP are intentionally NOT modified (ADR-05).'
Write-Host ('-' * 78)

if (-not $Apply) {
  Write-Host 'DRY RUN - nothing changed. Re-run with -Apply to write these values.'
  return
}

# --- back up anything we are about to change, once ---
if (-not (Test-Path $backupPath)) {
  $snapshot = [ordered]@{ variables = @{}; path = $userPath; taken_at = (Get-Date).ToUniversalTime().ToString('o') }
  foreach ($name in $vars.Keys) { $snapshot.variables[$name] = Get-UserVar $name }
  New-Item -ItemType Directory -Force -Path (Split-Path $backupPath) | Out-Null
  $snapshot | ConvertTo-Json -Depth 5 | Set-Content -Path $backupPath -Encoding UTF8
  Write-Host "Backup written: $backupPath"
}

foreach ($kv in $vars.GetEnumerator()) { Set-UserVar $kv.Key $kv.Value }

if ($missingPath.Count) {
  $newPath = (@($userPath.TrimEnd(';')) + $missingPath) -join ';'
  Set-UserVar 'PATH' $newPath
  Write-Host 'PATH updated (appended, nothing removed).'
}

Write-Host ''
Write-Host 'Applied. Now:'
Write-Host '  1) close and reopen every terminal / VS Code window'
Write-Host '  2) restart the Ollama app if it is running (so OLLAMA_MODELS takes effect)'
Write-Host '  3) run scripts\diagnose.ps1 to verify'
