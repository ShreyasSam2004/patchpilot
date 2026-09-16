<#
.SYNOPSIS
    Keep PatchPilot running: start it at logon, and restart it if it stops.

.DESCRIPTION
    Registers a Scheduled Task that runs start-patchpilot.ps1 at logon and then
    every few minutes afterwards.

    The repeat is the point, and the first version of this got it wrong. An
    AtLogOn trigger fires on an actual logon and nothing else, so a laptop that
    sleeps and resumes — or a crash, or anything that stops the processes —
    left PatchPilot down until someone noticed. Because the start script does
    nothing when both ports are already busy, running it on a timer costs two
    port checks and turns "start it once" into "keep it running".

    A per-user task, not a service: it needs no administrator rights, it runs as
    you (so it sees your .env, your PATH and your Docker), and you can see and
    disable it in Task Scheduler like anything else.

.PARAMETER Remove
    Unregister the task and leave nothing behind.

.PARAMETER DelaySeconds
    How long to wait after logon before the first start. The default gives
    Docker Desktop and the network a moment; PatchPilot itself does not need
    Docker to start, only to execute a run.

.PARAMETER CheckMinutes
    How often to re-check afterwards. Set to 0 for logon-only behaviour.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register-autostart.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register-autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [int]$DelaySeconds = 45,
    [int]$CheckMinutes = 10
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

$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT${DelaySeconds}S"
$triggers = @($logon)

if ($CheckMinutes -gt 0) {
    # A separate trigger, not a repetition hung off the logon one. Repetition
    # only begins once its trigger fires, so attaching it to logon leaves it
    # dormant until the *next* logon -- exactly the gap this is meant to close,
    # and invisible except that the task reports no next run time. Anchored in
    # the past with StartWhenAvailable, this one is due immediately and then
    # every CheckMinutes for good.
    $supervisor = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-1) `
        -RepetitionInterval (New-TimeSpan -Minutes $CheckMinutes)
    $triggers += $supervisor
}

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
    -Trigger $triggers `
    -Settings $settings `
    -Force | Out-Null

Write-Output "Registered the '$TaskName' task."
Write-Output ""
Write-Output "  starts      : $startScript"
if ($CheckMinutes -gt 0) {
    Write-Output "  when        : at logon for $env:USERNAME (after ${DelaySeconds}s), then every ${CheckMinutes} min"
    Write-Output "  behaviour   : a check finds it already up and does nothing; if it is down, it starts it"
} else {
    Write-Output "  when        : at logon for $env:USERNAME, after ${DelaySeconds}s"
}
Write-Output "  turn off    : powershell -ExecutionPolicy Bypass -File scripts\register-autostart.ps1 -Remove"
Write-Output "  run it now  : Start-ScheduledTask -TaskName $TaskName"
