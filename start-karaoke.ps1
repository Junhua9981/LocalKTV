# LocalKTV launcher: prepares the environment on first run, then starts the
# server (FastAPI + AI worker), opens /display and prints the join QR code.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$requirements = Join-Path $root "server\requirements.txt"
$dist = Join-Path $root "web\dist\index.html"

function Find-Python {
    foreach ($candidate in @("python3.12", "python3.12.exe", "py -3.12", "python")) {
        try {
            $version = & cmd /c "$candidate --version 2>&1"
            if ($version -match "3\.1[0-9]|3\.12|3\.13") { return $candidate }
        } catch { }
    }
    return $null
}

if (-not (Test-Path $python)) {
    Write-Host "[LocalKTV] creating virtualenv..."
    $base = Find-Python
    if (-not $base) {
        Write-Host "[LocalKTV] Python 3.10+ is required. Install it first." -ForegroundColor Red
        exit 1
    }
    & cmd /c "$base -m venv `"$root\.venv`""
    & $python -m pip install -q --upgrade pip
    & $python -m pip install -q -r $requirements
}

if (-not (Test-Path $dist)) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($npm) {
        Write-Host "[LocalKTV] building web UI (first run)..."
        Push-Location (Join-Path $root "web")
        try {
            npm install --no-audit --no-fund
            npm run build
        } finally {
            Pop-Location
        }
    } else {
        Write-Host "[LocalKTV] npm not found - /display UI unavailable" -ForegroundColor Yellow
    }
}

Set-Location $root
& $python (Join-Path $root "server\main.py") @args
