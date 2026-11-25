# Point the Windows Ollama *app* at CreatorOS's model directory, so a reboot does not undo the fix.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix_ollama_path.ps1            # report
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix_ollama_path.ps1 -Apply     # change it
#
# The Ollama tray app keeps its own `models` path in %LOCALAPPDATA%\Ollama\db.sqlite and that value wins
# over the OLLAMA_MODELS environment variable. On this machine it still pointed at D:\NEXUS_DATA\models
# from an earlier installation, which is why the daemon reported 0 models while 9.3 GB sat unused in
# <data root>\models\ollama (2026-11-24). `start_ollama.ps1` repairs the *running* daemon by setting the
# variable for the server process; this script fixes the durable setting the tray app will use at login.
#
# It writes one column of one row, after stopping the app and copying db.sqlite next to it as a backup.
# Nothing else is touched, and -Apply is needed to change anything.
param(
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

$root = if ($env:CREATOROS_ROOT) { $env:CREATOROS_ROOT } else { Join-Path $env:USERPROFILE 'CreatorOS' }
$wanted = Join-Path $root 'models\ollama'
$appData = Join-Path $env:LOCALAPPDATA 'Ollama'
$db = Join-Path $appData 'db.sqlite'
$appExe = Join-Path $root 'Programs\Ollama\ollama app.exe'

if (-not (Test-Path $db)) {
    Write-Output "the Ollama app database is not present: $db"
    Write-Output 'the app has never run, or Ollama is installed elsewhere. Nothing to fix.'
    exit 0
}

# Python ships with CreatorOS's own bundle, so this needs no system Python.
$python = Join-Path $root 'app\backend\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$read = @'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("select models from settings where id=1").fetchone()
print(row[0] if row else "")
con.close()
'@
$write = @'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("update settings set models=? where id=1", (sys.argv[2],))
con.commit()
print(con.execute("select models from settings where id=1").fetchone()[0])
con.close()
'@

$readFile = Join-Path $env:TEMP 'creatoros-ollama-read.py'
$writeFile = Join-Path $env:TEMP 'creatoros-ollama-write.py'
Set-Content -Path $readFile -Value $read -Encoding ascii
Set-Content -Path $writeFile -Value $write -Encoding ascii
try {
    $current = (& $python $readFile $db) -join ''
    Write-Output "Ollama app model path : $current"
    Write-Output "CreatorOS models      : $wanted"
    if ($current -eq $wanted) {
        Write-Output 'already correct: the app will serve the CreatorOS models after any restart'
        exit 0
    }
    if (-not $Apply) {
        Write-Output ''
        Write-Output 'different. Re-run with -Apply to change it (the app is stopped first and db.sqlite is copied as a backup).'
        exit 0
    }
    Get-Process 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    $backup = Join-Path $appData ("db.sqlite.creatoros-backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Copy-Item $db $backup -Force
    Write-Output "backup: $backup"
    $updated = (& $python $writeFile $db $wanted) -join ''
    Write-Output "updated to: $updated"
    if (Test-Path $appExe) {
        Start-Process -FilePath $appExe -WindowStyle Minimized
        Write-Output 'restarted the Ollama app'
    }
    Write-Output 'verify with: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_ollama.ps1 -Status'
    exit 0
} finally {
    Remove-Item $readFile, $writeFile -Force -ErrorAction SilentlyContinue
}
