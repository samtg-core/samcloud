#
# samcloud WSL keepalive — Windows side (handle-holder)
#
# Run on the Windows host that owns a WSL sibling device. Installs a
# scheduled task whose only job is to hold the WSL VM alive: while
# `wsl.exe --exec sleep infinity` is running, Windows reference-counts the
# distro as in-use and refuses to shut it down. When the call returns
# (`wsl --shutdown`, Windows Defender termination, vmIdleTimeout, host
# reboot, etc.), the script's loop catches it within seconds and starts a
# fresh VM.
#
# This replaces the prior Hyper-V-event-18508 design: that only fired on
# guest-OS shutdown and missed every other failure mode (Defender kills,
# idle timeouts, NIC crashes). Holding a wsl.exe handle covers all of them.
#
# Run with:
#   irm <registry>/api/v1/devices/<device-id>/helpers/wsl-keepalive | iex
#
# Template variables (replaced by API):
#   __DEVICE_ID__  __WSL_DISTRO__

$ErrorActionPreference = "Stop"

$Device = "__DEVICE_ID__"
$Distro = "__WSL_DISTRO__"
$TaskName = "samcloud-wsl-keepalive-$Device"

$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $IsAdmin) {
    Write-Host "This helper must run as Administrator (registers a scheduled task with S4U principal)." -ForegroundColor Red
    exit 1
}

# WSL distros are per-user: only the user that registered the distro can
# reach it through wsl.exe. So the keepalive must run as that user — NOT as
# SYSTEM (can't see WSL), NOT as the bootstrap-created `samcloud` local
# account (didn't register the distro). We use the interactive user
# (`$env:USERNAME`) regardless of whether that name has OpenSSH-incompatible
# characters — the keepalive doesn't ssh, it just holds a `wsl.exe --exec`
# handle, so spaces/special chars in the username are fine here.
#
# Trade-off: registering with -User (Interactive logon) means the task only
# fires when this user is logged in. For workstations that auto-login on
# boot, that's the operating model. Truly headless boxes would need a
# different shape (DPAPI-stored password, or a SYSTEM-registered distro).
$RunAsUser = $env:USERNAME

Write-Host "Installing WSL keepalive for $Device (distro: $Distro, run-as: $RunAsUser)..." -ForegroundColor Cyan

# Write the keepalive script to disk
$ScriptDir  = "$env:USERPROFILE\.config\samcloud"
$ScriptPath = "$ScriptDir\wsl-keepalive-$Distro.ps1"
$LogPath    = "$ScriptDir\wsl-keepalive-$Distro.log"
if (-not (Test-Path $ScriptDir)) { New-Item -ItemType Directory -Path $ScriptDir -Force | Out-Null }

@"
# samcloud WSL keepalive for $Distro — auto-generated
# Holds the WSL VM alive by keeping wsl.exe --exec running.
`$LogPath = "$LogPath"
function Log(`$msg) {
    `$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path `$LogPath -Value "[`$ts] `$msg" -ErrorAction SilentlyContinue
}
Log "=== WSL keepalive starting for $Distro ==="
while (`$true) {
    try {
        # Blocks for the lifetime of the VM. Returns when WSL is shut down,
        # killed, or times out — at which point we immediately restart it.
        & wsl.exe -d $Distro --exec /bin/bash -c "sleep infinity" 2>&1 | Out-Null
        Log "wsl --exec returned (VM stopped); restarting in 5s"
    } catch {
        Log "wsl --exec threw: `$_"
    }
    Start-Sleep -Seconds 5
}
"@ | Out-File -FilePath $ScriptPath -Encoding ascii

Write-Host "  Script: $ScriptPath" -ForegroundColor DarkGray
Write-Host "  Log:    $LogPath" -ForegroundColor DarkGray

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""

$Trigger = New-ScheduledTaskTrigger -AtStartup

# Use Interactive logon (via -User on Register-ScheduledTask below) instead of
# -Principal -LogonType S4U: S4U requires SeTcbPrivilege which Administrators
# don't get in their token (only SYSTEM does — verified the hard way in
# bootstrap.ps1, see CHANGELOG v0.7.6). Interactive logon fires the task when
# the user is logged in, which is the assumed auto-login workstation model.
# COMPUTERNAME is the correct domain prefix for local users; USERDOMAIN may
# resolve to a different name on domain-joined boxes (the AD domain) that
# doesn't contain the local samcloud user.
$TaskUser = "$env:COMPUTERNAME\$RunAsUser"

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)

# Cleanly replace any prior task (event-trigger version, or stale handle-holder)
$existing = $null
try { $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
if ($existing) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  Removed prior task: $TaskName" -ForegroundColor DarkYellow
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $TaskUser `
    -RunLevel Highest `
    -Description "samcloud: hold WSL distro $Distro alive for $Device (handle-holder)" `
    -Force | Out-Null

Write-Host "  Registered scheduled task: $TaskName" -ForegroundColor Green
Write-Host "  Trigger: AtStartup, runs as $TaskUser (Interactive), RunLevel Highest" -ForegroundColor DarkGray

# Start it now so the VM is held alive without waiting for next reboot
Start-ScheduledTask -TaskName $TaskName 2>$null
Start-Sleep -Seconds 2
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq "Running") {
    Write-Host "  Task started (state: Running)" -ForegroundColor Green
} else {
    Write-Host "  Task state: $state — check: schtasks /Query /TN $TaskName /V /FO LIST" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done. WSL distro '$Distro' will be held alive across crashes, idle timeouts, and reboots." -ForegroundColor Green
