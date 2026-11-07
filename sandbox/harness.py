"""
samcloud Sandbox Harness

Spins up an isolated registry instance with its own temp DB,
runs simulated device/service/user enrollment cycles against it,
validates the full loop, tears down cleanly.

CRITICAL: Never touches production DB at /var/lib/samcloud/registry.db.
All state lives in a temp directory that's deleted on exit.

Usage:
    python sandbox/harness.py                  # Run all cycles
    python sandbox/harness.py --keep           # Keep sandbox running for manual inspection
    python sandbox/harness.py --port 7778      # Custom port
"""

import argparse
import asyncio
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
SANDBOX_PORT = 7780  # a base for scripts that offset from it; Sandbox() itself takes a free port
BASE_URL = None  # set after port is known
STARTUP_TIMEOUT_S = float(os.environ.get("SANDBOX_STARTUP_TIMEOUT_S", "30"))


def free_port() -> int:
    """A port the kernel says is free right now.

    The sandbox used to bind a fixed port. Seats run the gates as different
    accounts on one host, and two concurrent runs collided: one failed in a way
    that looked like a regression, and on 2026-09-25 that cost two release
    attempts. The bind-then-close window is a small race; a fixed port is a
    certain one.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ═══════════════════════════════════════════════════════════════════════════
# Sandbox Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class Sandbox:
    """Isolated registry instance with temp DB."""

    def __init__(self, port: int = None):
        self.port = port or free_port()
        self.base_url = f"http://localhost:{self.port}"
        self.tmp_dir = None
        self.process = None
        self.db_path = None

    def _start_server(self):
        """Start sandbox registry on temp DB."""
        self.tmp_dir = tempfile.mkdtemp(prefix="samcloud-sandbox-")
        self.db_path = os.path.join(self.tmp_dir, "sandbox.db")

        env = os.environ.copy()
        env["SANDBOX_DB_PATH"] = self.db_path

        # Create a wrapper that patches DB_PATH before importing main
        wrapper = os.path.join(self.tmp_dir, "sandbox_main.py")
        with open(wrapper, "w") as f:
            f.write(f"""
import sys, os
sys.path.insert(0, "{REGISTRY_DIR}")
os.environ["SANDBOX_DB_PATH"] = "{self.db_path}"
os.environ["AUTHORIZED_KEYS_PATH"] = "{self.tmp_dir}/authorized_keys"
os.environ["GATEWAY_KEY"] = "{self.tmp_dir}/gateway"
os.environ["GATEWAY_TOKEN_PATH"] = "{self.tmp_dir}/gateway_token"
os.environ["REGISTRY_URL"] = "http://localhost:{self.port}"
os.environ["SAMCLOUD_DOMAIN"] = "example.org"
os.environ["PORT_RANGE_TUNNEL_START"] = "10001"
os.environ["PORT_RANGE_TUNNEL_END"] = "10012"

# Patch db module before anything imports it
import db
from pathlib import Path
db.DB_PATH = Path("{self.db_path}")
db.init_db()

# Now import and run the app
from main import app
import uvicorn
uvicorn.run(app, host="127.0.0.1", port={self.port}, log_level="warning")
""")

        venv_python = REGISTRY_DIR / ".venv" / "bin" / "python3"
        if not venv_python.exists():
            venv_python = "python3"

        self.process = subprocess.Popen(
            [str(venv_python), wrapper],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Wait for startup
        deadline = time.time() + STARTUP_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(0.5)
            if self.process.poll() is not None:
                break
            try:
                import urllib.request
                resp = urllib.request.urlopen(f"{self.base_url}/health", timeout=2)
                if resp.status == 200:
                    return True
            except Exception:
                continue

        # Say why. stderr was piped and never read, so a sandbox that died on
        # import and one that was merely slow looked identical.
        detail = ""
        if self.process.poll() is not None:
            detail = self.process.stderr.read().decode(errors="replace")[-2000:]
            raise RuntimeError(f"Sandbox exited {self.process.returncode} before answering:\n{detail}")
        raise RuntimeError(f"Sandbox failed to start within {STARTUP_TIMEOUT_S}s (still running, not answering /health)")

    def start(self, auto_auth=True):
        """Start sandbox. If auto_auth, creates admin and stores token."""
        self._start_server()
        if auto_auth:
            self.init_auth()

    def stop(self):
        """Kill sandbox and clean up temp files."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

        if self.tmp_dir and os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir)
            self.tmp_dir = None

    def init_auth(self, username="admin", password="admin"):
        """Create admin user (or login) and store token for subsequent API calls."""
        resp = self.api("POST", "/api/v1/init",
                        {"username": username, "password": password}, _skip_auth=True)
        if resp["status"] == 409:
            resp = self.api("POST", "/api/v1/auth/login",
                            {"username": username, "password": password}, _skip_auth=True)
        self.admin_token = resp["data"].get("token", "")

    def api(self, method: str, path: str, data: dict = None, headers: dict = None, _skip_auth: bool = False) -> dict:
        """Make API call to sandbox registry."""
        import urllib.request
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode() if data else None
        req_headers = {"Content-Type": "application/json"} if data else {}
        # Auto-inject auth unless skipped
        if not _skip_auth and hasattr(self, "admin_token") and self.admin_token:
            req_headers["Authorization"] = f"Bearer {self.admin_token}"
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return {"status": resp.status, "data": json.loads(resp.read()),
                    "headers": {k.lower(): v for k, v in resp.headers.items()}}
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                data = {"detail": body.decode("utf-8", errors="replace")}
            return {"status": e.code, "data": data}
        except Exception as e:
            return {"status": 0, "data": {}, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# Test Cycles
# ═══════════════════════════════════════════════════════════════════════════

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
        icon = "✓" if self.failed == 0 else "✗"
        return f"  {icon} {self.name}: {self.passed} passed, {self.failed} failed"


def cycle_1_device_enrollment(sandbox: Sandbox) -> TestResult:
    """Simulate enrolling a new tunnel device.

    A new GPU workstation appears on the network. It needs:
    1. Device registration with auto tunnel port
    2. Service registration with auto forward port
    3. Route creation for external access
    4. Health probe
    5. Verify collision detection
    """
    r = TestResult("Cycle 1: Device Enrollment")

    # Step 1: Register device
    resp = sandbox.api("POST", "/api/v1/devices", {
        "id": "sim-gpu-1",
        "hostname": "SIM-GPU-1",
        "context": "tunnel",
        "outbound_method": "reverse-tunnel",
        "outbound_ssh_user": "simuser",
        "outbound_ssh_port": 2222,
        "startup_method": "systemd",
        "recovery_method": "event-driven",
    })
    r.check("Register device", resp["status"] == 200, f"Got tunnel_port={resp['data'].get('tunnel_port')}")
    tunnel_port = resp["data"].get("tunnel_port")
    r.check("Auto-allocated tunnel port", tunnel_port is not None and 10001 <= tunnel_port <= 10012)

    # Step 2: Duplicate rejection
    resp = sandbox.api("POST", "/api/v1/devices", {
        "id": "sim-gpu-1", "hostname": "DUPE", "context": "tunnel",
    })
    r.check("Duplicate device rejected", resp["status"] == 409)

    # Step 3: Register service on device
    resp = sandbox.api("POST", "/api/v1/services", {
        "name": "inference-api",
        "device_id": "sim-gpu-1",
        "port": 8500,
        "description": "GPU inference API",
        "health_endpoint": "/health",
        "subdomain": "inf",
        "version": "1.0.0",
        "capabilities": ["inference", "embedding"],
    })
    r.check("Register service", resp["status"] == 200, f"slice_port={resp['data'].get('slice_port')}")
    slice_port = resp["data"].get("slice_port")
    r.check("Auto-allocated forward port", slice_port is not None and 18001 <= slice_port <= 18099)
    r.check("External URL generated", resp["data"].get("external_url") == "https://inf.example.org")

    # Step 4: Service created route
    resp = sandbox.api("GET", "/api/v1/routes")
    routes = resp["data"]
    inf_route = [rt for rt in routes if rt["subdomain"] == "inf"]
    r.check("Route auto-created", len(inf_route) == 1)
    r.check("Route target correct", inf_route[0]["target"] == f"localhost:{slice_port}" if inf_route else False)

    # Step 5: Port collision
    resp = sandbox.api("POST", "/api/v1/ports/allocate", {
        "port_type": "forward", "owner_id": "attacker", "preferred": slice_port,
    })
    r.check("Port collision detected", resp["status"] == 409)

    # Step 6: Get device with services
    resp = sandbox.api("GET", "/api/v1/devices/sim-gpu-1")
    r.check("Device has services", len(resp["data"].get("services", [])) == 1)
    r.check("Service name correct", resp["data"]["services"][0]["name"] == "inference-api")

    # Step 7: Audit trail
    resp = sandbox.api("GET", "/api/v1/audit?limit=10")
    actions = [a["action"] for a in resp["data"]]
    r.check("Audit has device.register", "device.register" in actions)
    r.check("Audit has service.register", "service.register" in actions)
    r.check("Audit has port.allocate", "port.allocate" in actions)

    return r


def cycle_2_user_and_tickets(sandbox: Sandbox) -> TestResult:
    """Simulate a user submitting feedback and tickets.

    A user discovers an issue, creates a ticket, another agent
    picks it up, comments, and resolves it.
    """
    r = TestResult("Cycle 2: User & Ticket Workflow")

    # Step 1: Check system status (what a user would do first)
    resp = sandbox.api("GET", "/api/v1/status")
    r.check("Status endpoint responds", resp["status"] == 200)
    r.check("Status has counts", "counts" in resp["data"])

    # Step 2: User creates a bug ticket
    resp = sandbox.api("POST", "/api/v1/tickets", {
        "type": "bug",
        "priority": "high",
        "summary": "Model gateway returns 502 intermittently",
        "body": "When hitting /v1/chat/completions with long prompts, sometimes get 502. Happens ~20% of requests.",
        "service_id": "sim-gpu-1/inference-api",
    })
    r.check("Ticket created", resp["status"] == 200)
    ticket_id = resp["data"]["ticket_id"]

    # Step 3: Agent triages the ticket — assignee must be a real username
    # (validated against the users table). Create the watchdog agent first.
    # Scope v1 (doc 261): an assignee must be able to read the ticket's group —
    # the admin's ticket is in `samcloud-contributors`, so the watchdog is made a member of it.
    made = sandbox.api("POST", "/api/v1/users", {
        "username": "agent-watchdog", "password": "x", "role": "agent", "scopes": ["group:samcloud-contributors"],
    })
    if made.get("data", {}).get("id") or made.get("data", {}).get("user_id"):
        uid = made["data"].get("id") or made["data"].get("user_id")
        sandbox.api("POST", "/api/v1/groups/samcloud-contributors/members", {"user_id": uid, "role": "editor"})
    resp = sandbox.api("PATCH", f"/api/v1/tickets/{ticket_id}", {
        "status": "triaged",
        "assignee": "agent-watchdog",
    })
    r.check("Ticket triaged", resp["status"] == 200)

    # Step 4: Agent adds diagnostic comment
    resp = sandbox.api("POST", f"/api/v1/tickets/{ticket_id}/comments", {
        "body": "Investigated: GPU OOM on long prompts. Model needs max_tokens capped at 2048 for 32B model on 32GB VRAM.",
    })
    r.check("Comment added", resp["status"] == 200)

    # Step 5: Agent resolves
    resp = sandbox.api("PATCH", f"/api/v1/tickets/{ticket_id}", {
        "status": "resolved",
    })
    r.check("Ticket resolved", resp["status"] == 200)

    # Step 6: Verify resolved state
    resp = sandbox.api("GET", f"/api/v1/tickets/{ticket_id}")
    r.check("Status is resolved", resp["data"]["status"] == "resolved")
    r.check("Has resolved_at", resp["data"].get("resolved_at") is not None)
    r.check("Has comment", len(resp["data"].get("comments", [])) == 1)
    r.check("Assignee set", resp["data"]["assignee"] == "agent-watchdog")

    # Step 7: Resolved ticket excluded from open list
    resp = sandbox.api("GET", "/api/v1/tickets?status=open")
    open_ids = [t["id"] for t in resp["data"]]
    r.check("Resolved not in open list", ticket_id not in open_ids)

    # Step 8: User creates feature request
    resp = sandbox.api("POST", "/api/v1/tickets", {
        "type": "feature",
        "priority": "low",
        "summary": "Add movie recommendations to chat concierge",
    })
    r.check("Feature ticket created", resp["status"] == 200)

    # Step 9: Pagination (#190) — limit/offset slice, X-Total-Count sees past it
    resp = sandbox.api("GET", "/api/v1/tickets?limit=1")
    total = int(resp.get("headers", {}).get("x-total-count", -1))
    r.check("Paged list respects limit", resp["status"] == 200 and len(resp["data"]) == 1)
    r.check("X-Total-Count exceeds page", total >= 2, f"total={total}")
    first_id = resp["data"][0]["id"] if resp["data"] else None
    resp = sandbox.api("GET", "/api/v1/tickets?limit=1&offset=1")
    r.check("Offset advances the page",
            len(resp["data"]) == 1 and resp["data"][0]["id"] != first_id)
    resp = sandbox.api("GET", "/api/v1/tickets?limit=501")
    r.check("Limit over max rejected", resp["status"] == 400)
    resp = sandbox.api("GET", "/api/v1/tickets?offset=-1")
    r.check("Negative offset rejected", resp["status"] == 400)

    # Step 10: watch state is readable (#414) and unsubscribe mutes — your own
    # later comment must NOT silently re-enroll you (#83).
    resp = sandbox.api("GET", "/api/v1/subscriptions")
    r.check("Subscriptions readable", resp["status"] == 200 and "tickets" in resp["data"])
    watched = [t["id"] for t in resp["data"].get("tickets", [])]
    r.check("Filer is auto-subscribed", ticket_id in watched)
    sandbox.api("DELETE", f"/api/v1/tickets/{ticket_id}/subscribe")
    sandbox.api("POST", f"/api/v1/tickets/{ticket_id}/comments", {"body": "parting note after opting out"})
    resp = sandbox.api("GET", "/api/v1/subscriptions")
    r.check("Mute survives own comment",
            ticket_id not in [t["id"] for t in resp["data"].get("tickets", [])])
    r.check("Mute is visible in watch state",
            any(m.get("ticket_id") == ticket_id for m in resp["data"].get("mutes", [])))
    sandbox.api("POST", f"/api/v1/tickets/{ticket_id}/subscribe")
    resp = sandbox.api("GET", "/api/v1/subscriptions")
    r.check("Explicit re-subscribe clears the mute",
            ticket_id in [t["id"] for t in resp["data"].get("tickets", [])]
            and not any(m.get("ticket_id") == ticket_id for m in resp["data"].get("mutes", [])))

    # Step 11: scope edits must not silently kill credentials (#472). A PATCH
    # that adds a scope keeps the existing bearer alive and carrying the new
    # scope; invalidation only happens when rotation is asked for by name.
    resp = sandbox.api("POST", "/api/v1/users", {
        "username": "agent-tokensem", "password": "x", "role": "agent", "scopes": ["group:home"],
    })
    tokensem_id = resp["data"]["user_id"]
    resp = sandbox.api("POST", f"/api/v1/users/{tokensem_id}/regenerate-token")
    t0 = resp["data"]["token"]
    t0_auth = {"Authorization": f"Bearer {t0}"}
    resp = sandbox.api("PATCH", f"/api/v1/users/{tokensem_id}",
                       {"scopes": ["group:home", "device:sim-nas-1"]})
    r.check("Scope PATCH does not rotate by default",
            resp["status"] == 200 and "token" not in resp["data"]
            and resp["data"].get("token_rotated") is False)
    resp = sandbox.api("GET", "/api/v1/auth/verify", headers=t0_auth)
    r.check("Existing token survives scope PATCH and sees the new scope",
            resp["status"] == 200 and "device:sim-nas-1" in resp["data"].get("scopes", []),
            f"scopes={resp['data'].get('scopes')}")
    resp = sandbox.api("PATCH", f"/api/v1/users/{tokensem_id}",
                       {"scopes": ["group:home"], "rotate_token": True})
    r.check("Explicit rotate_token mints and returns a new token",
            resp["status"] == 200 and resp["data"].get("token", "").startswith("sc_agent_"))
    resp = sandbox.api("GET", "/api/v1/auth/verify", headers=t0_auth)
    r.check("Old token dies only on explicit rotation", resp["status"] == 401)

    return r


def cycle_3_service_lifecycle(sandbox: Sandbox) -> TestResult:
    """Simulate a service going through its full lifecycle.

    Register → active → health check → degraded → stopped → deregistered.
    Also tests multi-service device and port exhaustion edge cases.
    """
    r = TestResult("Cycle 3: Service Lifecycle")

    # Step 1: Register a home device (no tunnel needed)
    resp = sandbox.api("POST", "/api/v1/devices", {
        "id": "sim-nas",
        "hostname": "SIM-NAS",
        "context": "samcloud",
        "type": "computer",
        "ip": "192.0.2.100",
        "startup_method": "docker",
        "recovery_method": "restart-policy",
    })
    r.check("Home device registered", resp["status"] == 200)
    r.check("No tunnel allocated for samcloud", resp["data"].get("tunnel_port") is None)

    # Step 2: Register multiple services on same device
    services = [
        {"name": "photoprism", "port": 2342, "subdomain": "photos", "health_endpoint": "/api/v1/status", "description": "Photo management"},
        {"name": "syncthing", "port": 8384, "subdomain": None, "health_endpoint": "/rest/system/status", "description": "File sync"},
        {"name": "nextcloud", "port": 8080, "subdomain": "cloud", "health_endpoint": "/status.php", "description": "Cloud storage"},
    ]
    for svc in services:
        resp = sandbox.api("POST", "/api/v1/services", {
            "name": svc["name"],
            "device_id": "sim-nas",
            "port": svc["port"],
            "description": svc["description"],
            "health_endpoint": svc["health_endpoint"],
            "subdomain": svc["subdomain"],
        })
        r.check(f"Register {svc['name']}", resp["status"] == 200)

    # Step 3: Verify device has all services
    resp = sandbox.api("GET", "/api/v1/devices/sim-nas")
    r.check("Device has 3 services", len(resp["data"].get("services", [])) == 3)

    # Step 4: List services filtered by device
    resp = sandbox.api("GET", "/api/v1/services?device_id=sim-nas")
    r.check("Filter by device works", len(resp["data"]) == 3)

    # Step 5: Verify routes created only for services with subdomains
    resp = sandbox.api("GET", "/api/v1/routes")
    route_subs = [rt["subdomain"] for rt in resp["data"]]
    r.check("photos route exists", "photos" in route_subs)
    r.check("cloud route exists", "cloud" in route_subs)
    r.check("syncthing has no route (no subdomain)", "syncthing" not in route_subs)

    # Step 6: Port uniqueness across device
    resp = sandbox.api("POST", "/api/v1/services", {
        "name": "duplicate-port",
        "device_id": "sim-nas",
        "port": 2342,  # same as photoprism
        "description": "Duplicate port test",
    })
    r.check("Duplicate port on device rejected", resp["status"] in (409, 500), f"status={resp['status']}")

    # Step 7: System status shows everything
    resp = sandbox.api("GET", "/api/v1/status")
    counts = resp["data"]["counts"]
    r.check("Status reflects new device", counts["devices"] >= 2)
    r.check("Status reflects new services", counts["services"] >= 4)

    # Step 8: Full audit trail integrity
    resp = sandbox.api("GET", "/api/v1/audit?limit=50")
    all_actions = [a["action"] for a in resp["data"]]
    r.check("Audit complete", all_actions.count("device.register") >= 2)
    r.check("Audit has service registrations", all_actions.count("service.register") >= 4)

    return r


# ═══════════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════════

def print_report(results: list[TestResult], sandbox: Sandbox):
    """Print human-readable test report."""
    total_pass = sum(r.passed for r in results)
    total_fail = sum(r.failed for r in results)
    total = total_pass + total_fail

    print()
    print("=" * 60)
    print("  samcloud Sandbox Test Report")
    print("=" * 60)
    print(f"  Registry:  {sandbox.base_url}")
    print(f"  Database:  {sandbox.db_path} (isolated)")
    print(f"  Production DB untouched: /var/lib/samcloud/registry.db")
    print()

    for r in results:
        print(r.summary())
        for step in r.steps:
            icon = "  ✓" if step["status"] == "PASS" else "  ✗"
            detail = f" — {step['detail']}" if step["detail"] else ""
            print(f"    {icon} {step['description']}{detail}")
        print()

    print("-" * 60)
    all_pass = total_fail == 0
    print(f"  {'ALL PASSED' if all_pass else 'FAILURES DETECTED'}: {total_pass}/{total} checks passed")
    print("=" * 60)
    print()

    return all_pass


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="samcloud Sandbox Test Harness")
    parser.add_argument("--port", type=int, default=None, help="Sandbox registry port (default: a free one)")
    parser.add_argument("--keep", action="store_true", help="Keep sandbox running after tests")
    args = parser.parse_args()

    sandbox = Sandbox(port=args.port)

    print(f"Starting sandbox registry on :{sandbox.port}...")
    try:
        sandbox.start()
        print(f"Sandbox running at {sandbox.base_url}")
        print(f"Temp DB at {sandbox.db_path}")
        print()

        # Run test cycles
        results = [
            cycle_1_device_enrollment(sandbox),
            cycle_2_user_and_tickets(sandbox),
            cycle_3_service_lifecycle(sandbox),
        ]

        all_pass = print_report(results, sandbox)

        if args.keep:
            print(f"Sandbox still running at {sandbox.base_url}")
            print(f"Press Ctrl+C to stop...")
            try:
                signal.pause()
            except KeyboardInterrupt:
                pass

        return 0 if all_pass else 1

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if not args.keep:
            sandbox.stop()
            print("Sandbox cleaned up.")


if __name__ == "__main__":
    sys.exit(main())
