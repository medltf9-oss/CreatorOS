<#
.SYNOPSIS
    Keeps the backend venv runnable when Windows Smart App Control blocks SQLAlchemy's C extensions.

.DESCRIPTION
    Smart App Control evaluates the unsigned compiled extensions shipped inside the SQLAlchemy wheel
    (`sqlalchemy/**/*.pyd`). On this machine it began blocking `_util_cy` and friends, which makes
    `import sqlalchemy` fail with:

        ImportError: DLL load failed while importing _util_cy:
        Une stratégie de contrôle d'application a bloqué ce fichier.

    SQLAlchemy ships a pure-Python implementation next to every compiled module, so the fix is to stop
    Python from picking the blocked `.pyd` files. This script renames them to `*.pyd.sacblocked`.
    Nothing about the operating system security policy is changed, and `-Restore` puts the files back
    (useful after a SQLAlchemy upgrade ships newly-signed binaries or after the policy is relaxed).

    This is an environment workaround, not a Phase 8 feature. It is recorded in
    docs/evidence/P8.md so the suite's green result is not mistaken for a clean environment.

.PARAMETER Restore
    Undo the rename and put the compiled extensions back.
#>
[CmdletBinding()]
param(
    [switch]$Restore
)

$ErrorActionPreference = 'Stop'
$sitePackages = Join-Path $PSScriptRoot '..\app\backend\.venv\Lib\site-packages\sqlalchemy'
$sitePackages = (Resolve-Path $sitePackages).Path

if ($Restore) {
    $moved = Get-ChildItem -Path $sitePackages -Recurse -Filter '*.pyd.sacblocked'
    foreach ($file in $moved) {
        $target = $file.FullName.Substring(0, $file.FullName.Length - '.sacblocked'.Length)
        if (-not (Test-Path $target)) {
            Move-Item -LiteralPath $file.FullName -Destination $target
        }
    }
    Write-Host "Restored $($moved.Count) compiled extension(s)."
    exit 0
}

$blocked = Get-ChildItem -Path $sitePackages -Recurse -Filter '*.pyd'
foreach ($file in $blocked) {
    Move-Item -LiteralPath $file.FullName -Destination ($file.FullName + '.sacblocked')
}
Write-Host "Renamed $($blocked.Count) compiled extension(s); SQLAlchemy now runs pure Python."
Write-Host 'Verify with: app\backend\.venv\Scripts\python.exe -c "import sqlalchemy; print(sqlalchemy.__version__)"'
