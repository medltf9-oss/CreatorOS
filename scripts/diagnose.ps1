<#
CreatorOS Studio - diagnostics (Phase 1 / P1.T14)

Prints real facts about this machine and this installation. Prints no secrets.
Exit codes: 0 = all required checks pass, 1 = a required check failed, 2 = warnings only.

Usage: powershell -ExecutionPolicy Bypass -File diagnose.ps1 [-Json]
#>
[CmdletBinding()]
param([switch]$Json, [string]$Root = (Join-Path $env:USERPROFILE 'CreatorOS'))

$ErrorActionPreference = 'Continue'
$results = [ordered]@{}
$fail = 0
$warn = 0

function Check([string]$name, [scriptblock]$probe, [ValidateSet('required', 'optional')][string]$level = 'required') {
  $value = $null
  $ok = $false
  $detail = ''
  try {
    $value = & $probe
    $ok = $true
  } catch {
    $detail = $_.Exception.Message
  }
  if (-not $ok) { if ($level -eq 'required') { $script:fail++ } else { $script:warn++ } }
  $script:results[$name] = [ordered]@{ ok = $ok; level = $level; value = $value; error = $detail }
}

$exe = { param($p, $a) $out = & $p @a 2>&1; if ($LASTEXITCODE -ne 0 -and -not $out) { throw "$p failed" }; ($out | Select-Object -First 1) }

Check 'os' { (Get-CimInstance Win32_OperatingSystem).Caption + ' ' + (Get-CimInstance Win32_OperatingSystem).Version }
Check 'cpu' { (Get-CimInstance Win32_Processor).Name }
Check 'ram_gb' { [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1) }
Check 'gpu' {
  $smi = 'nvidia-smi'
  $q = & $smi '--query-gpu=name,memory.total,driver_version' '--format=csv,noheader' 2>&1
  if ($LASTEXITCODE -ne 0) { throw 'nvidia-smi unavailable' }
  $q
} 'optional'

Check 'data_root_exists' { Test-Path $Root }
Check 'data_root_writable' {
  $probe = Join-Path $Root 'temp\.diag_write_test'
  'x' | Set-Content -Path $probe -Encoding ascii
  Remove-Item $probe -Force
  'writable'
}
Check 'disk_free_gb' {
  $d = Get-PSDrive -Name ($Root.Substring(0, 1)) -ErrorAction Stop
  [math]::Round($d.Free / 1GB, 1)
}
Check 'disk_free_ok' {
  $d = Get-PSDrive -Name ($Root.Substring(0, 1))
  if (($d.Free / 1GB) -lt 20) { throw ('only {0} GB free' -f [math]::Round($d.Free / 1GB, 1)) }
  'ok'
}

Check 'python' { (& (Join-Path $Root 'app\backend\.venv\Scripts\python.exe') -V) -join ' ' }
Check 'backend_importable' {
  $py = Join-Path $Root 'app\backend\.venv\Scripts\python.exe'
  $out = & $py -c "import creatoros.main; print('creatoros import ok')" 2>&1
  if ($LASTEXITCODE -ne 0) { throw ($out -join ' ') }
  ($out -join ' ')
}
Check 'node' { (node -v) }
Check 'pnpm' {
  $p = Join-Path $Root 'tools\npm-global\pnpm.cmd'
  if (Test-Path $p) { (& $p -v) } else { (pnpm -v) }
} 'optional'
Check 'git' { (git --version) } 'optional'

Check 'ffmpeg' {
  $f = Join-Path $Root 'tools\ffmpeg\bin\ffmpeg.exe'
  if (-not (Test-Path $f)) { throw "missing: $f" }
  ((& $f -hide_banner -version | Select-Object -First 1))
}
Check 'ffprobe' {
  $f = Join-Path $Root 'tools\ffmpeg\bin\ffprobe.exe'
  if (-not (Test-Path $f)) { throw "missing: $f" }
  ((& $f -hide_banner -version | Select-Object -First 1))
}

Check 'ollama_installed' {
  $o = Join-Path $Root 'Programs\Ollama\ollama.exe'
  if (-not (Test-Path $o)) { throw "missing: $o" }
  'present'
}
Check 'ollama_api' {
  $v = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 5
  'online v' + $v.version
}
Check 'ollama_models' {
  $t = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
  'count=' + $t.models.Count
} 'optional'
Check 'ollama_models_on_D' {
  $dir = Join-Path $Root 'models\ollama\blobs'
  if (-not (Test-Path $dir)) { throw 'no blobs directory on D:' }
  $gb = [math]::Round(((Get-ChildItem $dir -Recurse -File | Measure-Object Length -Sum).Sum / 1GB), 2)
  "$gb GB in $dir"
}

# --- Phase 3: local AI infrastructure ----------------------------------------
# The probe logic lives in scripts/probe_ai.py so it can be run and tested on its own.
$probePy = Join-Path $Root 'scripts/probe_ai.py'
$venvPython = Join-Path $Root 'app/backend/.venv/Scripts/python.exe'

Check 'ai_gateway_health' {
  $env:CREATOROS_ROOT = $Root
  $out = & $venvPython $probePy gateway 2>&1
  if ($LASTEXITCODE -ne 0) { throw ($out | Select-Object -Last 1) }
  ($out | Select-Object -First 1)
}
Check 'ai_runtime_state' {
  $env:CREATOROS_ROOT = $Root
  $out = & $venvPython $probePy profiles 2>&1
  if ($LASTEXITCODE -ne 0) { throw ($out | Select-Object -Last 1) }
  ($out | Select-Object -Last 1)
}
Check 'ai_gpu_telemetry' {
  $out = & $venvPython $probePy gpu 2>&1
  if ($LASTEXITCODE -ne 0) { throw ($out | Select-Object -Last 1) }
  ($out | Select-Object -Last 1)
} 'optional'
Check 'ai_no_cloud_provider' {
  $deps = & $venvPython -m pip list --format=freeze 2>&1
  $bad = $deps | Where-Object { $_ -match '^(openai|anthropic|google-generativeai|cohere|mistralai)==' }
  if ($bad) { throw ('external AI SDK installed: ' + ($bad -join ', ')) }
  'none installed'
}

Check 'env_OLLAMA_MODELS' { [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User') }
Check 'env_PIP_CACHE_DIR' { [Environment]::GetEnvironmentVariable('PIP_CACHE_DIR', 'User') }
Check 'env_HF_HOME' { [Environment]::GetEnvironmentVariable('HF_HOME', 'User') }
Check 'env_npm_config_cache' { [Environment]::GetEnvironmentVariable('npm_config_cache', 'User') }
Check 'env_PNPM_HOME' { [Environment]::GetEnvironmentVariable('PNPM_HOME', 'User') }
Check 'temp_not_global_redirect' {
  $t = [Environment]::GetEnvironmentVariable('TEMP', 'User')
  if ($t -and $t.ToLower().StartsWith('d:')) { throw 'machine TEMP points at D: (ADR-05 violation)' }
  "ok ($t)"
}
Check 'temp_writable_on_D' {
  $p = Join-Path $Root 'temp\.diag'
  'x' | Set-Content $p -Encoding ascii; Remove-Item $p -Force; 'ok'
}

Check 'desktop_deps' {
  $nm = Join-Path $Root 'app\desktop\node_modules'
  if (-not (Test-Path $nm)) { throw 'node_modules missing' }
  $e = Join-Path $nm 'electron\dist\electron.exe'
  if (-not (Test-Path $e)) { throw 'electron binary missing (run: pnpm rebuild electron)' }
  'electron ' + (Get-Content (Join-Path $nm 'electron\package.json') -Raw | ConvertFrom-Json).version
}

# --- Phase 7: publishing readiness -------------------------------------------
# Each platform answers why it can or cannot publish, from the database, not from a guess. These are
# optional: a machine without a Meta app or a TikTok app is a normal installation, not a failure.
$publishProbe = Join-Path $Root 'scripts\publish_readiness.py'
Check 'publish_readiness' {
  $env:CREATOROS_ROOT = $Root
  $out = & $venvPython $publishProbe 2>&1
  if ($LASTEXITCODE -ne 0) { throw ($out | Select-Object -Last 1) }
  ($out | Select-Object -Last 1)
} 'optional'
Check 'publish_migration_head' {
  $env:CREATOROS_ROOT = $Root
  Push-Location (Join-Path $Root 'app\backend')
  try {
    $py = Join-Path $Root 'app\backend\.venv\Scripts\python.exe'
    $out = & $py -m alembic current 2>&1
    $line = ($out | Where-Object { $_ -match '^[0-9a-f]{12}' } | Select-Object -Last 1)
    if (-not $line) { throw 'alembic current produced no revision' }
    $line
  } finally { Pop-Location }
}
Check 'publish_live_opt_in_off' {
  if ($env:CREATOROS_LIVE_PUBLISH -eq '1') { throw 'CREATOROS_LIVE_PUBLISH=1 is set (live publishing harness enabled)' }
  'off'
}

Check 'backend_tests' {
  $py = Join-Path $Root 'app\backend\.venv\Scripts\python.exe'
  Push-Location (Join-Path $Root 'app\backend')
  try {
    $out = & $py -m pytest -q 2>&1
    $line = ($out | Where-Object { $_ -match 'passed|failed|error' } | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0) { throw $line }
    $line
  } finally { Pop-Location }
} 'optional'

Check 'writes_outside_root' {
  $suspicious = @()
  foreach ($p in @("$env:LOCALAPPDATA\Ollama")) {
    if (Test-Path $p) {
      $mb = [math]::Round(((Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB), 2)
      $suspicious += "$p = $mb MB (documented third-party state)"
    }
  }
  if ($suspicious.Count -eq 0) { 'none found' } else { $suspicious -join '; ' }
} 'optional'

if ($Json) {
  $results | ConvertTo-Json -Depth 5
} else {
  Write-Host ''
  Write-Host 'CreatorOS Studio - diagnostics' -ForegroundColor Cyan
  Write-Host ('=' * 78)
  foreach ($k in $results.Keys) {
    $r = $results[$k]
    $icon = if ($r.ok) { 'OK  ' } elseif ($r.level -eq 'optional') { 'WARN' } else { 'FAIL' }
    $color = if ($r.ok) { 'Green' } elseif ($r.level -eq 'optional') { 'Yellow' } else { 'Red' }
    $text = if ($r.ok) { $r.value } else { $r.error }
    Write-Host ("[{0}] {1,-26} {2}" -f $icon, $k, $text) -ForegroundColor $color
  }
  Write-Host ('=' * 78)
  Write-Host ("required failures: {0}   warnings: {1}" -f $fail, $warn) -ForegroundColor $(if ($fail) { 'Red' } elseif ($warn) { 'Yellow' } else { 'Green' })
}

if ($fail -gt 0) { exit 1 } elseif ($warn -gt 0) { exit 2 } else { exit 0 }
