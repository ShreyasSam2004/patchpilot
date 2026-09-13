<#
.SYNOPSIS
    Start PatchPilot: the API (with its embedded worker) and the web UI.

.DESCRIPTION
    Safe to run any number of times. Each half is started only if its port is
    free, so running this while PatchPilot is already up does nothing rather
    than launching a second copy that cannot bind.

    Paths are resolved from this script's own location, so moving or renaming
    the project folder does not break it.

    Run by hand, or automatically at logon via register-autostart.ps1.

.PARAMETER NoWait
    Return as soon as the processes are launched instead of waiting for both to
    answer. Used by the logon task, where nothing is watching the output.
#>
[CmdletBinding()]
param(
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'

$root     = Split-Path -Parent $PSScriptRoot
$backend  = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$logs     = Join-Path $root 'logs'
$python   = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path $logs)) { New-Item -ItemType Directory -Force $logs | Out-Null }

function Test-PortBusy([int]$Port) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return ($null -ne $conn)
}

function Wait-ForUrl([string]$Url, [int]$TimeoutSeconds = 90) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) { return $true }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    return $false
}

# --- the API ------------------------------------------------------------------
if (Test-PortBusy 8000) {
    Write-Output "API      already running on :8000"
} elseif (-not (Test-Path $python)) {
    Write-Output "API      SKIPPED - no virtualenv at $python (run: make install-backend)"
} else {
    # The project's own entry point: it reads host, port and log level from
    # settings, and the app's lifespan starts the embedded worker that actually
    # executes runs. Bare uvicorn would work but would ignore that config.
    Start-Process -FilePath $python `
        -ArgumentList '-m', 'patchpilot.cli', 'serve' `
        -WorkingDirectory $backend `
        -RedirectStandardOutput (Join-Path $logs 'api.out.log') `
        -RedirectStandardError  (Join-Path $logs 'api.err.log') `
        -WindowStyle Hidden | Out-Null
    Write-Output "API      starting on :8000"
}

# --- the web UI ---------------------------------------------------------------
$nextBuild = Join-Path $frontend '.next'
$node = $null
$cmd = Get-Command node -ErrorAction SilentlyContinue
if ($cmd) { $node = $cmd.Source }

if (Test-PortBusy 3000) {
    Write-Output "Frontend already running on :3000"
} elseif (-not $node) {
    Write-Output "Frontend SKIPPED - node is not on PATH"
} elseif (-not (Test-Path $nextBuild)) {
    # `next start` serves a build; without one it exits immediately and the
    # only symptom is a port that never opens.
    Write-Output "Frontend SKIPPED - no production build (run: npm run build in frontend/)"
} else {
    Start-Process -FilePath $node `
        -ArgumentList (Join-Path $frontend 'node_modules\next\dist\bin\next'), 'start', '-p', '3000' `
        -WorkingDirectory $frontend `
        -RedirectStandardOutput (Join-Path $logs 'web.out.log') `
        -RedirectStandardError  (Join-Path $logs 'web.err.log') `
        -WindowStyle Hidden | Out-Null
    Write-Output "Frontend starting on :3000"
}

if ($NoWait) { return }

$apiUp = Wait-ForUrl 'http://127.0.0.1:8000/api/health'
$webUp = Wait-ForUrl 'http://localhost:3000/'

Write-Output ""
if ($apiUp) { Write-Output "API      ready   http://127.0.0.1:8000" }
else        { Write-Output "API      DID NOT START - see $logs\api.err.log" }
if ($webUp) { Write-Output "Frontend ready   http://localhost:3000" }
else        { Write-Output "Frontend DID NOT START - see $logs\web.err.log" }

if ($apiUp -and $webUp) {
    Write-Output ""
    Write-Output "PatchPilot is up:  http://localhost:3000"
}
