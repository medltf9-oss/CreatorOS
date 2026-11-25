# Start (or repair) the local Ollama daemon for CreatorOS: per-user, models under the data root.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_ollama.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_ollama.ps1 -Status
#
# Why this file verifies instead of just starting: the Windows Ollama *app* stores its own model path in
# its settings database and that value wins over the OLLAMA_MODELS environment variable. Measured on this
# machine (2026-11-24): the app had `models = D:\NEXUS_DATA\models` left over from an earlier install, so
# the daemon reported 0 models while 9.3 GB of models sat in <data root>\models\ollama. `ollama list`
# looked broken, the AI pages had nothing to route to, and nothing in the product could fix it.
#
# The repair is to run `ollama serve` with OLLAMA_MODELS set *for the server process*, which wins over the
# app's stored setting. The app's autostart can then be corrected once in its own settings UI (or the
# stored value changed with scripts\fix_ollama_path.ps1) so a reboot does not undo this.
param(
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

$root = if ($env:CREATOROS_ROOT) { $env:CREATOROS_ROOT } else { Join-Path $env:USERPROFILE 'CreatorOS' }
$exe = Join-Path $root 'Programs\Ollama\ollama.exe'
$models = Join-Path $root 'models\ollama'
$expected = @('qwen3:0.6b', 'qwen3:1.7b', 'qwen3:4b', 'qwen2.5-coder:3b', 'gemma3:4b', 'nomic-embed-text')

if (-not (Test-Path $exe)) {
    Write-Output "ollama.exe not found at $exe - run scripts\bootstrap.ps1 first"
    exit 1
}
if (-not (Test-Path $models)) {
    Write-Output "model directory not found: $models"
    exit 1
}

function Get-Tags {
    try {
        $response = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 4
        return @($response.models | ForEach-Object { $_.name })
    } catch {
        return @()
    }
}

function Get-Version {
    try {
        return (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3).version
    } catch {
        return $null
    }
}

$tags = Get-Tags
$version = Get-Version
$missing = @($expected | Where-Object { $expectedName = $_; -not ($tags | Where-Object { $_ -eq $expectedName -or $_ -like "$expectedName*" }) })

if ($Status) {
    Write-Output "daemon      : $(if ($version) { "running (version $version)" } else { 'not running' })"
    Write-Output "models dir  : $models ($([math]::Round((Get-ChildItem $models -Recurse -File | Measure-Object Length -Sum).Sum / 1GB, 2)) GB on disk)"
    Write-Output "visible     : $($tags.Count) model(s) $(if ($tags.Count) { '- ' + ($tags -join ', ') } else { '' })"
    if ($missing.Count) { Write-Output "not visible : $($missing -join ', ')" }
    exit 0
}

if ($version -and $missing.Count -eq 0) {
    Write-Output "ollama already serving all $($tags.Count) documented models (version $version)"
    exit 0
}

if ($version -and $missing.Count -gt 0) {
    Write-Output "ollama is running but does not see: $($missing -join ', ')"
    Write-Output "repairing: stopping the app-managed daemon and serving $models directly"
    Get-Process 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Get-Version)) { break }
    }
}

$env:OLLAMA_MODELS = $models
$env:OLLAMA_HOST = '127.0.0.1:11434'
Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden
Write-Output "started ollama serve (models: $models)"

for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if (Get-Version) { break }
}
$version = Get-Version
if (-not $version) {
    Write-Output 'ollama did not answer on 127.0.0.1:11434 within 30s'
    exit 1
}
$tags = Get-Tags
$missing = @($expected | Where-Object { $expectedName = $_; -not ($tags | Where-Object { $_ -eq $expectedName -or $_ -like "$expectedName*" }) })
if ($missing.Count) {
    Write-Output "ollama ready ($version) but still missing: $($missing -join ', ')"
    Write-Output 'the model directory is not the one being served - run: scripts\start_ollama.ps1 -Status'
    exit 1
}
Write-Output "ollama ready: $version, $($tags.Count) documented models visible"
exit 0
