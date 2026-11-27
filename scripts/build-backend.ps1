<#
.SYNOPSIS
    Assembles the runnable backend that the installer ships.

.DESCRIPTION
    The shipped backend is a *signed interpreter plus a pure-Python payload*, not a compiled
    executable. Layout (about 62 MB after the rules below pruned the developer environment):

        installer_builds\backend\
            python.exe, python3.dll, python312.dll     (Python Software Foundation, Authenticode-signed)
            DLLs\                                       (the interpreter's own extensions, signed)
            Lib\                                        (standard library, pruned of dev-only packages)
            Lib\site-packages\                           (dependencies + the creatoros package)
            alembic.ini, migrations\                     (so "migrate" works from the bundle)

    Why not PyInstaller (see docs/ADR/ADR-34-Release-Packaging.md):

      * Smart App Control is ON on this machine and blocks every newly built unsigned executable.
        A frozen bundle is such an executable; every copy of the Python interpreter keeps its
        publisher signature and therefore keeps working. This is not a preference, it is the only
        layout that runs here without changing an OS security setting.
      * It is also more debuggable: a traceback from the shipped backend names a real file, and an
        operator can run it by hand with no build step.
      * The cost is size (110 MB vs 39 MB). The installer compresses it; the difference is not worth
        a blocked launch.

    Idempotent: the output directory is rebuilt from scratch on every run. Nothing outside
    installer_builds\ and temp\ is written.

.PARAMETER OutputRoot
    Where to place the bundle. Defaults to installer_builds\backend.

.PARAMETER SkipVerify
    Assemble only; skip the migrate + serve smoke test.
#>
[CmdletBinding()]
param(
    [switch]$SkipVerify,
    [string]$OutputRoot = ''
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $scriptDir '..')).Path
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $repo 'installer_builds\backend'
}

$backend = Join-Path $repo 'app\backend'
$projectVersion = ((Select-String -Path (Join-Path $backend 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value)
$venvPython = Join-Path $backend '.venv\Scripts\python.exe'
$sitePackages = Join-Path $backend '.venv\Lib\site-packages'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }

if (-not (Test-Path $venvPython)) { throw "backend virtualenv python not found: $venvPython" }
if (-not (Test-Path $sitePackages)) { throw "backend site-packages not found: $sitePackages" }
& $venvPython (Join-Path $scriptDir 'backend_lock.py') --check
if ($LASTEXITCODE -ne 0) { throw 'the backend environment does not match requirements-runtime.lock' }

Write-Step 'locating the interpreter that owns the virtualenv'
$basePrefix = & $venvPython -c "import sys; print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0) { throw "could not ask $venvPython for sys.base_prefix" }
$basePrefix = $basePrefix.Trim()
if (-not (Test-Path $basePrefix)) { throw "base interpreter not found: $basePrefix" }
$pyVersion = & $venvPython -c "import sys; print('%d%d' % sys.version_info[:2])"
Write-Host "    base: $basePrefix (Python $pyVersion)"

$pythonExe = Join-Path $basePrefix 'python.exe'
$x = Join-Path $basePrefix "python$pyVersion.dll"
if (-not (Test-Path $pythonExe)) { throw "python.exe not found in $basePrefix" }
if (-not (Test-Path $x)) { throw "python$pyVersion.dll not found in $basePrefix" }

Write-Step 'verifying the interpreter against the committed runtime manifest'
& $venvPython -B (Join-Path $scriptDir 'runtime_manifest.py') --check --root $basePrefix
if ($LASTEXITCODE -ne 0) { throw 'the base interpreter does not match app\backend\runtime-manifest.json' }

Write-Step 'checking the interpreter signature'
$sig = Get-AuthenticodeSignature $pythonExe
Write-Host "    $pythonExe -> $($sig.Status) ($($sig.SignerCertificate.Subject))"
if ($sig.Status -ne 'Valid') {
    throw "the interpreter is not validly signed ($($sig.Status)); refusing to ship it"
}
if (-not $sig.SignerCertificate -or $sig.SignerCertificate.Subject -notlike '*O=Python Software Foundation*') {
    throw "the interpreter signer is not Python Software Foundation: $($sig.SignerCertificate.Subject)"
}

Write-Step "rebuilding $OutputRoot"
if (Test-Path $OutputRoot) { Remove-Item $OutputRoot -Recurse -Force }
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

Write-Step 'copying the interpreter'
Copy-Item $pythonExe -Destination $OutputRoot
foreach ($dll in @("python$pyVersion.dll", 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')) {
    $source = Join-Path $basePrefix $dll
    if (Test-Path $source) { Copy-Item $source -Destination $OutputRoot }
}
Copy-Item (Join-Path $basePrefix 'DLLs') -Destination $OutputRoot -Recurse
Copy-Item (Join-Path $basePrefix 'Lib') -Destination $OutputRoot -Recurse
# The copied interpreter must still match the committed manifest. This catches a copy fault, a
# substituted base directory, and any extra .exe/.dll/.pyd dropped into the runtime.
& $venvPython -B (Join-Path $scriptDir 'runtime_manifest.py') --check --root $OutputRoot --strict-extras
if ($LASTEXITCODE -ne 0) { throw 'the assembled interpreter does not match app\backend\runtime-manifest.json' }
# LICENSE is required by the PSF licence when redistributing the interpreter.
$license = Join-Path $basePrefix 'LICENSE.txt'
if (Test-Path $license) { Copy-Item $license -Destination (Join-Path $OutputRoot 'PYTHON-LICENSE.txt') }

Write-Step 'pruning development-only standard library packages'
$lib = Join-Path $OutputRoot 'Lib'
$prune = @(
    'test', 'tkinter', 'turtledemo', 'idlelib', 'ensurepip', 'lib2to3', 'distutils', 'venv',
    'site-packages\pip', 'site-packages\setuptools', 'site-packages\pkg_resources',
    'site-packages\_pytest', 'site-packages\pytest', 'site-packages\coverage',
    'site-packages\PyInstaller', 'site-packages\pyinstaller_hooks_contrib',
    'site-packages\altgraph', 'site-packages\pefile', 'site-packages\pywin32_ctypes'
)
foreach ($item in $prune) {
    $target = Join-Path $lib $item
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
}

Write-Step 'installing backend dependencies into the bundle'
$bundleSite = Join-Path $lib 'site-packages'
New-Item -ItemType Directory -Path $bundleSite -Force | Out-Null
# *.dist-info stays for runtime packages: it is how `importlib.metadata` reports versions, and a
# dependency query that cannot answer is a support ticket.
Copy-Item (Join-Path $sitePackages '*') -Destination $bundleSite -Recurse -Force
# Editable-install hooks point at this machine's source tree and must never ship.
Get-ChildItem $bundleSite -Filter '__editable__*' -ErrorAction SilentlyContinue | Remove-Item -Force
# Bytecode caches belong to the developer machine; Smart App Control may also rename binaries it
# blocked to *.sacblocked. Neither name is in a wheel RECORD, so neither may ship.
Get-ChildItem $bundleSite -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Get-ChildItem $bundleSite -Recurse -File -Filter '*.sacblocked' -ErrorAction SilentlyContinue |
    Remove-Item -Force
Remove-Item (Join-Path $bundleSite 'a1_coverage.pth') -ErrorAction SilentlyContinue
# CPython's base Lib/site-packages carries this placeholder for pip installs. It belongs to no
# distribution and nothing imports it, so it is removed rather than shipped unowned.
Remove-Item (Join-Path $bundleSite 'README.txt') -ErrorAction SilentlyContinue

# The virtualenv is a developer environment. Copying it wholesale ships pytest, coverage, ruff and
# PyInstaller (including the four unsigned bootloader .exe files) into the product unless they are
# removed explicitly. This list is the evidence-backed minimum: none of these modules is imported by
# creatoros at runtime, and the smoke test below fails loudly if removing one broke the bundle.
$devDistributions = @{
    'pytest'                    = @('pytest', '_pytest', 'py')
    'pytest-asyncio'            = @('pytest_asyncio')
    'pytest-cov'                = @('pytest_cov')
    'coverage'                  = @('coverage')
    'pyinstaller'               = @('PyInstaller', 'pyinstaller')
    'pyinstaller-hooks-contrib' = @('pyinstaller_hooks_contrib', '_pyinstaller_hooks_contrib')
    'altgraph'                  = @('altgraph')
    'pywin32-ctypes'            = @('pywin32_ctypes', 'win32ctypes')
    'pefile'                    = @('pefile', 'peutils')
    'ordlookup'                 = @('ordlookup')
    'pygments'                  = @('pygments')
    'packaging'                 = @('packaging')
    'iniconfig'                 = @('iniconfig')
    'ruff'                      = @('ruff', '_ruff')
    'respx'                     = @('respx')
    'pluggy'                    = @('pluggy')
    'pip'                       = @('pip')
    'setuptools'                = @('setuptools', 'pkg_resources', '_distutils_hack')
}
$removed = 0
foreach ($dist in $devDistributions.Keys) {
    foreach ($module in $devDistributions[$dist]) {
        # Some packages install a single top-level module file (pefile.py, peutils.py) next to
        # the directories; checking only the bare name left those files in the 1.0.0 bundle.
        foreach ($candidate in @($module, "$module.py")) {
            $target = Join-Path $bundleSite $candidate
            if (Test-Path $target) { Remove-Item $target -Recurse -Force; $removed++ }
        }
    }
    # dist-info directories use the PEP 503 normalised name (pytest_asyncio-1.5.0.dist-info),
    # while this table uses the project name (pytest-asyncio). Matching only hyphens left four
    # metadata directories in the 1.0.0 bundle, and every inventory then reported those
    # development packages as installed. Accept either separator.
    $distPattern = '^' + ($dist -replace '-', '[-_]') + '-[0-9]'
    Get-ChildItem $bundleSite -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $distPattern } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force; $removed++ }
}
# setuptools leaves a .pth shim behind that the interpreter executes on every start. Removing the
# package without this file prints a ModuleNotFoundError before any output and breaks JSON consumers.
Remove-Item (Join-Path $bundleSite 'distutils-precedence.pth') -ErrorAction SilentlyContinue

# A silent reintroduction is worse than a larger bundle: refuse to build one.
foreach ($dist in $devDistributions.Keys) {
    foreach ($module in $devDistributions[$dist]) {
        foreach ($candidate in @($module, "$module.py")) {
            if (Test-Path (Join-Path $bundleSite $candidate)) {
                throw "development package '$candidate' leaked into the bundle"
            }
        }
    }
    # A removed module with a surviving dist-info still looks installed to importlib.metadata and
    # to every dependency inventory. Treat that as a leak, not as cosmetic leftovers.
    $metadata = Get-ChildItem $bundleSite -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match ('^' + ($dist -replace '-', '[-_]') + '-[0-9]') }
    if ($metadata) {
        throw "development package metadata '$($metadata[0].Name)' leaked into the bundle"
    }
}
Write-Host "    pruned $removed development-only entries"

# The virtualenv can contain packages that are neither development tooling nor runtime
# dependencies. The lock is authoritative: replay and remove every other distribution, then refuse
# to continue if any site-packages file is still unowned.
Write-Step 'pruning packages outside the runtime lock'
& $venvPython (Join-Path $scriptDir 'payload_prune.py') --payload $OutputRoot --lock (Join-Path $backend 'requirements-runtime.lock')
if ($LASTEXITCODE -ne 0) { throw 'payload does not match the runtime lock after pruning' }

Write-Step 'copying the creatoros package'
Copy-Item (Join-Path $backend 'src\creatoros') -Destination $bundleSite -Recurse -Force
Get-ChildItem $bundleSite -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

# The virtualenv may hold an editable install from an older release; its dist-info describes the
# developer machine, not this payload. Remove it, then write canonical metadata at the project
# version and hash the files actually shipped.
Get-ChildItem $bundleSite -Directory -Filter 'creatoros_backend-*.dist-info' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
$metadataScript = Join-Path $scriptDir 'payload_metadata.py'
$bundlePython = Join-Path $OutputRoot 'python.exe'
& $bundlePython -B $metadataScript --payload $OutputRoot --project $backend --version $projectVersion
if ($LASTEXITCODE -ne 0) { throw 'first-party payload metadata generation failed' }
& $bundlePython -B $metadataScript --check --payload $OutputRoot --project $backend
if ($LASTEXITCODE -ne 0) { throw 'first-party payload metadata verification failed' }

Write-Step 'copying the migration environment'
Copy-Item (Join-Path $backend 'alembic.ini') -Destination $OutputRoot
Copy-Item (Join-Path $backend 'migrations') -Destination $OutputRoot -Recurse
Get-ChildItem (Join-Path $OutputRoot 'migrations') -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

$bundleExe = Join-Path $OutputRoot 'python.exe'
$size = [math]::Round(((Get-ChildItem $OutputRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "    bundle: $OutputRoot ($size MB)"

function Invoke-BundlePython {
    param([string[]]$Arguments, [int]$TimeoutSeconds = 120)
    $stamp = [guid]::NewGuid().ToString('N')
    $stdoutPath = Join-Path $sandbox "$stamp.out"
    $stderrPath = Join-Path $sandbox "$stamp.err"
    $proc = Start-Process -FilePath $bundleExe -ArgumentList $Arguments `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
        -PassThru -WindowStyle Hidden
    try {
        if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            throw "bundled interpreter timed out after $TimeoutSeconds s: $($Arguments -join ' ')"
        }
        # WaitForExit(int) can return before the exit code and redirected streams are observable.
        # The no-argument overload closes that race; reading ExitCode immediately can otherwise
        # report a spurious failure for a process that actually exited 0.
        $proc.WaitForExit()
        $exitCode = $proc.ExitCode
        $stdout = if (Test-Path $stdoutPath) { Get-Content $stdoutPath -Raw } else { '' }
        $stderr = if (Test-Path $stderrPath) { Get-Content $stderrPath -Raw } else { '' }
        return [pscustomobject]@{ ExitCode = $exitCode; StdOut = $stdout; StdErr = $stderr }
    } finally {
        Remove-Item $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not $SkipVerify) {
    Write-Step 'verifying the bundle on an empty data root'
    $sandbox = Join-Path $repo 'temp\bundle-verify'
    if (Test-Path $sandbox) { Remove-Item $sandbox -Recurse -Force }
    New-Item -ItemType Directory -Path $sandbox | Out-Null

    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $env:CREATOROS_ROOT = $sandbox
    $env:TEMP = Join-Path $sandbox 'temp'
    $env:TMP = $env:TEMP
    $env:CREATOROS_DISABLE_AI = '1'
    try {
        $created = Invoke-BundlePython -Arguments @('-m', 'creatoros', 'version') -TimeoutSeconds 60
        Write-Host ($created.StdOut.Trim())
        if ($created.ExitCode -ne 0) { throw "bundled interpreter could not run 'creatoros version':`n$($created.StdErr)" }

        $migrated = Invoke-BundlePython -Arguments @('-m', 'creatoros', 'migrate') -TimeoutSeconds 180
        if ($migrated.ExitCode -ne 0) { throw "bundled interpreter could not migrate an empty data root:`n$($migrated.StdErr)" }
        $versioned = Invoke-BundlePython -Arguments @('-m', 'creatoros', 'version', '--json') -TimeoutSeconds 60
        if ($versioned.ExitCode -ne 0) { throw "bundled interpreter could not report its version:`n$($versioned.StdErr)" }
        $parsed = $versioned.StdOut | ConvertFrom-Json
        if (-not $parsed.revision) { throw "no migration revision after migrate" }
        if ($parsed.revision -ne $parsed.head) { throw "revision $($parsed.revision) is not head $($parsed.head)" }
        Write-Host "    migrated (empty) -> $($parsed.revision)"

        Write-Step 'starting the bundled API and reading /health/live'
        $port = 5610
        $logPath = Join-Path $sandbox 'serve.log'
        $env:CREATOROS_SESSION_TOKEN = 'bundle-verify-token'
        $proc = Start-Process -FilePath $bundleExe `
            -ArgumentList @('-m', 'creatoros', 'serve', '--port', $port) `
            -RedirectStandardOutput $logPath -RedirectStandardError "$logPath.err" `
            -PassThru -WindowStyle Hidden
        try {
            $live = $null
            for ($i = 0; $i -lt 60; $i++) {
                Start-Sleep -Milliseconds 700
                try {
                    $live = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health/live" -TimeoutSec 2
                    break
                } catch { }
            }
            if (-not $live) {
                $tail = (Get-Content "$logPath.err" -Tail 20 -ErrorAction SilentlyContinue) -join "`n"
                throw "the bundled API never became live on port $port`n$tail"
            }
            Write-Host "    /health/live -> $($live.status) (version $($live.version))"
            if ($live.status -ne 'live') { throw "unexpected health status: $($live.status)" }
        } finally {
            if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
        }
    } finally {
        $ErrorActionPreference = $previousEap
        foreach ($name in @('CREATOROS_ROOT', 'TEMP', 'TMP', 'CREATOROS_DISABLE_AI', 'CREATOROS_SESSION_TOKEN')) {
            Remove-Item "Env:\$name" -ErrorAction SilentlyContinue
        }
    }
}

# Running the bundle during verification writes __pycache__ into the very tree that ships. Remove
# it so two builds of the same inputs produce the same bytes, and so the payload carries no compiled
# leftovers from this machine.
Get-ChildItem $OutputRoot -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Get-ChildItem $OutputRoot -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue | Remove-Item -Force

$finalSize = [math]::Round(((Get-ChildItem $OutputRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Step 'done'
Write-Host "Final bundle: $finalSize MB"
Write-Host "The installer build expects: $OutputRoot\python.exe"
