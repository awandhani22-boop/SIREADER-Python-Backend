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

function Assert-LoopbackPostgresTarget([string]$Value, [string]$Source) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "DATABASE_URL from $Source is empty; maintenance registration is blocked."
    }

    $Candidate = $Value.Trim()
    if ($Candidate.StartsWith('"') -and $Candidate.EndsWith('"')) {
        $Candidate = $Candidate.Substring(1, $Candidate.Length - 2)
    } elseif ($Candidate.StartsWith("'") -and $Candidate.EndsWith("'")) {
        $Candidate = $Candidate.Substring(1, $Candidate.Length - 2)
    }

    try {
        $Parsed = [System.Uri]::new($Candidate)
    } catch {
        throw "DATABASE_URL from $Source is invalid; maintenance registration is blocked."
    }

    $Scheme = $Parsed.Scheme.ToLowerInvariant()
    $HostName = $Parsed.DnsSafeHost.TrimStart('[').TrimEnd(']').ToLowerInvariant()
    if (-not [string]::IsNullOrEmpty($Parsed.Query) -or
        -not [string]::IsNullOrEmpty($Parsed.Fragment)) {
        throw "DATABASE_URL from $Source contains query/fragment overrides; maintenance registration is blocked."
    }
    if ($Scheme -notin @("postgres", "postgresql", "postgresql+psycopg") -or
        $HostName -notin @("localhost", "127.0.0.1", "::1")) {
        throw "DATABASE_URL from $Source is not loopback PostgreSQL. Remote, Supabase, and production targets are blocked."
    }
    $UserName = [System.Uri]::UnescapeDataString(($Parsed.UserInfo -split ':', 2)[0])
    $DatabaseName = $Parsed.AbsolutePath.Trim('/')
    if ($UserName -ne "sireader_workforce_maint" -or $DatabaseName -ne "sireader_disposable") {
        throw "DATABASE_URL from $Source must use sireader_workforce_maint on sireader_disposable."
    }
}

# Refuse libpq overrides in every Windows environment scope. These variables
# can redirect a URL that otherwise appears to target loopback PostgreSQL.
$BlockedPgVariables = @(
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGSERVICE", "PGSERVICEFILE",
    "PGLOADBALANCEHOSTS", "PGTARGETSESSIONATTRS", "PGSSLMODE"
)
foreach ($Variable in $BlockedPgVariables) {
    foreach ($Scope in @("Process", "User", "Machine")) {
        $Override = [Environment]::GetEnvironmentVariable($Variable, $Scope)
        if (-not [string]::IsNullOrWhiteSpace($Override)) {
            throw "PostgreSQL connection override environment is set; maintenance registration is blocked."
        }
    }
}

# The core DB connection loads .env without overriding process variables.
# Check .env and every Windows environment scope that may take precedence.
$DatabaseCandidates = @()
$MaintenanceUrlKey = "SIREADER_WORKFORCE_MAINTENANCE_DATABASE_URL"
$EnvPattern = '^\s*(?:export\s+)?' + [regex]::Escape($MaintenanceUrlKey) + '\s*='
$EnvMatch = Get-Content -LiteralPath $EnvFile |
    Where-Object { $_ -match $EnvPattern } |
    Select-Object -Last 1

if ($null -ne $EnvMatch) {
    $FileValue = $EnvMatch -replace ('^\s*(?:export\s+)?' + [regex]::Escape($MaintenanceUrlKey) + '\s*=\s*'), ''
    $FileValue = $FileValue -replace '\s+#.*$', ''
    $DatabaseCandidates += @{ Value = $FileValue.Trim(); Source = ".env" }
}

foreach ($Scope in @("Process", "User", "Machine")) {
    $ScopedValue = [Environment]::GetEnvironmentVariable($MaintenanceUrlKey, $Scope)
    if (-not [string]::IsNullOrWhiteSpace($ScopedValue)) {
        $DatabaseCandidates += @{ Value = $ScopedValue; Source = "$Scope environment" }
    }
}

if ($DatabaseCandidates.Count -eq 0) {
    throw "$MaintenanceUrlKey is not configured. Maintenance registration is blocked."
}

foreach ($Candidate in $DatabaseCandidates) {
    Assert-LoopbackPostgresTarget -Value $Candidate.Value -Source $Candidate.Source
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
