$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

& $Python -m app.workers.maintenance
if ($LASTEXITCODE -ne 0) {
    throw "SIREADER workforce maintenance failed with exit code $LASTEXITCODE"
}
