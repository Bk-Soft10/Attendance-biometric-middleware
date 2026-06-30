<#
.SYNOPSIS
    One-click installer for the Biometric Attendance Bridge on Windows.

.DESCRIPTION
    Creates a Python virtualenv, installs dependencies, writes config.ini, and
    registers a Scheduled Task that runs the bridge as a daemon at startup.

.EXAMPLE
    # Interactive
    powershell -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    # Non-interactive
    powershell -ExecutionPolicy Bypass -File install.ps1 -OdooUrl https://acme.odoo.com `
        -Transport json2 -ApiKey xxxx -AutoDiscover true -Interval 5 -NonInteractive
#>
param(
    [string]$InstallDir = "C:\biometric-bridge",
    [string]$OdooUrl = "",
    [string]$OdooDb = "",
    [ValidateSet("json2", "custom")][string]$Transport = "json2",
    [string]$ApiKey = "",
    [string]$AutoDiscover = "true",
    [int]$Interval = 5,
    [int]$SinceHours = 24,
    [string]$DeviceCode = "",
    [string]$DeviceHost = "",
    [int]$DevicePort = 4370,
    [switch]$NonInteractive,
    [switch]$NoService
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "BiometricBridge"

function Ask($prompt, $default) {
    if ($NonInteractive) { return $default }
    if ($default) { $r = Read-Host "$prompt [$default]"; if ([string]::IsNullOrWhiteSpace($r)) { return $default } else { return $r } }
    return Read-Host $prompt
}

Write-Host "============================================================" -ForegroundColor Green
Write-Host " Biometric Attendance Bridge - Windows Installer" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green

if (-not $NonInteractive) {
    $InstallDir = Ask "Install directory" $InstallDir
    $OdooUrl    = Ask "Odoo URL (https://company.odoo.com)" $OdooUrl
    $OdooDb     = Ask "Odoo database name (blank if single-DB host)" $OdooDb
    $Transport  = Ask "Transport (json2/custom)" $Transport
    if ($Transport -eq "json2") {
        Write-Host "  -> Provide a USER API key (Preferences > Account Security > New API Key)"
    } else {
        Write-Host "  -> Provide a device API key (Biometric > Devices > device form)"
    }
    $ApiKey       = Ask "API key" $ApiKey
    $AutoDiscover = Ask "Auto-discover all devices from Odoo? (true/false)" $AutoDiscover
    if ($AutoDiscover -ne "true") {
        $DeviceCode = Ask "Device code (single-device mode)" $DeviceCode
        $DeviceHost = Ask "Device IP (single-device mode)" $DeviceHost
        $DevicePort = [int](Ask "Device port" $DevicePort)
    }
    $Interval = [int](Ask "Sync interval (minutes)" $Interval)
}

if ([string]::IsNullOrWhiteSpace($OdooUrl) -or [string]::IsNullOrWhiteSpace($ApiKey)) {
    Write-Host "ERROR: Odoo URL and API key are required." -ForegroundColor Red
    exit 1
}

# Locate python
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { $Python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $Python) { Write-Host "ERROR: Python not found. Install Python 3.8+ and re-run." -ForegroundColor Red; exit 1 }

Write-Host "Installing to: $InstallDir" -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force (Join-Path $ScriptDir "biometric_middleware.py") $InstallDir
if (Test-Path (Join-Path $ScriptDir "requirements.txt")) {
    Copy-Item -Force (Join-Path $ScriptDir "requirements.txt") $InstallDir
}

Write-Host "Creating virtual environment..." -ForegroundColor Yellow
& $Python -m venv (Join-Path $InstallDir "venv")
$VenvPy = Join-Path $InstallDir "venv\Scripts\python.exe"
& $VenvPy -m pip install --upgrade pip | Out-Null
Write-Host "Installing dependencies (pyzk, requests)..." -ForegroundColor Yellow
& $VenvPy -m pip install pyzk requests | Out-Null

# Write config.ini
$ConfigFile = Join-Path $InstallDir "config.ini"
Write-Host "Writing configuration: $ConfigFile" -ForegroundColor Yellow
@"
[odoo]
url = $OdooUrl
db = $OdooDb
transport = $Transport
api_key = $ApiKey
auto_discover = $AutoDiscover
device_code = $DeviceCode
timeout = 30

[device]
host = $DeviceHost
port = $DevicePort
password = 0
type = auto

[sync]
interval_minutes = $Interval
batch_size = 100
clear_after_sync = false
since_hours = $SinceHours
retry_attempts = 3
retry_delay_seconds = 10

[logging]
level = INFO
file =
"@ | Set-Content -Encoding ASCII $ConfigFile

# Optional test run
if (-not $NonInteractive) {
    $runTest = Ask "Run a test sync now? (yes/no)" "no"
    if ($runTest -eq "yes") {
        Write-Host "Running a single sync cycle..." -ForegroundColor Yellow
        & $VenvPy (Join-Path $InstallDir "biometric_middleware.py") --config $ConfigFile --once -v
    }
}

# Scheduled Task (runs daemon at startup)
if (-not $NoService) {
    Write-Host "Registering Scheduled Task: $TaskName" -ForegroundColor Yellow
    $action = New-ScheduledTaskAction -Execute $VenvPy `
        -Argument "`"$(Join-Path $InstallDir 'biometric_middleware.py')`" --config `"$ConfigFile`" --daemon" `
        -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Scheduled Task registered and started." -ForegroundColor Green
    Write-Host "Manage it with: Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
} else {
    Write-Host "Skipping Scheduled Task. Run manually with:" -ForegroundColor Yellow
    Write-Host "  `"$VenvPy`" `"$(Join-Path $InstallDir 'biometric_middleware.py')`" --config `"$ConfigFile`" --daemon"
}

Write-Host ""
Write-Host "Done. Configuration: $ConfigFile" -ForegroundColor Green
