# tunnel-debug — diagnose why the SYSTEM-context tunnel task isn't binding.
#
# Run as Administrator. Writes to C:\Windows\Temp\samcloud-tunnel-debug.log
# and tails the result. Template vars are filled in by the registry at fetch
# time so this is self-contained (no need for the operator to know the key
# path, plane host, port, or tunnel port).

$ErrorActionPreference = "Continue"
$Device      = "__DEVICE_ID__"
$KeyPath     = "$env:USERPROFILE\.ssh\samcloud_$Device"
$GwHost      = "__PLANE_HOST__"
$GwPort      = "__PLANE_SSH_PORT__"
$GwUser      = "__PLANE_SSH_USER__"
$Log         = "C:\Windows\Temp\samcloud-tunnel-debug.log"

"=== samcloud tunnel-debug $(Get-Date -Format o) ===" | Out-File $Log
"device:   $Device"                                    | Out-File $Log -Append
"key:      $KeyPath"                                   | Out-File $Log -Append
"gateway:  $GwUser@$GwHost`:$GwPort"                   | Out-File $Log -Append
""                                                     | Out-File $Log -Append

"--- key exists ---"                                   | Out-File $Log -Append
"exists: $(Test-Path $KeyPath)"                        | Out-File $Log -Append
""                                                     | Out-File $Log -Append

"--- key ACL ---"                                      | Out-File $Log -Append
& icacls.exe $KeyPath 2>&1                             | Out-File $Log -Append
""                                                     | Out-File $Log -Append

"--- key owner ---"                                    | Out-File $Log -Append
(Get-Acl $KeyPath).Owner                               | Out-File $Log -Append
""                                                     | Out-File $Log -Append

"--- ssh -V ---"                                       | Out-File $Log -Append
& ssh -V 2>&1                                          | Out-File $Log -Append
""                                                     | Out-File $Log -Append

"--- ssh -v exit (as current user) ---"                | Out-File $Log -Append
& ssh -v -i $KeyPath -p $GwPort -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o UserKnownHostsFile=NUL -l $GwUser $GwHost exit 2>&1 | Out-File $Log -Append
""                                                     | Out-File $Log -Append

# ── Run the same ssh -v as SYSTEM via a one-shot scheduled task ────────
# Lets us see the error message SYSTEM-context ssh emits, which the tunnel
# task's `2>&1 | Out-Null` swallows. Uses the managed Register-ScheduledTask
# API (not schtasks /Create /TR ...) to avoid the argv-parsing landmine.

"--- ssh -v exit (as SYSTEM via temp task) ---"        | Out-File $Log -Append
$sysScript = "C:\Windows\Temp\samcloud-debug-sys.ps1"
$sysLog    = "C:\Windows\Temp\samcloud-debug-sys.log"

@"
`$ErrorActionPreference = 'Continue'
& ssh -v -i '$KeyPath' -p $GwPort -o StrictHostKeyChecking=no -o IdentitiesOnly=yes -o UserKnownHostsFile=NUL -l $GwUser $GwHost exit *> '$sysLog'
"@ | Set-Content $sysScript -Encoding ASCII

$action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$sysScript`""
$trigger   = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(2))
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$task      = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal
Register-ScheduledTask -TaskName "samcloud-debug-sys" -InputObject $task -Force | Out-Null

# Wait up to ~12s for the SYSTEM task to fire + complete
for ($i = 0; $i -lt 6; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Path $sysLog) { Start-Sleep -Seconds 1; break }
}

if (Test-Path $sysLog) {
    Get-Content $sysLog | Out-File $Log -Append
} else {
    "(no sys-log produced — SYSTEM task did not write)" | Out-File $Log -Append
}

Unregister-ScheduledTask -TaskName "samcloud-debug-sys" -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item $sysScript -ErrorAction SilentlyContinue
Remove-Item $sysLog -ErrorAction SilentlyContinue

Get-Content $Log | Select-Object -Last 80
