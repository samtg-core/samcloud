#
# samcloud bootstrap — Windows (PowerShell)
#
# Run with:
#   irm <console>/api/v1/bootstrap/<token>/ps1 | iex
#
# Does the same as bootstrap.sh but for native Windows:
#   1. Generate SSH keypair (ssh-keygen)
#   2. Activate with registry
#   3. Install gateway pubkey (handles administrators_authorized_keys)
#   4. SSH config entry
#   5. Tunnel as Windows Scheduled Task (if reverse-tunnel)
#   6. Device token
#   7. Verify
#
# Template variables (replaced by API):
#   __ENROLLMENT_TOKEN__  __REGISTRY_URL__  __DEVICE_ID__
#   __TUNNEL_PORT__  __CONTEXT__  __PLANE_HOST__  __PLANE_SSH_PORT__
#   __PLANE_SSH_USER__  __ENV__

$ErrorActionPreference = "Stop"

# ── Config ───────────────────────────────────────────────
$Token      = "__ENROLLMENT_TOKEN__"
$Api        = "__REGISTRY_URL__"
$Device     = "__DEVICE_ID__"
$Tunnel     = "__TUNNEL_PORT__"
$Context    = "__CONTEXT__"
$GwHost     = "__PLANE_HOST__"
$GwPort     = "__PLANE_SSH_PORT__"
$GwUser     = "__PLANE_SSH_USER__"
$DeployEnv  = "__ENV__"

# ── Env-namespacing (ticket #106) ────────────────────────
# prod keeps the original BARE names (existing devices need no migration).
# Any other env (e.g. staging) gets an <env>-scoped namespace so one
# machine/user can hold prod + staging routes at once; re-enrolling one env
# never clobbers the other (tasks/keys/config/token are all env-keyed).
if (-not $DeployEnv -or $DeployEnv -eq "prod") {
    $SshHost       = "samcloud-gw"
    $KeyPath       = "$env:USERPROFILE\.ssh\samcloud_$Device"
    $CfgDir        = "$env:USERPROFILE\.config\samcloud"
    $TaskName      = "samcloud-tunnel-$Device"
    $TaskGlob      = "samcloud-tunnel-*"
    $TunScript     = "tunnel-$Device.ps1"
    $CfgMark       = "# samcloud — bootstrap ($Device)"
    $CfgRegex      = "^# samcloud.*bootstrap"
} else {
    $SshHost       = "$DeployEnv-samcloud-gw"
    $KeyPath       = "$env:USERPROFILE\.ssh\samcloud_${DeployEnv}_$Device"
    $CfgDir        = "$env:USERPROFILE\.config\samcloud\$DeployEnv"
    $TaskName      = "samcloud-$DeployEnv-tunnel-$Device"
    $TaskGlob      = "samcloud-$DeployEnv-tunnel-*"
    $TunScript     = "tunnel-$Device.ps1"
    $CfgMark       = "# samcloud $DeployEnv-route ($Device)"
    $CfgRegex      = "^# samcloud $DeployEnv-route"
}
$TokenFile = "$CfgDir\device_token"
$Step       = 0

# ── UI ───────────────────────────────────────────────────
function Show-Step($name) { $script:Step++; Write-Host "`n--- $script:Step. $name ---" -ForegroundColor Cyan }
function Show-Ok($msg)    { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Show-Info($msg)  { Write-Host "  $msg" -ForegroundColor DarkGray }
function Show-Warn($msg)  { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Show-Fail($msg)  { Write-Host "  ✗ $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "╔═══════════════════════════════════════════════╗" -ForegroundColor Blue
Write-Host "║       samcloud  bootstrap (Windows)           ║" -ForegroundColor Blue
Write-Host "╚═══════════════════════════════════════════════╝" -ForegroundColor Blue
$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host ""
Write-Host "  Device:     $Device"
Write-Host "  Admin:      $IsAdmin"
if ($Tunnel) { Write-Host "  Tunnel:     port $Tunnel" }

# ══════════════════════════════════════════════════════════
# 1. SSH Directory + Keypair
# ══════════════════════════════════════════════════════════

Show-Step "SSH Setup"

$SshDir = "$env:USERPROFILE\.ssh"
if (-not (Test-Path $SshDir)) { New-Item -ItemType Directory -Path $SshDir -Force | Out-Null }

# Ensure known_hosts exists with correct permissions (prevents "failed to add host" error)
$KnownHosts = "$SshDir\known_hosts"
if (-not (Test-Path $KnownHosts)) {
    New-Item -ItemType File -Path $KnownHosts -Force | Out-Null
}
# Fix permissions — owner full control, no inheritance (Windows SSH is picky)
icacls $KnownHosts /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>$null | Out-Null
icacls $SshDir /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>$null | Out-Null
Show-Ok "SSH directory ready"

if (Test-Path $KeyPath) {
    Show-Ok "Exists: $KeyPath"
    $PubKey = Get-Content "${KeyPath}.pub" -Raw
} else {
    # CRITICAL: use cmd /c to avoid PowerShell UTF-16 redirect
    & ssh-keygen -t ed25519 -f $KeyPath -N '""' -C "samcloud:$Device" -q 2>$null
    if (-not (Test-Path $KeyPath)) {
        # Fallback: ssh-keygen may need different quoting on some Windows versions
        & ssh-keygen -t ed25519 -f $KeyPath -N "" -C "samcloud:$Device" -q 2>$null
    }
    $PubKey = Get-Content "${KeyPath}.pub" -Raw
    Show-Ok "Generated: $KeyPath"
}

# Make the private key usable by the SYSTEM-context tunnel task.
#
# Windows OpenSSH refuses any key whose ACL lets a non-owner-equivalent
# principal read it. ssh-keygen creates the file with an explicit grant to
# the running user (e.g. "Workstation 1"); `icacls /grant:r SYSTEM Admins`
# does NOT remove that explicit grant — /grant:r only replaces grants for
# the principals you name. So we have to rebuild the ACL from scratch:
#   - Owner: BUILTIN\Administrators (via takeown /A; satisfies ssh's
#     owner-equivalence check for the SYSTEM context).
#   - ACL: inheritance disabled, all existing ACEs stripped, then exactly
#     SYSTEM:Read + Administrators:Read added back. Admin users (incl. the
#     enrolling user) still read via the Administrators group; nobody else
#     has any access, so ssh stops complaining about "too open".
& takeown.exe /F $KeyPath /A 2>$null | Out-Null
$acl = Get-Acl -Path $KeyPath
$acl.SetAccessRuleProtection($true, $false)  # disable inheritance + drop inherited
foreach ($r in @($acl.Access)) { $null = $acl.RemoveAccessRule($r) }
$sysSid = New-Object System.Security.Principal.SecurityIdentifier "S-1-5-18"
$admSid = New-Object System.Security.Principal.SecurityIdentifier "S-1-5-32-544"
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sysSid, "Read", "Allow")))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $admSid, "Read", "Allow")))
Set-Acl -Path $KeyPath -AclObject $acl
Show-Info $PubKey.Substring(0, [Math]::Min(60, $PubKey.Length)) + "..."

# ══════════════════════════════════════════════════════════
# 2. SSH Target User
# ══════════════════════════════════════════════════════════
# OpenSSH's valid_user rejects names outside [A-Za-z0-9._-], so Windows
# display names like "Workstation 2" can never be SSH logins. If the running
# user's name isn't safe, provision a dedicated local admin ('samcloud') and
# use that as the SSH target. The gateway key goes into
# administrators_authorized_keys (step 3), which any admin shares.

Show-Step "SSH Target User"

$SshUserRe = '^[A-Za-z0-9._-]+$'
$SshTargetUser = $env:USERNAME
$SshTargetCreated = $false

$IsAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($SshTargetUser -match $SshUserRe) {
    Show-Ok "Using logged-in user: $SshTargetUser"
} else {
    Show-Warn "Logged-in user '$SshTargetUser' contains characters OpenSSH rejects"
    $SshTargetUser = "samcloud"
    if (-not $IsAdmin) {
        Show-Fail "Cannot provision local user '$SshTargetUser' — re-run as Administrator"
        exit 1
    }
    $existing = Get-LocalUser -Name $SshTargetUser -ErrorAction SilentlyContinue
    if ($existing) {
        Show-Ok "Local user '$SshTargetUser' already exists"
    } else {
        # Random password — never used for login (key auth only), but Windows requires one
        $bytes = New-Object byte[] 24
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $pw = ConvertTo-SecureString -AsPlainText -Force -String ([Convert]::ToBase64String($bytes) + "A1!")
        New-LocalUser -Name $SshTargetUser -Password $pw -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires `
            -Description "samcloud SSH target (admin, key-auth)" | Out-Null
        Show-Ok "Created local user '$SshTargetUser'"
        $SshTargetCreated = $true
    }
    # Idempotently ensure Administrators membership — sshd reads admin keys for admin users
    try {
        Add-LocalGroupMember -Group "Administrators" -Member $SshTargetUser -ErrorAction Stop
        Show-Ok "Added '$SshTargetUser' to Administrators"
    } catch {
        # Already a member → MemberExists exception; any other error bubbles up
        if ($_.Exception.GetType().Name -ne "MemberExistsException") {
            Show-Warn "Could not add to Administrators: $_"
        }
    }
}

# ══════════════════════════════════════════════════════════
# 3. Activate
# ══════════════════════════════════════════════════════════

Show-Step "Activate"

# The gateway reaches this machine at localhost:22, directly or through the reverse
# tunnel. If nothing answers there, the enrollment would succeed and the device would
# never be reachable. Checked before the token is consumed.
$SshdAnswers = $false
try {
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect("127.0.0.1", 22)
    $SshdAnswers = $probe.Connected
    $probe.Close()
} catch { $SshdAnswers = $false }
if (-not $SshdAnswers) {
    Show-Fail "Nothing answers on localhost:22; the gateway reaches this machine there."
    Show-Info "  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"
    Show-Info "  Set-Service sshd -StartupType Automatic; Start-Service sshd"
    Show-Info "Your enrollment token is still valid — re-run this command after starting it."
    exit 1
}
Show-Ok "sshd answers on localhost:22"

# The host key this machine's sshd presents, asked of the sshd itself rather than
# guessed from a file. Sent to /devices/activate so the gateway verifies the FIRST
# hop instead of learning whatever answers then. Empty is fine — the gateway falls
# back to learning it. Windows tunnels reach localhost:22 (see the -R target below).
$HostPubKey = ""
if (Get-Command ssh-keyscan -ErrorAction SilentlyContinue) {
    try {
        $scan = & ssh-keyscan -t ed25519 -p 22 -T 5 localhost 2>$null
        foreach ($line in $scan) {
            $parts = "$line".Split(" ")
            if ($parts.Length -ge 3 -and $parts[1] -eq "ssh-ed25519") {
                $HostPubKey = "$($parts[1]) $($parts[2])"
                break
            }
        }
    } catch { $HostPubKey = "" }
}
if ($HostPubKey) {
    Show-Ok "Host key read from local sshd"
} else {
    Show-Warn "Could not read this machine's SSH host key; the gateway will learn it on first hop"
}

$Body = @{
    token = $Token
    ssh_pubkey = $PubKey.Trim()
    startup_method = "scheduled-task"
    ssh_user = $SshTargetUser
    host_pubkey = $HostPubKey
} | ConvertTo-Json

try {
    $Resp = Invoke-RestMethod -Uri "$Api/api/v1/devices/activate" `
        -Method POST -ContentType "application/json" -Body $Body
    Show-Ok "Activated"
} catch {
    Show-Fail "Activation failed: $_"
    Show-Warn "Token may be expired or already used. Re-generate from admin."
    exit 1
}

$DevToken = $Resp.device_token
$GwPubKey = $Resp.gateway_pubkey
# The gateway's HOST keys (what this device checks the gateway BY) and the alias to
# file them under — distinct from $GwPubKey, the gateway's CLIENT key.
$GwHostKeyAlias = if ($Resp.gateway_host_key_alias) { $Resp.gateway_host_key_alias } else { "samcloud-gateway" }
$GwHostKeys = $Resp.gateway_host_keys
$KeysN    = if ($Resp.authorized_keys_synced) { $Resp.authorized_keys_synced.managed } else { "?" }
$SshUserWarning = $Resp.ssh_user_warning

Show-Ok "authorized_keys synced ($KeysN managed)"
if (-not (Test-Path $CfgDir)) { New-Item -ItemType Directory -Path $CfgDir -Force | Out-Null }
if (-not $GwHostKeys) {
    # Not in the activation answer: ask the published endpoint once more.
    try {
        $hk = Invoke-RestMethod -Uri "$Api/api/v1/gateway/host-key"
        $GwHostKeys = $hk.host_keys
        if ($hk.alias) { $GwHostKeyAlias = $hk.alias }
    } catch { }
}
# The tunnel verifies the gateway strictly against this file: with no key in it,
# every connect is refused. The tunnel script retries the fetch before each
# attempt, so this is reported, not fatal.
$khLines = @()
foreach ($k in @($GwHostKeys)) {
    $f = "$k".Split(' ')
    if ($f.Length -ge 2) { $khLines += "$GwHostKeyAlias $($f[0]) $($f[1])" }
}
if ($khLines.Count -gt 0) {
    Set-Content -Path "$CfgDir\known_hosts" -Value $khLines -Encoding ascii
    Show-Ok "Gateway host key pinned — the tunnel verifies the gateway it dials"
} else {
    Show-Fail "Could not get the gateway's host key; the tunnel refuses to connect until it has one"
    Show-Info "  Check: irm $Api/api/v1/gateway/host-key"
}
if (-not $DevToken) { Show-Fail "The activation answer carried no device token" }
if ($Resp.host_key_pinned -eq $true) {
    Show-Ok "Host key pinned — the gateway verifies this machine from its first hop"
} elseif ($Resp.host_key_pinned -eq $false) {
    Show-Info "A host key is already pinned for this device; this run did not change it"
}
if ($SshUserWarning) { Show-Warn $SshUserWarning }

# ══════════════════════════════════════════════════════════
# 4. Gateway Key
# ══════════════════════════════════════════════════════════

Show-Step "Gateway Access"

if ($GwPubKey) {
    # Windows sshd: admin users read from C:\ProgramData\ssh\administrators_authorized_keys
    # Regular users read from ~/.ssh/authorized_keys
    # Try admin path first, fall back to user path

    $AdminKeys = "C:\ProgramData\ssh\administrators_authorized_keys"
    $UserKeys  = "$env:USERPROFILE\.ssh\authorized_keys"

    if ($IsAdmin -and (Test-Path "C:\ProgramData\ssh")) {
        # Admin path — CRITICAL: write as ASCII, not UTF-16
        $existing = if (Test-Path $AdminKeys) { Get-Content $AdminKeys -Raw } else { "" }
        if ($existing -notmatch [regex]::Escape($GwPubKey.Trim())) {
            # Use cmd /c to write ASCII (PowerShell > writes UTF-16 which sshd rejects)
            cmd /c "echo $($GwPubKey.Trim()) >> `"$AdminKeys`""
            # Fix permissions: only SYSTEM and Administrators
            icacls $AdminKeys /inheritance:r /grant "SYSTEM:(F)" /grant "BUILTIN\Administrators:(F)" 2>$null | Out-Null
            Show-Ok "Gateway key → admin authorized_keys"
        } else {
            Show-Ok "Gateway key already in admin authorized_keys"
        }
    } else {
        # User path
        if (-not (Test-Path (Split-Path $UserKeys))) {
            New-Item -ItemType Directory -Path (Split-Path $UserKeys) -Force | Out-Null
        }
        $existing = if (Test-Path $UserKeys) { Get-Content $UserKeys -Raw } else { "" }
        if ($existing -notmatch [regex]::Escape($GwPubKey.Trim())) {
            cmd /c "echo $($GwPubKey.Trim()) >> `"$UserKeys`""
            Show-Ok "Gateway key → user authorized_keys"
        } else {
            Show-Ok "Gateway key already in user authorized_keys"
        }

        if (Test-Path "C:\ProgramData\ssh") {
            Show-Warn "Windows sshd detected but not running as admin"
            Show-Info "Admin users need key in: $AdminKeys"
            Show-Info "Re-run this script as Administrator if SSH access fails"
        }
    }
} else {
    Show-Warn "No gateway pubkey — gateway won't route to this device"
}

# ══════════════════════════════════════════════════════════
# 5. SSH Config
# ══════════════════════════════════════════════════════════

Show-Step "SSH Config"

$SshConfig = "$env:USERPROFILE\.ssh\config"

# Ticket #15: previous logic was "if Host samcloud-gw exists anywhere, skip" —
# which left a stale block from a pre-fix bootstrap (User alice, wrong IdentityFile,
# wrong port) sitting at the top of the file. SSH matches the FIRST block, so
# the new (correct) values appended below were ignored. Mirror bash's "clean
# and rewrite" instead: filter out any prior samcloud bootstrap block, then
# always append fresh.
if (Test-Path $SshConfig) {
    $lines = Get-Content $SshConfig
    $newLines = @()
    $skipping = $false
    $removed = $false
    foreach ($line in $lines) {
        if ($line -match $CfgRegex) {
            $skipping = $true
            $removed = $true
            continue
        }
        if ($skipping -and $line -match "^\s*StrictHostKeyChecking") {
            $skipping = $false
            continue
        }
        if (-not $skipping) { $newLines += $line }
    }
    if ($removed) {
        $newLines | Out-File -FilePath $SshConfig -Encoding ascii
        Show-Info "Removed prior samcloud bootstrap block(s)"
    }
}

$ConfigBlock = @"

$CfgMark
Host $SshHost
    HostName $GwHost
    Port $GwPort
    User $GwUser
    IdentityFile "$KeyPath"
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    StrictHostKeyChecking accept-new
"@
# Use Out-File with ASCII encoding (not PowerShell default UTF-16)
$ConfigBlock | Out-File -FilePath $SshConfig -Append -Encoding ascii
Show-Ok "Wrote fresh '$SshHost' block to $SshConfig"

# ══════════════════════════════════════════════════════════
# 6. Tunnel (Windows Scheduled Task)
# ══════════════════════════════════════════════════════════

if ($Tunnel) {
    Show-Step "Tunnel"

    # $TaskName defined in the env-namespacing block above

    # Sweep legacy + crossed-wire tasks first, then this device's prior task.
    # Devices that were enrolled multiple times (different device ids, or the
    # pre-samcloud WorkcloudTunnel) accumulate stale scheduled tasks that race
    # for the same tunnel port on the host. The unmodified samcloud-tunnel-$Device
    # cleanup only catches the current device's own prior task; this block
    # catches everything else samcloud-shaped that isn't ours.
    $legacyTasks = @()
    try {
        $legacyTasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
            ($_.TaskName -eq 'WorkcloudTunnel') -or
            ($_.TaskName -like $TaskGlob -and $_.TaskName -ne $TaskName)
        }
    } catch { }
    if ($legacyTasks -and -not $IsAdmin) {
        foreach ($t in $legacyTasks) { Show-Warn "Stale task '$($t.TaskName)' detected (need Administrator to remove)" }
    } elseif ($legacyTasks) {
        foreach ($t in $legacyTasks) {
            try { Stop-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue } catch { }
            try { Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false -ErrorAction Stop; Show-Info "Removed stale task '$($t.TaskName)'" } catch { Show-Warn "Could not remove '$($t.TaskName)': $_" }
        }
    }

    # Also sweep stale tunnel scripts left by prior enrolments under different
    # device names (e.g. tunnel-gpu-1-ps.ps1 lingering after a gpu-1 bootstrap
    # got run by mistake). Keep only tunnel-$Device.ps1.
    $TunnelDir = $CfgDir
    if (Test-Path $TunnelDir) {
        Get-ChildItem -Path $TunnelDir -Filter 'tunnel-*.ps1' -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne $TunScript } | ForEach-Object {
            try { Remove-Item $_.FullName -Force -ErrorAction Stop; Show-Info "Removed stale tunnel script '$($_.Name)'" } catch { Show-Warn "Could not remove '$($_.Name)': $_" }
        }
    }

    # Ticket #15: previous logic was "if scheduled task exists, skip" — which
    # left a stale task pointing at the old (pre-fix) tunnel script. The task
    # would keep firing and the actual tunnel kept failing silently. Mirror
    # bash's launchd cleanup: stop + unregister, then re-create.
    $existingTask = $null
    try { $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    if ($existingTask -and -not $IsAdmin) {
        Show-Warn "Existing task '$TaskName' detected but cannot be replaced (need Administrator)"
        Show-Info "Re-run this script as Administrator to refresh it"
    } elseif ($existingTask) {
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Show-Info "Removed prior scheduled task '$TaskName'"
    }

    if (-not $IsAdmin) {
        Show-Warn "Scheduled task requires admin — run as Administrator"
        Show-Info "Or start tunnel manually: ssh -N -R ${Tunnel}:localhost:22 $SshHost"
    } else {
        # Create a PowerShell script for the tunnel
        $TunnelScript = "$CfgDir\$TunScript"
        $TunnelDir = Split-Path $TunnelScript
        if (-not (Test-Path $TunnelDir)) { New-Item -ItemType Directory -Path $TunnelDir -Force | Out-Null }

        # Self-contained: inline -i / -p / -l / host so the script does NOT
        # depend on ~/.ssh/config. Required because the task runs as SYSTEM,
        # whose USERPROFILE is C:\Windows\system32\config\systemprofile, not
        # the enrolling user's home — SYSTEM can't see samcloud-gw Host block.
        # The gateway's host key is verified, against the key the registry publishes
        # (#825). The refresh sits INSIDE the loop, in front of every connect: a
        # gateway re-key then costs one failed attempt and ten seconds rather than
        # refusing forever, which is what a once-at-start refresh would have done.
        # known_hosts lives beside this script in CfgDir, which SYSTEM can read —
        # SYSTEM's USERPROFILE is not the enrolling user's, so the path is absolute
        # and nothing here depends on ~/.ssh/config.
        @"
# samcloud tunnel for $Device — auto-generated
`$Key   = '$KeyPath'
`$Kh    = '$CfgDir\known_hosts'
`$Alias = '$GwHostKeyAlias'
`$Api   = '$Api'
while (`$true) {
    # Refresh the pin. Written atomically, and only when the fetch parsed — a
    # network blip must never wipe a working pin. No fallback to an unverified
    # connect: with no pin, ssh refuses, which is the point.
    try {
        `$resp = Invoke-RestMethod -Uri "`$Api/api/v1/gateway/host-key" -TimeoutSec 10
        `$a = if (`$resp.alias) { `$resp.alias } else { `$Alias }
        `$lines = @()
        foreach (`$k in `$resp.host_keys) {
            `$f = "`$k".Split(' ')
            if (`$f.Length -ge 2) { `$lines += "`$a `$(`$f[0]) `$(`$f[1])" }
        }
        if (`$lines.Count -gt 0) {
            Set-Content -Path "`$Kh.new" -Value `$lines -Encoding ascii
            Move-Item -Path "`$Kh.new" -Destination `$Kh -Force
        }
    } catch { }
    try {
        & ssh -N -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=`"`$Kh`" -o HostKeyAlias=`$Alias -o HashKnownHosts=no -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -i `"`$Key`" -p $GwPort -l $GwUser -R ${Tunnel}:localhost:22 $GwHost 2>&1 | Out-Null
    } catch { }
    Start-Sleep -Seconds 10
}
"@ | Out-File -FilePath $TunnelScript -Encoding ascii

        Show-Ok "Tunnel script: $TunnelScript"

        # Run the task as SYSTEM via the ServiceAccount principal. The original
        # S4U principal path (run as the enrolling user, no stored password) is
        # blocked from Admin shells: S4U needs SeTcbPrivilege which Admins
        # don't get in their token — only SYSTEM has it, so requesting S4U for
        # any non-SYSTEM principal fails with "Access is denied". SYSTEM is
        # fine for the lifeline tunnel: it only invokes ssh.exe from PATH,
        # doesn't need a user profile or WSL access. WSL-dependent tunnels
        # belong inside WSL (systemd), not in a Windows scheduled task.
        $Action = New-ScheduledTaskAction `
            -Execute "powershell.exe" `
            -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$TunnelScript`""
        $Trigger = New-ScheduledTaskTrigger -AtStartup
        $Principal = New-ScheduledTaskPrincipal `
            -UserId "NT AUTHORITY\SYSTEM" `
            -LogonType ServiceAccount `
            -RunLevel Highest
        $Settings = New-ScheduledTaskSettingsSet `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -RestartCount 999 `
            -ExecutionTimeLimit (New-TimeSpan -Days 365) `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable

        try {
            Register-ScheduledTask -TaskName $TaskName -Action $Action `
                -Trigger $Trigger -Settings $Settings -Principal $Principal `
                -Description "samcloud reverse tunnel for $Device" `
                -Force | Out-Null
            Show-Ok "Scheduled task: $TaskName (RunAs=SYSTEM)"

            Start-ScheduledTask -TaskName $TaskName 2>$null
            Start-Sleep -Seconds 2
            $status = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
            if ($status -eq "Running") {
                Show-Ok "Tunnel started"
            } else {
                Show-Warn "Task state: $status — check: schtasks /Query /TN $TaskName"
            }
        } catch {
            Show-Warn "Could not create task: $_"
            Show-Info "Run manually: powershell -File `"$TunnelScript`""
        }
    }
}

# ══════════════════════════════════════════════════════════
# 7. Device Token
# ══════════════════════════════════════════════════════════

Show-Step "Device Token"

if ($DevToken) {
    if (-not (Test-Path $CfgDir)) { New-Item -ItemType Directory -Path $CfgDir -Force | Out-Null }
    $DevToken | Out-File -FilePath $TokenFile -Encoding ascii -NoNewline
    Show-Ok "Stored: $TokenFile"
} else {
    Show-Warn "No device token received"
}

# ══════════════════════════════════════════════════════════
# 8. Verify
# ══════════════════════════════════════════════════════════

Show-Step "Verify"

$Checks = 0; $Passed = 0
function Check($name, $test) {
    $script:Checks++
    if (& $test) { Show-Ok $name; $script:Passed++ } else { Show-Warn $name }
}

Check "SSH key"      { Test-Path $KeyPath }
Check "Device token" { Test-Path $TokenFile }
Check "SSH config"   { (Test-Path $SshConfig) -and ((Get-Content $SshConfig -Raw) -match "Host $SshHost") }
Check "SSH target '$SshTargetUser'" { $null -ne (Get-LocalUser -Name $SshTargetUser -ErrorAction SilentlyContinue) -or ($SshTargetUser -eq $env:USERNAME) }

if ($Tunnel) {
    Check "Tunnel task" { try { $null = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop; $true } catch { $false } }
    Check "Gateway host key" { (Test-Path "$CfgDir\known_hosts") -and ((Get-Content "$CfgDir\known_hosts" -Raw) -match "^$([regex]::Escape($GwHostKeyAlias)) ") }

    # A registered task is not a connected tunnel: one refused on every attempt keeps
    # retrying. ExitOnForwardFailure makes ssh exit unless the gateway accepted the
    # forward, so the tunnel's ssh still running across several looks is the
    # connection holding.
    Show-Info "Waiting for the tunnel to connect..."
    $TunnelUp = $false
    $seen = 0
    for ($i = 0; $i -lt 15; $i++) {
        $live = Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match [regex]::Escape("${Tunnel}:localhost:22") }
        if ($live) { $seen++ } else { $seen = 0 }
        if ($seen -ge 3) { $TunnelUp = $true; break }
        Start-Sleep -Seconds 2
    }
    Check "Tunnel connected" { $TunnelUp }
    if (-not $TunnelUp) { Show-Info "  Why: run it by hand: powershell -File `"$CfgDir\$TunScript`"" }
}

# ══════════════════════════════════════════════════════════
# Done
# ══════════════════════════════════════════════════════════

Write-Host ""
Write-Host "╔═══════════════════════════════════════════════╗" -ForegroundColor Blue
if ($Passed -eq $Checks) {
    Write-Host "║  ✓ Bootstrap complete  ($Passed/$Checks)               ║" -ForegroundColor Green
} else {
    Write-Host "║  ! Bootstrap partial   ($Passed/$Checks)               ║" -ForegroundColor Yellow
}
Write-Host "╚═══════════════════════════════════════════════╝" -ForegroundColor Blue
Write-Host ""
Write-Host "  $Device enrolled in samcloud"
if ($Tunnel) { Write-Host "  Tunnel: localhost:$Tunnel → this machine" }
Write-Host ""
