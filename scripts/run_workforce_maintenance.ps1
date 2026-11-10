$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$PythonCandidates = @(
    (Join-Path $ProjectRoot ".venv\Scripts\python.exe"),
    "C:\SIREADER\.venv\Scripts\python.exe"
)
$Python = $PythonCandidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $Python) {
    throw "Python 3.12 runtime not found. Expected the backend .venv or C:\SIREADER\.venv."
}

& $Python -m app.workers.maintenance
if ($LASTEXITCODE -ne 0) {
    throw "SIREADER workforce maintenance failed with exit code $LASTEXITCODE"
}
