<#
.SYNOPSIS
    Start PatchPilot automatically when you log in.

.DESCRIPTION
    Registers a Scheduled Task that runs start-patchpilot.ps1 at logon, so the
    API and web UI come back on their own after a restart.

    A per-user logon task, not a service: it needs no administrator rights, it
    runs as you (so it sees your .env, your PATH and your Docker), and you can
    see and disable it in Task Scheduler like anything else.

    The start script is idempotent, so a stray extra run is harmless.

.PARAMETER Remove
    Unregister the task and leave nothing behind.

.PARAMETER DelaySeconds
    How long to wait after logon before starting. The default gives Docker
    Desktop and the network a moment to come up first; PatchPilot itself does
    not need Docker to start, only to execute a run.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register-autostart.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register-autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [int]$DelaySeconds = 45
)

$ErrorActionPreference = 'Stop'

$TaskName = 'PatchPilot'
$startScript = Join-Path $PSScriptRoot 'start-patchpilot.ps1'

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed the '$TaskName' logon task. PatchPilot will no longer start by itself."
    } else {
        Write-Output "No '$TaskName' task registered; nothing to remove."
    }
    return
}

if (-not (Test-Path $startScript)) {
    throw "start-patchpilot.ps1 not found next to this script ($startScript)"
}

# -NoWait: nothing is reading the output at logon, and the task should finish
# promptly rather than sitting for the health-check timeout.
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ' +
               "-File `"$startScript`" -NoWait")

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT${DelaySeconds}S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description 'Starts the PatchPilot API and web UI at logon.' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Force | Out-Null

Write-Output "Registered the '$TaskName' logon task."
Write-Output ""
Write-Output "  starts      : $startScript"
Write-Output "  when        : at logon for $env:USERNAME, after ${DelaySeconds}s"
Write-Output "  turn off    : powershell -ExecutionPolicy Bypass -File scripts\register-autostart.ps1 -Remove"
Write-Output "  run it now  : Start-ScheduledTask -TaskName $TaskName"
