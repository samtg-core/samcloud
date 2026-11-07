"""
samcloud Enrollment E2E Simulation

End-to-end simulation of the enrollment lifecycle:
1. Access route enrollment (phone) — immediate activation
2. Access target enrollment (GPU server) — bootstrap → activate
3. Both-mode enrollment (Mac) — bootstrap + access key
4. Bootstrap script serving and template rendering
5. Service registration on enrolled devices
6. Access key generation and revocation
7. Full gateway key sync simulation

Uses isolated temp DB, never touches production.

Usage:
    python sandbox/test_enrollment_e2e.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REGISTRY_DIR = Path(__file__).parent.parent / "registry"
SANDBOX_PORT = 7781

# Reuse harness classes
sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox, TestResult


# =====================================================================
# Scenarios
# =====================================================================

def scenario_1_phone_enrollment(sandbox: Sandbox) -> TestResult:
    """Phone enrollment: access route only, immediate activation, access key generation."""
    r = TestResult("Scenario 1: Phone Enrollment (Access Route)")

    # Enroll a phone — should activate immediately
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "alices-phone",
        "name": "Alice's phone",
        "type": "phone",
        "access_route": "ssh-tunnel",
    })
    r.check("Phone enrollment succeeds", resp["status"] == 200)
    data = resp["data"]
    r.check("Immediate activation (no pending)",
            data.get("status") == "online")
    r.check("No bootstrap token (not needed)",
            "token" not in data)
    r.check("No tunnel port (access-only device)",
            "tunnel_port" not in data)

    # Verify device is active, not pending
    resp = sandbox.api("GET", "/api/v1/devices/alices-phone")
    r.check("Device retrievable", resp["status"] == 200)
    device = resp["data"]
    r.check("Status is online", device.get("status") == "online")
    r.check("Enrolled flag set", device.get("enrolled") == 1)
    r.check("Type stored", device.get("type") == "phone")
    r.check("Inbound method stored",
            device.get("inbound_method") == "ssh-tunnel")
    r.check("No outbound method", device.get("outbound_method") is None)

    # Not in pending list
    resp = sandbox.api("GET", "/api/v1/enroll/pending")
    pending_ids = [d["id"] for d in resp["data"]]
    r.check("Not in pending list", "alices-phone" not in pending_ids)

    # Create admin user for access key generation
    sandbox.api("POST", "/api/v1/init", {
        "username": "admin", "password": "admin123"
    })

    # Generate access key
    resp = sandbox.api("POST", "/api/v1/access-keys", {
        "username": "admin",
        "device_name": "alices-phone",
    })
    r.check("Access key generated", resp["status"] == 200)
    key_data = resp["data"]
    r.check("Key ID correct",
            key_data.get("key_id") == "admin@alices-phone")
    r.check("Private key returned",
            "OPENSSH PRIVATE KEY" in key_data.get("private_key", ""))
    r.check("Public key returned",
            key_data.get("public_key", "").startswith("ssh-ed25519"))
    r.check("Warning about private key",
            "private key" in key_data.get("warning", "").lower())

    # Verify key appears in access keys list
    resp = sandbox.api("GET", "/api/v1/access-keys")
    keys = resp["data"]
    r.check("Key in access keys list",
            any(k.get("key_id") == "admin@alices-phone" for k in keys))

    return r


def scenario_2_gpu_server_enrollment(sandbox: Sandbox) -> TestResult:
    """GPU server enrollment: access target, bootstrap → activate → register service."""
    r = TestResult("Scenario 2: GPU Server Enrollment (Access Target)")

    # Enroll a GPU server with reverse tunnel
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "gpu-2",
        "name": "GPU-2 RTX 6000",
        "type": "gpu-workstation",
        "access_target": "reverse-tunnel",
        "target_ssh_user": "you",
    })
    r.check("Server enrollment succeeds", resp["status"] == 200)
    data = resp["data"]
    r.check("Status is pending", data.get("status") == "pending")
    r.check("Bootstrap token returned",
            data.get("token", "").startswith("sc_enroll_"))
    r.check("Tunnel port allocated",
            data.get("tunnel_port") is not None and data["tunnel_port"] >= 10001)

    token = data["token"]
    tunnel_port = data["tunnel_port"]

    # Verify pending state
    resp = sandbox.api("GET", "/api/v1/devices/gpu-2")
    r.check("Device in pending state",
            resp["data"].get("status") == "pending")
    r.check("Outbound method stored",
            resp["data"].get("outbound_method") == "reverse-tunnel")
    r.check("SSH user stored",
            resp["data"].get("outbound_ssh_user") == "you")

    # In pending list
    resp = sandbox.api("GET", "/api/v1/enroll/pending")
    pending_ids = [d["id"] for d in resp["data"]]
    r.check("In pending list", "gpu-2" in pending_ids)

    # Fetch bootstrap script
    bootstrap_resp = urllib.request.urlopen(
        f"{sandbox.base_url}/api/v1/bootstrap/{token}")
    script = bootstrap_resp.read().decode()
    r.check("Bootstrap script served", bootstrap_resp.status == 200)
    r.check("Script contains device ID", "gpu-2" in script)
    r.check("Script contains token", token in script)
    r.check("Script contains tunnel port", str(tunnel_port) in script)
    r.check("Template vars fully replaced",
            "__ENROLLMENT_TOKEN__" not in script
            and "__DEVICE_ID__" not in script
            and "__TUNNEL_PORT__" not in script)

    # Activate (simulating what bootstrap.sh does)
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestGPU samcloud:gpu-2",
        "startup_method": "systemd",
    })
    r.check("Activation succeeds", resp["status"] == 200)
    activate_data = resp["data"]
    r.check("Status is enrolled",
            activate_data.get("status") == "enrolled")
    r.check("Device token returned",
            activate_data.get("device_token", "").startswith("sc_device_"))
    r.check("Tunnel port preserved",
            activate_data.get("tunnel_port") == tunnel_port)

    device_token = activate_data["device_token"]

    # Verify active state
    resp = sandbox.api("GET", "/api/v1/devices/gpu-2")
    r.check("Device now online", resp["data"].get("status") == "online")
    r.check("SSH pubkey stored",
            "ssh-ed25519" in resp["data"].get("ssh_pubkey", ""))

    # No longer pending
    resp = sandbox.api("GET", "/api/v1/enroll/pending")
    pending_ids = [d["id"] for d in resp["data"]]
    r.check("No longer pending", "gpu-2" not in pending_ids)

    # Register a service using device token
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "nerfstudio",
        "port": 8420,
        "health_endpoint": "/health",
        "subdomain": "spl-test",
    }, headers={"Authorization": f"Bearer {device_token}"})
    r.check("Service registration with device token",
            resp["status"] == 200)
    r.check("Service ID correct",
            resp["data"].get("service_id") == "gpu-2/nerfstudio")
    r.check("Slice port allocated",
            resp["data"].get("slice_port") is not None)

    return r


def scenario_3_dual_mode_enrollment(sandbox: Sandbox) -> TestResult:
    """Dual-mode enrollment: Mac that both accesses AND provides services."""
    r = TestResult("Scenario 3: Dual-Mode Enrollment (Both)")

    # Enroll with both access_route and access_target
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "alices-laptop",
        "name": "Alice's laptop",
        "type": "laptop",
        "access_route": "lan",
        "access_target": "direct-ssh",
        "target_ip": "192.0.2.50",
        "target_ssh_user": "you",
    })
    r.check("Dual-mode enrollment succeeds", resp["status"] == 200)
    data = resp["data"]
    r.check("Status is pending (needs bootstrap for target)",
            data.get("status") == "pending")
    r.check("Has bootstrap token",
            data.get("token", "").startswith("sc_enroll_"))
    r.check("No tunnel port (direct-ssh, not reverse-tunnel)",
            "tunnel_port" not in data or data.get("tunnel_port") is None)

    token = data["token"]

    # Check stored config
    resp = sandbox.api("GET", "/api/v1/devices/alices-laptop")
    device = resp["data"]
    r.check("Inbound method stored",
            device.get("inbound_method") == "lan")
    r.check("Outbound method stored",
            device.get("outbound_method") == "direct-ssh")
    r.check("Outbound IP stored",
            device.get("outbound_ip") == "192.0.2.50")
    r.check("SSH user stored",
            device.get("outbound_ssh_user") == "you")

    # Activate the target side
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMacKey samcloud:alices-laptop",
        "startup_method": "launchd",
    })
    r.check("Activation succeeds", resp["status"] == 200)

    # Now also generate access key (for the route side)
    resp = sandbox.api("POST", "/api/v1/access-keys", {
        "username": "admin",
        "device_name": "alices-laptop",
    })
    r.check("Access key generated for dual-mode device",
            resp["status"] == 200)
    r.check("Key ID correct",
            resp["data"].get("key_id") == "admin@alices-laptop")

    # Device should be fully active now
    resp = sandbox.api("GET", "/api/v1/devices/alices-laptop")
    r.check("Device online after activation",
            resp["data"].get("status") == "online")
    r.check("Both inbound and outbound configured",
            resp["data"].get("inbound_method") == "lan"
            and resp["data"].get("outbound_method") == "direct-ssh")

    return r


def scenario_4_lan_server_enrollment(sandbox: Sandbox) -> TestResult:
    """LAN server enrollment: direct-ssh target, no tunnel needed."""
    r = TestResult("Scenario 4: LAN Server (Direct SSH Target)")

    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "mediahost",
        "name": "Media Server",
        "type": "media-server",
        "access_target": "direct-ssh",
        "target_ip": "192.0.2.32",
        "target_ssh_user": "you",
        "target_ssh_port": 22,
    })
    r.check("LAN server enrollment succeeds", resp["status"] == 200)
    data = resp["data"]
    r.check("Status is pending", data.get("status") == "pending")
    r.check("Has token (needs bootstrap even on LAN)",
            "token" in data)
    r.check("No tunnel port allocated (direct-ssh)",
            "tunnel_port" not in data or data.get("tunnel_port") is None)

    # Activate
    token = data["token"]
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI mediahost",
    })
    r.check("LAN server activated", resp["status"] == 200)

    device_token = resp["data"]["device_token"]

    # Register multiple services
    for svc in [
        {"name": "mediaserver", "port": 8080, "subdomain": "media-test"},
        {"name": "indexer", "port": 8081},
        {"name": "radarr", "port": 7878, "subdomain": "rdr-test"},
    ]:
        svc["health_endpoint"] = "/health"
        resp = sandbox.api("POST", "/api/v1/services/register", svc,
                           headers={"Authorization": f"Bearer {device_token}"})
        r.check(f"Service {svc['name']} registered", resp["status"] == 200)

    # Verify services
    resp = sandbox.api("GET", "/api/v1/devices/mediahost")
    services = resp["data"].get("services", [])
    r.check("All 3 services registered",
            len(services) == 3,
            f"found {len(services)}")

    return r


def scenario_5_error_cases(sandbox: Sandbox) -> TestResult:
    """Error cases: duplicates, invalid tokens, missing fields."""
    r = TestResult("Scenario 5: Error Cases")

    # Duplicate enrollment
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "alices-phone",
        "name": "Duplicate",
        "access_route": "ssh-tunnel",
    })
    r.check("Duplicate device rejected (409)",
            resp["status"] == 409)

    # Invalid bootstrap token
    try:
        urllib.request.urlopen(
            f"{sandbox.base_url}/api/v1/bootstrap/sc_enroll_fakefakefake")
    except urllib.error.HTTPError as e:
        r.check("Invalid bootstrap token returns 404", e.code == 404)
    else:
        r.check("Invalid bootstrap token returns 404", False)

    # Non-enrollment token format
    try:
        urllib.request.urlopen(
            f"{sandbox.base_url}/api/v1/bootstrap/not_a_token")
    except urllib.error.HTTPError as e:
        r.check("Non-token bootstrap URL returns 404", e.code == 404)
    else:
        r.check("Non-token bootstrap URL returns 404", False)

    # Activate with bad token
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": "sc_enroll_0000000000000000000000000000000",
        "ssh_pubkey": "ssh-ed25519 AAAA fake",
    })
    r.check("Bad token activation rejected (401)",
            resp["status"] == 401)

    # Access key for non-existent user
    resp = sandbox.api("POST", "/api/v1/access-keys", {
        "username": "nonexistent",
        "device_name": "whatever",
    })
    r.check("Access key for missing user rejected (404)",
            resp["status"] == 404)

    # Revoke non-existent key
    resp = sandbox.api("DELETE", "/api/v1/access-keys/nobody@nothing")
    r.check("Revoke missing key returns 404", resp["status"] == 404)

    return r


def scenario_6_access_key_lifecycle(sandbox: Sandbox) -> TestResult:
    """Access key lifecycle: generate, list, revoke, verify removal."""
    r = TestResult("Scenario 6: Access Key Lifecycle")

    # Create a second user
    sandbox.api("POST", "/api/v1/users", {
        "username": "robin", "password": "hello", "role": "user"
    })

    # Generate keys for different users and devices
    for user, device in [("admin", "admin-laptop"), ("robin", "robins-phone")]:
        resp = sandbox.api("POST", "/api/v1/access-keys", {
            "username": user, "device_name": device,
        })
        r.check(f"Key generated for {user}@{device}", resp["status"] == 200)

    # List all keys
    resp = sandbox.api("GET", "/api/v1/access-keys")
    all_keys = resp["data"]
    r.check("All keys listed",
            len(all_keys) >= 4,  # admin@alices-phone, admin@alices-laptop, admin@admin-laptop, robin@robins-phone
            f"found {len(all_keys)} keys")

    # List filtered by user
    resp = sandbox.api("GET", "/api/v1/access-keys?username=robin")
    robin_keys = resp["data"]
    r.check("User filter works",
            len(robin_keys) == 1 and robin_keys[0].get("username") == "robin")

    # Revoke a key
    resp = sandbox.api("DELETE", "/api/v1/access-keys/robin@robins-phone")
    r.check("Key revoked successfully", resp["status"] == 200)

    # Verify revoked key is gone
    resp = sandbox.api("GET", "/api/v1/access-keys?username=robin")
    r.check("Revoked key no longer listed",
            len(resp["data"]) == 0)

    # Verify other keys still exist
    resp = sandbox.api("GET", "/api/v1/access-keys")
    remaining = resp["data"]
    r.check("Other keys unaffected",
            len(remaining) >= 3)

    return r


def scenario_7_audit_trail(sandbox: Sandbox) -> TestResult:
    """Verify the audit trail captures the full enrollment lifecycle."""
    r = TestResult("Scenario 7: Audit Trail")

    resp = sandbox.api("GET", "/api/v1/audit?limit=100")
    all_actions = [a["action"] for a in resp["data"]]

    expected = [
        "device.enroll.created",    # enrollment invitations
        "device.activated",          # bootstrap activations
        "token.enrollment.created",  # enrollment token creation
        "token.device.created",      # device token creation
        "access_key.generated",      # SSH key generation
        "access_key.revoked",        # key revocation
    ]

    for action in expected:
        r.check(f"Audit has '{action}'",
                action in all_actions,
                f"{'found' if action in all_actions else 'missing'}")

    # Check enrollment details include new fields
    enroll_entries = [a for a in resp["data"]
                      if a["action"] == "device.enroll.created"]
    if enroll_entries:
        detail = json.loads(enroll_entries[0].get("detail", "{}"))
        r.check("Enrollment audit has access_route/target info",
                "access_route" in detail or "access_target" in detail,
                f"detail keys: {list(detail.keys())}")

    return r


def scenario_8_gateway_key_management(sandbox: Sandbox) -> TestResult:
    """Gateway keypair generation, storage, and distribution via activate."""
    r = TestResult("Scenario 8: Gateway Key Management")

    # Generate gateway keypair
    resp = sandbox.api("POST", "/api/v1/gateway/keygen")
    r.check("Gateway keygen succeeds", resp["status"] == 200)
    data = resp["data"]
    r.check("Public key returned",
            data.get("public_key", "").startswith("ssh-ed25519"))
    gateway_pub = data.get("public_key", "")

    # Calling again returns existing (not re-generated)
    resp2 = sandbox.api("POST", "/api/v1/gateway/keygen")
    r.check("Second keygen returns existing",
            resp2["data"].get("status") == "exists")
    r.check("Same public key",
            resp2["data"].get("public_key") == gateway_pub)

    # Fetch pubkey via GET
    resp = sandbox.api("GET", "/api/v1/gateway/pubkey")
    r.check("GET pubkey works", resp["status"] == 200)
    r.check("Matches keygen output",
            resp["data"].get("public_key") == gateway_pub)

    # Gateway status includes key info
    resp = sandbox.api("GET", "/api/v1/gateway/status")
    r.check("Status shows key exists",
            resp["data"].get("gateway_key_exists") is True)

    # Enroll + activate a device — should return gateway pubkey
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "gw-key-test",
        "name": "GW Key Test",
        "access_target": "direct-ssh",
        "target_ip": "192.0.2.100",
    })
    token = resp["data"]["token"]

    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAA gw-key-test",
    })
    r.check("Activate returns gateway pubkey",
            resp["data"].get("gateway_pubkey", "").startswith("ssh-ed25519"))
    r.check("Gateway pubkey matches",
            resp["data"].get("gateway_pubkey") == gateway_pub)

    return r


def scenario_9_bootstrap_script_serving(sandbox: Sandbox) -> TestResult:
    """Bootstrap script endpoint serves rendered script with correct variables."""
    r = TestResult("Scenario 9: Bootstrap Script Serving")

    # Enroll a target device
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "bootstrap-serve-test",
        "name": "Bootstrap Serve Test",
        "access_target": "reverse-tunnel",
        "target_ssh_user": "testuser",
    })
    token = resp["data"]["token"]
    tunnel_port = resp["data"].get("tunnel_port")

    # Fetch bootstrap script
    bootstrap_resp = urllib.request.urlopen(
        f"{sandbox.base_url}/api/v1/bootstrap/{token}")
    script = bootstrap_resp.read().decode()
    r.check("Bootstrap script served (200)", bootstrap_resp.status == 200)

    # Verify all template variables are filled
    r.check("Device ID filled", "bootstrap-serve-test" in script)
    r.check("Token filled", token in script)
    r.check("Tunnel port filled",
            str(tunnel_port) in script if tunnel_port else True)
    r.check("No unfilled templates",
            "__ENROLLMENT_TOKEN__" not in script
            and "__DEVICE_ID__" not in script
            and "__TUNNEL_PORT__" not in script
            and "__REGISTRY_URL__" not in script)

    # Script should reference gateway pubkey installation
    r.check("Script installs gateway pubkey",
            "GATEWAY_PUBKEY" in script or "gateway" in script.lower())

    # Invalid token returns 404
    try:
        urllib.request.urlopen(
            f"{sandbox.base_url}/api/v1/bootstrap/sc_enroll_fake")
    except urllib.error.HTTPError as e:
        r.check("Invalid token returns 404", e.code == 404)
    else:
        r.check("Invalid token returns 404", False)

    return r


def scenario_10_retoken(sandbox: Sandbox) -> TestResult:
    """Re-generate enrollment token for pending device."""
    r = TestResult("Scenario 10: Token Regeneration")

    # Enroll a device
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "retoken-test",
        "name": "Retoken Test",
        "access_target": "direct-ssh",
        "target_ip": "198.51.100.1",
    })
    old_token = resp["data"]["token"]
    r.check("Initial enrollment", resp["status"] == 200)

    # Re-generate token
    resp = sandbox.api("POST", "/api/v1/enroll/retoken-test/retoken")
    r.check("Retoken succeeds", resp["status"] == 200)
    new_token = resp["data"]["token"]
    r.check("New token differs from old",
            new_token != old_token)
    r.check("New token is valid format",
            new_token.startswith("sc_enroll_"))

    # Old token should be invalid
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": old_token,
        "ssh_pubkey": "ssh-ed25519 AAAA old",
    })
    r.check("Old token rejected", resp["status"] == 401)

    # New token works
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": new_token,
        "ssh_pubkey": "ssh-ed25519 AAAA new",
    })
    r.check("New token accepted", resp["status"] == 200)

    # Retoken on an ACTIVE device is ALLOWED — it's the re-bootstrap path
    # (key loss / redeploy / lockout recovery), as the endpoint documents and
    # as we use in practice. The fresh token re-activates and overwrites the
    # pubkey. (Was previously asserted as 409, which never matched the design.)
    resp = sandbox.api("POST", "/api/v1/enroll/retoken-test/retoken")
    r.check("Retoken on active device allowed (re-bootstrap path)",
            resp["status"] == 200 and resp["data"].get("token", "").startswith("sc_enroll_"))

    return r


def scenario_11_setup_script_gateway_fixes(sandbox: Sandbox) -> TestResult:
    """Access-key setup-script: anchored grep (no false-match), RequestTTY auto, hint.

    Regression guard for ticket #84 — a bare `grep -q "Host samcloud"` false-matched an
    existing `Host samcloud-pi-1` alias and silently skipped writing the real block.
    """
    r = TestResult("Scenario 11: Setup-Script Gateway Fixes (#84)")

    # A user must exist for the setup-script endpoint
    sandbox.api("POST", "/api/v1/users", {
        "username": "gwuser", "password": "hunter2", "role": "user"
    })

    # Fetch the generated setup-script (returns text/x-shellscript, not JSON)
    req = urllib.request.Request(
        f"{sandbox.base_url}/api/v1/access-keys/setup-script",
        data=json.dumps({"username": "gwuser", "device_name": "gw-route-test"}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {sandbox.admin_token}"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    script = resp.read().decode()
    r.check("Setup-script served (200)", resp.status == 200)

    # --- Content assertions ---
    r.check("Uses anchored grep (^Host ... samcloud)",
            "^Host[[:space:]]+samcloud" in script)
    r.check("No bare unanchored 'Host samcloud' grep",
            'grep -q "Host samcloud"' not in script
            and "grep -q 'Host samcloud'" not in script)
    r.check("RequestTTY auto (not force)",
            "RequestTTY auto" in script and "RequestTTY force" not in script)
    r.check("Surfaces the non-interactive form",
            "-- <cmd>" in script)

    # --- Behavioral test: run the script against a HOME that already has
    #     `Host samcloud-pi-1`; the real `Host samcloud` block must still be written. ---
    tmp_home = tempfile.mkdtemp(prefix="samcloud-setupscript-")
    try:
        ssh_dir = os.path.join(tmp_home, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        config_path = os.path.join(ssh_dir, "config")
        with open(config_path, "w") as f:
            f.write("Host samcloud-pi-1\n    HostName 192.0.2.50\n    User pi\n")
        os.chmod(config_path, 0o600)

        script_path = os.path.join(tmp_home, "setup.sh")
        with open(script_path, "w") as f:
            f.write(script)

        env = dict(os.environ, HOME=tmp_home)
        proc = subprocess.run(["bash", script_path], env=env,
                              capture_output=True, text=True, timeout=30)
        r.check("Setup-script runs cleanly", proc.returncode == 0,
                proc.stderr[-200:] if proc.returncode != 0 else "")

        with open(config_path) as f:
            lines = [ln.rstrip() for ln in f]
        r.check("Pre-existing 'Host samcloud-pi-1' preserved",
                any(ln.strip() == "Host samcloud-pi-1" for ln in lines))
        r.check("Real 'Host samcloud' block WAS written (no false-match skip)",
                any(ln.strip() == "Host samcloud" for ln in lines))
        r.check("Written block uses RequestTTY auto",
                any("RequestTTY auto" in ln for ln in lines))
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)

    return r


# =====================================================================
# Report
# =====================================================================

def print_report(results: list, sandbox) -> bool:
    total_pass = sum(r.passed for r in results)
    total_fail = sum(r.failed for r in results)
    total = total_pass + total_fail

    print()
    print("=" * 64)
    print("  samcloud Enrollment E2E Simulation Report")
    print("=" * 64)
    print(f"  Registry:  {sandbox.base_url}")
    print(f"  Database:  {sandbox.db_path} (isolated)")
    print()

    for r in results:
        print(r.summary())
        for step in r.steps:
            icon = "  +" if step["status"] == "PASS" else "  X"
            detail = f" -- {step['detail']}" if step["detail"] else ""
            print(f"    {icon} {step['description']}{detail}")
        print()

    print("-" * 64)
    all_pass = total_fail == 0
    print(f"  {'ALL PASSED' if all_pass else 'FAILURES DETECTED'}: {total_pass}/{total} checks passed")
    print("=" * 64)
    print()

    return all_pass


# =====================================================================
# Main
# =====================================================================

def main():
    sandbox = Sandbox()

    print(f"Starting enrollment E2E sandbox on :{sandbox.port}...")
    try:
        sandbox.start()
        print(f"Sandbox running at {sandbox.base_url}")
        print(f"Temp DB at {sandbox.db_path}")
        print()

        results = [
            scenario_1_phone_enrollment(sandbox),
            scenario_2_gpu_server_enrollment(sandbox),
            scenario_3_dual_mode_enrollment(sandbox),
            scenario_4_lan_server_enrollment(sandbox),
            scenario_5_error_cases(sandbox),
            scenario_6_access_key_lifecycle(sandbox),
            scenario_7_audit_trail(sandbox),
            scenario_8_gateway_key_management(sandbox),
            scenario_9_bootstrap_script_serving(sandbox),
            scenario_10_retoken(sandbox),
            scenario_11_setup_script_gateway_fixes(sandbox),
        ]

        all_pass = print_report(results, sandbox)
        return 0 if all_pass else 1

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        sandbox.stop()
        print("Sandbox cleaned up.")


if __name__ == "__main__":
    sys.exit(main())
