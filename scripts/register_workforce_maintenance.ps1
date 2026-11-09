[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$TaskName = "SIREADER-WorkforceMaintenance"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $ProjectRoot "scripts\run_workforce_maintenance.ps1"
$EnvFile = Join-Path $ProjectRoot ".env"

if ($Remove) {
    & schtasks.exe /Delete /TN $TaskName /F | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to remove scheduled task '$TaskName'."
    }
    Write-Host "Removed scheduled task: $TaskName"
    exit 0
}

if (-not (Test-Path $Runner -PathType Leaf)) {
    throw "Maintenance runner not found: $Runner"
}
if (-not (Test-Path $EnvFile -PathType Leaf)) {
    throw "Backend .env file not found. Configure the local database connection before registering maintenance."
}

if ($PSVersionTable.PSEdition -eq "Core") {
    $PowerShellExe = Join-Path $PSHOME "pwsh.exe"
} else {
    $PowerShellExe = Join-Path $PSHOME "powershell.exe"
}
if (-not (Test-Path $PowerShellExe -PathType Leaf)) {
    throw "PowerShell executable not found: $PowerShellExe"
}

# /IT keeps the task tied to the currently interactive Windows user. No Windows
# password is embedded in the task definition or script command.
$TaskCommand = '"{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}"' -f $PowerShellExe, $Runner

& schtasks.exe /Create /SC MINUTE /MO 5 /TN $TaskName /TR $TaskCommand /F /RL LIMITED /IT | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Unable to register scheduled task '$TaskName'. Run this script from the intended Windows account."
}

Write-Host "Registered $TaskName to run every 5 minutes for the current interactive user."
Write-Host "Project: $ProjectRoot"
Write-Host "Runner:  $Runner"

& schtasks.exe /Query /TN $TaskName /FO LIST /V | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "The scheduled task was created but could not be queried."
}

if ($RunNow) {
    & schtasks.exe /Run /TN $TaskName | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "The scheduled task was registered but could not be started."
    }
    Write-Host "Started one maintenance cycle."
}
