"""
samcloud Enrollment System Tests

Tests the Phase 2 enrollment flow:
- Token generation and validation
- Device enrollment invitation → bootstrap activation
- Service registration with device tokens
- Pending enrollment listing

Uses the sandbox harness pattern (isolated temp DB, full API server).

Usage:
    python sandbox/test_enrollment.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REGISTRY_DIR = Path(__file__).parent.parent / "registry"
SANDBOX_PORT = 7779  # Different from harness.py to avoid conflicts

# Reuse the harness Sandbox and TestResult classes

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.steps = []
        self.passed = 0
        self.failed = 0

    def check(self, description: str, condition: bool, detail: str = ""):
        status = "PASS" if condition else "FAIL"
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        self.steps.append({"description": description, "status": status, "detail": detail})

    def summary(self) -> str:
        icon = "+" if self.failed == 0 else "X"
        return f"  {icon} {self.name}: {self.passed} passed, {self.failed} failed"


# Import Sandbox from harness (shared, with auth support)
sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox

# Legacy compat: the old local Sandbox had a slightly different api signature
# The harness Sandbox now handles auth automatically via init_auth()



# =====================================================================
# Test Cycles
# =====================================================================

def cycle_1_full_enrollment_flow(sandbox: Sandbox) -> TestResult:
    """Full enrollment flow: create invitation -> bootstrap activate -> register service."""
    r = TestResult("Cycle 1: Full Enrollment Flow")

    # Step 1: Create enrollment invitation (access target — needs bootstrap)
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "new-gpu",
        "name": "NEW-GPU-HOST",
        "type": "computer",
        "access_target": "reverse-tunnel",
        "target_ssh_user": "you",
    })
    r.check("Enrollment invitation created", resp["status"] == 200,
            f"status={resp['status']}")
    enroll_data = resp["data"]
    r.check("Token starts with sc_enroll_",
            enroll_data.get("token", "").startswith("sc_enroll_"),
            f"token={enroll_data.get('token', '')[:30]}...")
    r.check("Bootstrap URL contains token",
            enroll_data.get("token", "") in enroll_data.get("bootstrap_url", ""),
            f"url={enroll_data.get('bootstrap_url', '')[:60]}...")
    r.check("Expires in 3600 seconds",
            enroll_data.get("expires_in") == 3600)
    r.check("Tunnel port allocated",
            enroll_data.get("tunnel_port") is not None,
            f"tunnel_port={enroll_data.get('tunnel_port')}")

    token = enroll_data.get("token", "")
    tunnel_port = enroll_data.get("tunnel_port")

    # Step 2: Verify device is in pending state
    resp = sandbox.api("GET", f"/api/v1/devices/new-gpu")
    r.check("Device created with pending status",
            resp["status"] == 200 and resp["data"].get("status") == "pending",
            f"status={resp['data'].get('status')}")

    # Step 2b: Fetch bootstrap script
    import urllib.request
    bootstrap_resp = urllib.request.urlopen(f"{sandbox.base_url}/api/v1/bootstrap/{token}")
    bootstrap_script = bootstrap_resp.read().decode()
    r.check("Bootstrap script served",
            bootstrap_resp.status == 200 and len(bootstrap_script) > 100)
    r.check("Bootstrap has device ID",
            "new-gpu" in bootstrap_script)
    r.check("Bootstrap has enrollment token",
            token in bootstrap_script)
    r.check("Bootstrap has tunnel port",
            str(tunnel_port) in bootstrap_script)
    r.check("Bootstrap template vars filled",
            "__ENROLLMENT_TOKEN__" not in bootstrap_script and "__DEVICE_ID__" not in bootstrap_script)
    # Ticket #14: bootstrap must fail loud on sudo-no-tty rather than silently
    # no-op every sudo step. Sentinel is the helper added in registry/bootstrap.sh.
    r.check("Bootstrap has sudo-no-tty fail-fast",
            "ensure_sudo_or_fail" in bootstrap_script)

    # Ticket #15: PowerShell bootstrap must clean stale Host block + Scheduled
    # Task on re-run instead of skipping when artifacts exist. Sentinels are
    # the new comment markers from the rewrite.
    ps_resp = urllib.request.urlopen(f"{sandbox.base_url}/api/v1/bootstrap/{token}/ps1")
    ps_script = ps_resp.read().decode()
    r.check("PowerShell bootstrap served",
            ps_resp.status == 200 and len(ps_script) > 100)
    r.check("PS bootstrap cleans stale ssh-config block",
            "Removed prior samcloud bootstrap" in ps_script)
    r.check("PS bootstrap cleans stale scheduled task",
            "Unregister-ScheduledTask" in ps_script and "Removed prior scheduled task" in ps_script)
    r.check("PS bootstrap template vars filled",
            "__ENROLLMENT_TOKEN__" not in ps_script and "__DEVICE_ID__" not in ps_script)

    # Step 3: Activate device (simulating bootstrap script)
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest123 samcloud:new-gpu",
        "startup_method": "systemd",
    })
    r.check("Device activation succeeded", resp["status"] == 200,
            f"status={resp['status']}, data={resp['data']}")
    activate_data = resp["data"]
    r.check("Status is enrolled",
            activate_data.get("status") == "enrolled")
    r.check("Device ID matches",
            activate_data.get("device_id") == "new-gpu")
    r.check("Tunnel port matches",
            activate_data.get("tunnel_port") == tunnel_port)
    r.check("Device token returned",
            activate_data.get("device_token", "").startswith("sc_device_"),
            f"token={activate_data.get('device_token', '')[:30]}...")

    device_token = activate_data.get("device_token", "")

    # Step 4: Verify device is now online
    resp = sandbox.api("GET", f"/api/v1/devices/new-gpu")
    r.check("Device status is online",
            resp["data"].get("status") == "online")
    r.check("Device enrolled flag set",
            resp["data"].get("enrolled") == 1)
    r.check("SSH pubkey stored",
            resp["data"].get("ssh_pubkey", "").startswith("ssh-ed25519"),
            f"pubkey={resp['data'].get('ssh_pubkey', '')[:30]}...")

    # Step 5: Register a service using the device token
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "inference-api",
        "port": 8500,
        "health_endpoint": "/health",
        "subdomain": "inf",
    }, headers={"Authorization": f"Bearer {device_token}"})
    r.check("Service registration succeeded", resp["status"] == 200,
            f"status={resp['status']}, data={resp['data']}")
    svc_data = resp["data"]
    r.check("Service ID correct",
            svc_data.get("service_id") == "new-gpu/inference-api")
    r.check("Slice port allocated",
            svc_data.get("slice_port") is not None,
            f"slice_port={svc_data.get('slice_port')}")
    r.check("External URL correct",
            svc_data.get("external_url") == "https://inf.example.org")
    r.check("Service token returned",
            svc_data.get("service_token", "").startswith("sc_service_"),
            f"token={svc_data.get('service_token', '')[:30]}...")

    # Step 6: Verify service exists via regular API
    resp = sandbox.api("GET", "/api/v1/services/new-gpu/inference-api")
    r.check("Service visible in registry",
            resp["status"] == 200 and resp["data"].get("name") == "inference-api")

    # Step 7: Verify route was created
    resp = sandbox.api("GET", "/api/v1/routes")
    routes = resp["data"]
    inf_route = [rt for rt in routes if rt.get("subdomain") == "inf"]
    r.check("Route created for service",
            len(inf_route) == 1,
            f"found {len(inf_route)} inf routes")

    # Step 8: Audit trail
    resp = sandbox.api("GET", "/api/v1/audit?limit=20")
    actions = [a["action"] for a in resp["data"]]
    r.check("Audit has enrollment invitation",
            "device.enroll.created" in actions)
    r.check("Audit has device activation",
            "device.activated" in actions)
    r.check("Audit has authenticated service registration",
            "service.register.authenticated" in actions)

    return r


def cycle_2_token_validation(sandbox: Sandbox) -> TestResult:
    """Token validation: expired, used, invalid tokens all rejected."""
    r = TestResult("Cycle 2: Token Validation")

    # Step 1: Invalid token rejected
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": "sc_enroll_invalid_garbage_token_1234",
        "ssh_pubkey": "ssh-ed25519 AAAA...",
    })
    r.check("Invalid token rejected", resp["status"] == 401,
            f"status={resp['status']}")

    # Step 2: Create a valid token and use it (access target to get a token)
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "token-test-device",
        "name": "TOKEN-TEST",
        "access_target": "direct-ssh",
        "target_ip": "192.0.2.100",
    })
    token = resp["data"]["token"]

    # Use the token
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReuse samcloud:test",
    })
    r.check("First use succeeds", resp["status"] == 200)

    # Step 3: Try to reuse the same token (single-use enforcement)
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIReuse samcloud:test",
    })
    r.check("Reused token rejected", resp["status"] == 401,
            f"status={resp['status']}")

    # Step 4: Completely malformed token
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": "not_even_close",
        "ssh_pubkey": "ssh-ed25519 AAAA...",
    })
    r.check("Malformed token rejected", resp["status"] == 401)

    # Step 5: Empty token
    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": "",
        "ssh_pubkey": "ssh-ed25519 AAAA...",
    })
    r.check("Empty token rejected", resp["status"] == 401)

    # Step 6: Duplicate enrollment of same device-id should be rejected
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "token-test-device",
        "name": "TOKEN-TEST-UPDATED",
        "access_target": "direct-ssh",
    })
    r.check("Re-enrollment of existing device rejected (already active)",
            resp["status"] == 409,
            f"status={resp['status']}")

    # Step 7: Service registration without auth token rejected
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "test-svc",
        "port": 8080,
    }, headers={"Authorization": ""}, _skip_auth=True)
    r.check("Service register without auth rejected", resp["status"] == 401,
            f"status={resp['status']}")

    # Step 8: Service registration with invalid token rejected
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "test-svc",
        "port": 8080,
    }, headers={"Authorization": "Bearer sc_device_fakefakefake"})
    r.check("Service register with bad token rejected", resp["status"] == 401,
            f"status={resp['status']}")

    return r


def cycle_3_service_registration_with_device_token(sandbox: Sandbox) -> TestResult:
    """Service registration with device token: valid token works, wrong device rejected."""
    r = TestResult("Cycle 3: Service Registration Auth")

    # Set up: enroll a device (access target) and get its token
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "svc-test-dev",
        "name": "SVC-TEST",
        "access_target": "reverse-tunnel",
        "target_ssh_user": "you",
    })
    token = resp["data"]["token"]

    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISvcTest samcloud:svc-test",
        "startup_method": "systemd",
    })
    device_token = resp["data"]["device_token"]

    # Step 1: Register a service with valid device token
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "api-one",
        "port": 8500,
        "health_endpoint": "/health",
        "subdomain": "api1",
    }, headers={"Authorization": f"Bearer {device_token}"})
    r.check("Service registered with valid token", resp["status"] == 200,
            f"service_id={resp['data'].get('service_id')}")
    r.check("Service ID has correct device prefix",
            resp["data"].get("service_id") == "svc-test-dev/api-one")

    # Step 2: Register a second service (different name) — should also work
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "api-two",
        "port": 9000,
        "health_endpoint": "/status",
    }, headers={"Authorization": f"Bearer {device_token}"})
    r.check("Second service registered", resp["status"] == 200)
    r.check("No external URL when no subdomain",
            resp["data"].get("external_url") is None)

    # Step 3: Duplicate service name rejected
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "api-one",
        "port": 8501,
    }, headers={"Authorization": f"Bearer {device_token}"})
    r.check("Duplicate service name rejected", resp["status"] == 409,
            f"status={resp['status']}")

    # Step 4: Using a service token (not device token) for registration should fail
    # (service tokens don't have service:register scope)
    svc_token = resp["data"].get("service_token", "")
    # Get the service token from the first registration
    first_resp = sandbox.api("GET", "/api/v1/services/svc-test-dev/api-one")
    r.check("First service retrievable", first_resp["status"] == 200)

    # Step 5: Verify services show up on device
    resp = sandbox.api("GET", "/api/v1/devices/svc-test-dev")
    services = resp["data"].get("services", [])
    r.check("Device has 2 services", len(services) == 2,
            f"found {len(services)} services")

    # Step 6: Auth header with wrong format rejected
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "bad-auth",
        "port": 7777,
    }, headers={"Authorization": f"Token {device_token}"})
    r.check("Wrong auth format rejected", resp["status"] == 401)

    # Step 7: Auth header with no Bearer prefix rejected
    resp = sandbox.api("POST", "/api/v1/services/register", {
        "name": "bad-auth-2",
        "port": 7778,
    }, headers={"Authorization": device_token})
    r.check("No Bearer prefix rejected", resp["status"] == 401)

    return r


def cycle_4_pending_enrollments(sandbox: Sandbox) -> TestResult:
    """Pending enrollments list."""
    r = TestResult("Cycle 4: Pending Enrollments")

    # Step 1: Create multiple enrollment invitations (all access targets — stay pending)
    for i in range(3):
        resp = sandbox.api("POST", "/api/v1/enroll", {
            "device_id": f"pending-dev-{i}",
            "name": f"PENDING-{i}",
            "access_target": "reverse-tunnel" if i < 2 else "direct-ssh",
            "target_ssh_user": "test",
        })
        r.check(f"Enrollment {i} created", resp["status"] == 200)

    # Step 2: Check pending list
    resp = sandbox.api("GET", "/api/v1/enroll/pending")
    r.check("Pending list returned", resp["status"] == 200)
    pending = resp["data"]
    pending_ids = [d["id"] for d in pending]
    r.check("All 3 pending devices listed",
            all(f"pending-dev-{i}" in pending_ids for i in range(3)),
            f"found: {pending_ids}")

    # Step 3: Activate one device
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "pending-dev-activate",
        "name": "WILL-ACTIVATE",
        "access_target": "direct-ssh",
        "target_ip": "192.0.2.200",
    })
    activate_token = resp["data"]["token"]

    resp = sandbox.api("POST", "/api/v1/devices/activate", {
        "token": activate_token,
        "ssh_pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIActivate samcloud:test",
    })
    r.check("Activation succeeded", resp["status"] == 200)

    # Step 4: Verify activated device no longer in pending list
    resp = sandbox.api("GET", "/api/v1/enroll/pending")
    pending_ids = [d["id"] for d in resp["data"]]
    r.check("Activated device not in pending list",
            "pending-dev-activate" not in pending_ids,
            f"pending: {pending_ids}")

    # Step 5: The 3 original pending devices should still be there
    r.check("Original pending devices still listed",
            all(f"pending-dev-{i}" in pending_ids for i in range(3)),
            f"found: {pending_ids}")

    # Step 6: Existing endpoints still work (don't break existing behavior)
    resp = sandbox.api("POST", "/api/v1/devices", {
        "id": "legacy-device",
        "hostname": "LEGACY",
        "context": "samcloud",
    })
    r.check("Legacy device registration still works", resp["status"] == 200)

    resp = sandbox.api("GET", "/api/v1/devices/legacy-device")
    r.check("Legacy device status is online (not pending)",
            resp["data"].get("status") == "online")

    return r


def cycle_5_access_route_enrollment(sandbox: Sandbox) -> TestResult:
    """Access route enrollment: device activates immediately, no token needed."""
    r = TestResult("Cycle 5: Access Route Enrollment")

    # Step 1: Enroll a route-only device (phone)
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "alices-phone",
        "name": "Alice's phone",
        "type": "phone",
        "access_route": "ssh-tunnel",
    })
    r.check("Route-only enrollment succeeds", resp["status"] == 200)
    data = resp["data"]
    r.check("Status is online (immediate activation)",
            data.get("status") == "online",
            f"status={data.get('status')}")
    r.check("No token returned (no bootstrap needed)",
            "token" not in data,
            f"keys={list(data.keys())}")

    # Step 2: Device should NOT be in pending list
    resp = sandbox.api("GET", "/api/v1/enroll/pending")
    pending_ids = [d["id"] for d in resp["data"]]
    r.check("Route device not in pending list",
            "alices-phone" not in pending_ids,
            f"pending: {pending_ids}")

    # Step 3: Device should be in devices list
    resp = sandbox.api("GET", "/api/v1/devices/alices-phone")
    r.check("Device exists and is online",
            resp["status"] == 200 and resp["data"].get("status") == "online")
    r.check("Inbound method stored",
            resp["data"].get("inbound_method") == "ssh-tunnel",
            f"inbound_method={resp['data'].get('inbound_method')}")

    # Step 4: Enroll a "both" device (Mac — route + target)
    resp = sandbox.api("POST", "/api/v1/enroll", {
        "device_id": "alices-laptop",
        "name": "Alice's laptop",
        "type": "laptop",
        "access_route": "lan",
        "access_target": "direct-ssh",
        "target_ip": "192.0.2.45",
        "target_ssh_user": "you",
    })
    r.check("Both-mode enrollment succeeds", resp["status"] == 200)
    data = resp["data"]
    r.check("Both-mode status is pending (needs bootstrap)",
            data.get("status") == "pending",
            f"status={data.get('status')}")
    r.check("Both-mode returns token",
            "token" in data and data["token"].startswith("sc_enroll_"))

    return r


# =====================================================================
# Report
# =====================================================================

def print_report(results: list, sandbox: Sandbox) -> bool:
    total_pass = sum(r.passed for r in results)
    total_fail = sum(r.failed for r in results)
    total = total_pass + total_fail

    print()
    print("=" * 60)
    print("  samcloud Enrollment System Test Report")
    print("=" * 60)
    print(f"  Registry:  {sandbox.base_url}")
    print(f"  Database:  {sandbox.db_path} (isolated)")
    print(f"  Production DB untouched: /var/lib/samcloud/registry.db")
    print()

    for r in results:
        print(r.summary())
        for step in r.steps:
            icon = "  +" if step["status"] == "PASS" else "  X"
            detail = f" -- {step['detail']}" if step["detail"] else ""
            print(f"    {icon} {step['description']}{detail}")
        print()

    print("-" * 60)
    all_pass = total_fail == 0
    print(f"  {'ALL PASSED' if all_pass else 'FAILURES DETECTED'}: {total_pass}/{total} checks passed")
    print("=" * 60)
    print()

    return all_pass


# =====================================================================
# Main
# =====================================================================

def main():
    sandbox = Sandbox()

    print(f"Starting enrollment test sandbox on :{sandbox.port}...")
    try:
        sandbox.start()
        print(f"Sandbox running at {sandbox.base_url}")
        print(f"Temp DB at {sandbox.db_path}")
        print()

        results = [
            cycle_1_full_enrollment_flow(sandbox),
            cycle_2_token_validation(sandbox),
            cycle_3_service_registration_with_device_token(sandbox),
            cycle_4_pending_enrollments(sandbox),
            cycle_5_access_route_enrollment(sandbox),
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
