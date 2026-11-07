# WSL_BOOT_VERSION: 1.0.0
# samcloud-wsl-boot.ps1 - WSL persistence as a bootstrap primitive (#231).
#
# WHY THIS EXISTS: WSL2 terminates a distro shortly after its last wsl.exe
# client exits, even with systemd + daemons running inside - background
# services do NOT pin the VM. Any samcloud rail living in WSL silently dies
# minutes after boot unless a foreground wsl.exe client holds the distro open.
# Proven live on a Windows workstation 2026-07-25 (three reboots; see ticket #231).
#
# WHAT IT DOES (as scheduled task SamcloudWSLBoot, AtStartup + S4U, elevated):
#   1. starts the distro (systemd -> daemons -> restart-policy containers)
#   2. optionally re-syncs netsh portproxy + firewall rules to the CURRENT
#      WSL IP for ports listed in the conf (stale-IP portproxy after reboot
#      is the classic silent breakage)
#   3. holds a foreground `wsl.exe --exec sleep infinity` forever - the pin
#   4. if the keepalive dies (WSL bounced), exits 1 so Task Scheduler's
#      restart policy re-runs the whole loop: restart + re-sync + new pin
#
# CONF (same directory, samcloud-wsl-boot.conf, KEY=VALUE):
#   DISTRO=Ubuntu          # omit to use the default distro
#   PORTS=8080,8081        # optional; TCP only (portproxy can't do UDP)
#
# ONE-TIME REGISTration (elevated PowerShell, any admin, once):
#   powershell -ExecutionPolicy Bypass -File <this file> -Register
# Everything else - including future updates to this file - is unattended:
# the samcloud listener inside WSL self-updates this script in place when the
# registry serves a newer WSL_BOOT_VERSION (the task runs whatever is here).
param([switch]$Register)

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $here "samcloud-wsl-boot.log"
function Log($m) { "$([DateTime]::Now.ToString('s')) $m" | Out-File -Append -Encoding ascii $log }

# --- conf ---
$distro = ""; $ports = @()
$conf = Join-Path $here "samcloud-wsl-boot.conf"
if (Test-Path $conf) {
  foreach ($line in Get-Content $conf) {
    if ($line -match '^\s*DISTRO\s*=\s*(\S+)') { $distro = $Matches[1] }
    if ($line -match '^\s*PORTS\s*=\s*(\S+)') {
      $ports = $Matches[1] -split '[,\s]+' | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ }
    }
  }
}
$dargs = @(); if ($distro) { $dargs = @("-d", $distro) }

if ($Register) {
  $existing = Get-ScheduledTask -TaskName "SamcloudWSLBoot" -ErrorAction SilentlyContinue
  if ($existing) { Unregister-ScheduledTask -TaskName "SamcloudWSLBoot" -Confirm:$false }
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$($MyInvocation.MyCommand.Path)`""
  $trigger = New-ScheduledTaskTrigger -AtStartup
  # S4U: fires with NOBODY logged in, no stored password, still sees the
  # per-user WSL distros (SYSTEM does not).
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365)
  Register-ScheduledTask -TaskName "SamcloudWSLBoot" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null
  Write-Output "registered: SamcloudWSLBoot (AtStartup + S4U + Highest) -> $($MyInvocation.MyCommand.Path)"
  Write-Output "starting it now..."
  Start-ScheduledTask -TaskName "SamcloudWSLBoot"
  exit 0
}

Log "=== boot task start (distro='$distro' ports=$($ports -join ',')) ==="
& wsl.exe @dargs -u root /bin/true | Out-Null
Start-Sleep -Seconds 8

if ($ports.Count -gt 0) {
  $ip = ""
  foreach ($i in 1..10) {
    $raw = (& wsl.exe @dargs hostname -I)
    if ($raw) { $ip = "$raw".Trim().Split(" ")[0] }
    if ($ip) { break }
    Start-Sleep -Seconds 3
  }
  if ($ip) {
    foreach ($p in $ports) {
      netsh interface portproxy delete v4tov4 listenport=$p listenaddress=0.0.0.0 | Out-Null
      netsh interface portproxy add v4tov4 listenport=$p listenaddress=0.0.0.0 connectport=$p connectaddress=$ip | Out-Null
      if (-not (Get-NetFirewallRule -DisplayName "samcloud-wsl-$p" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "samcloud-wsl-$p" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $p | Out-Null
      }
    }
    Log "portproxy + firewall synced to $ip for ports $($ports -join ',')"
  } else {
    Log "WARN: no WSL IP resolved - portproxy sync skipped this round"
  }
}

Log "holding wsl keepalive client (task stays running; pin for the distro)"
& wsl.exe @dargs --exec sleep infinity
Log "keepalive exited - WSL bounced; exit 1 so the task restarts and re-syncs"
exit 1
