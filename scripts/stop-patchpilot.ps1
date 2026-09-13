<#
.SYNOPSIS
    Stop the PatchPilot API and web UI.

.DESCRIPTION
    Stops whatever currently holds ports 8000 and 3000, identified by the port
    rather than by process name, so it cannot take down an unrelated python or
    node process that happens to be running.

    Sandbox containers are left alone: a run in flight owns one, and killing it
    from here would lose the run's work rather than stopping it cleanly.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Stop-PortOwner([int]$Port, [string]$Label) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Output "$Label not running on :$Port"
        return
    }
    foreach ($pid_ in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        try {
            $proc = Get-Process -Id $pid_ -ErrorAction Stop
            Stop-Process -Id $pid_ -Force -ErrorAction Stop
            Write-Output "$Label stopped (pid $pid_, $($proc.ProcessName))"
        } catch {
            Write-Output "$Label could not stop pid ${pid_}: $($_.Exception.Message)"
        }
    }
}

Stop-PortOwner 8000 'API     '
Stop-PortOwner 3000 'Frontend'

Start-Sleep -Seconds 2
$left = Get-NetTCPConnection -State Listen -LocalPort 8000, 3000 -ErrorAction SilentlyContinue
if ($left) {
    Write-Output ""
    Write-Output "still listening:"
    $left | Select-Object LocalPort, OwningProcess | Format-Table -AutoSize
} else {
    Write-Output ""
    Write-Output "both ports free"
}
