"""
samcloud Users & Groups Test Suite

Tests user management, group management, group-scoped status,
and the full admin init flow against a sandboxed registry instance.

Usage:
    python sandbox/test_users_groups.py
    python sandbox/test_users_groups.py --port 7781
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox, TestResult, print_report, SANDBOX_PORT


# ===================================================================
# Helpers
# ===================================================================

def register_device(sandbox: Sandbox, device_id: str, hostname: str,
                    context: str = "samcloud", ip: str = None) -> dict:
    payload = {
        "id": device_id,
        "hostname": hostname,
        "context": context,
    }
    if ip:
        payload["ip"] = ip
    return sandbox.api("POST", "/api/v1/devices", payload)


def register_service(sandbox: Sandbox, device_id: str, name: str,
                     port: int, subdomain: str = None) -> dict:
    payload = {
        "name": name,
        "device_id": device_id,
        "port": port,
        "description": f"Service {name}",
        "health_endpoint": "/health",
    }
    if subdomain:
        payload["subdomain"] = subdomain
    return sandbox.api("POST", "/api/v1/services", payload)


# ===================================================================
# Cycle 1: Admin Init
# ===================================================================

def cycle_1_admin_init(sandbox: Sandbox) -> TestResult:
    """Test first-run admin initialization.

    - Create admin with password
    - Verify token is returned
    - Verify second init is rejected
    - Verify admin appears in user list
    """
    r = TestResult("Cycle 1: Admin Init")

    # Step 1: Users table should be empty
    resp = sandbox.api("GET", "/api/v1/users")
    r.check("Users list empty initially",
            resp["status"] == 200 and len(resp["data"]) == 0,
            f"count={len(resp['data']) if resp['status'] == 200 else 'ERROR'}")

    # Step 2: Init admin
    resp = sandbox.api("POST", "/api/v1/init", {
        "username": "admin",
        "password": "testpass123",
    })
    r.check("Admin init succeeds",
            resp["status"] == 200 and resp["data"].get("status") == "initialized",
            f"resp={resp['data']}")

    user_id = resp["data"].get("user_id")
    r.check("Admin user_id returned", user_id is not None and user_id > 0,
            f"user_id={user_id}")

    token = resp["data"].get("token")
    r.check("Admin token returned", token is not None and token.startswith("sc_device_"),
            f"token prefix OK" if token else "no token")

    # Step 3: Second init should fail
    resp = sandbox.api("POST", "/api/v1/init", {
        "username": "evil",
        "password": "hacker",
    })
    r.check("Second init rejected (409)",
            resp["status"] == 409,
            f"status={resp['status']}")

    # Step 4: Admin appears in user list
    resp = sandbox.api("GET", "/api/v1/users")
    r.check("Admin in user list",
            resp["status"] == 200 and len(resp["data"]) == 1,
            f"count={len(resp['data'])}")
    r.check("Admin username correct",
            resp["data"][0].get("username") == "admin")
    r.check("Admin role is 'admin'",
            resp["data"][0].get("role") == "admin")

    # Step 5: Audit log recorded init
    resp = sandbox.api("GET", "/api/v1/audit?action=admin.init")
    r.check("Audit log has admin.init",
            resp["status"] == 200 and len(resp["data"]) >= 1,
            f"entries={len(resp['data'])}")

    return r


# ===================================================================
# Cycle 2: User Management
# ===================================================================

def cycle_2_user_management(sandbox: Sandbox) -> TestResult:
    """Test user CRUD operations.

    - Create users with different roles
    - List users
    - Get user by ID (with groups)
    - Update user role and scopes
    - Duplicate username rejected
    """
    r = TestResult("Cycle 2: User Management")

    # Step 1: Create user 'robin'
    resp = sandbox.api("POST", "/api/v1/users", {
        "username": "robin",
        "password": "pass456",
        "role": "user",
    })
    r.check("Create user robin",
            resp["status"] == 200 and resp["data"].get("username") == "robin",
            f"user_id={resp['data'].get('user_id')}")
    robin_id = resp["data"].get("user_id")

    # Step 2: Create user 'turbo' (agent)
    resp = sandbox.api("POST", "/api/v1/users", {
        "username": "turbo",
        "password": "agentpass",
        "role": "agent",
        "scopes": ["read", "write"],
    })
    r.check("Create agent user turbo",
            resp["status"] == 200 and resp["data"].get("username") == "turbo",
            f"user_id={resp['data'].get('user_id')}")
    turbo_id = resp["data"].get("user_id")

    # Step 3: Duplicate username rejected
    resp = sandbox.api("POST", "/api/v1/users", {
        "username": "robin",
        "password": "other",
    })
    r.check("Duplicate username rejected",
            resp["status"] == 409,
            f"status={resp['status']}")

    # Step 4: List users (should be 3: admin, robin, turbo)
    resp = sandbox.api("GET", "/api/v1/users")
    r.check("List shows 3 users",
            resp["status"] == 200 and len(resp["data"]) == 3,
            f"count={len(resp['data'])}")
    usernames = [u["username"] for u in resp["data"]]
    r.check("All usernames present",
            "admin" in usernames and "robin" in usernames and "turbo" in usernames)

    # Step 5: Get user by ID
    resp = sandbox.api("GET", f"/api/v1/users/{robin_id}")
    r.check("Get user by ID",
            resp["status"] == 200 and resp["data"].get("username") == "robin",
            f"role={resp['data'].get('role')}")
    r.check("User has groups field",
            "groups" in resp["data"],
            f"groups={resp['data'].get('groups')}")

    # Step 6: Get user not found
    resp = sandbox.api("GET", "/api/v1/users/9999")
    r.check("User not found returns 404",
            resp["status"] == 404)

    # Step 7: Update user role
    resp = sandbox.api("PATCH", f"/api/v1/users/{robin_id}", {
        "role": "admin",
    })
    r.check("Update user role",
            resp["status"] == 200)

    resp = sandbox.api("GET", f"/api/v1/users/{robin_id}")
    r.check("Role updated to admin",
            resp["data"].get("role") == "admin")

    # Step 8: Update user scopes
    resp = sandbox.api("PATCH", f"/api/v1/users/{turbo_id}", {
        "scopes": ["read", "write", "admin"],
    })
    r.check("Update user scopes",
            resp["status"] == 200)

    # Step 9: Update non-existent user
    resp = sandbox.api("PATCH", "/api/v1/users/9999", {
        "role": "admin",
    })
    r.check("Update nonexistent user returns 404",
            resp["status"] == 404)

    return r


# ===================================================================
# Cycle 3: Group Management
# ===================================================================

def cycle_3_group_management(sandbox: Sandbox) -> TestResult:
    """Test group CRUD and membership.

    - Create groups
    - Add services to groups
    - Add members to groups
    - Remove services and members
    - Duplicate detection
    """
    r = TestResult("Cycle 3: Group Management")

    # --- Setup: Register devices and services ---
    register_device(sandbox, "test-nas", "TEST-NAS", "samcloud", "192.0.2.100")
    register_device(sandbox, "test-gpu", "TEST-GPU", "tunnel")
    register_service(sandbox, "test-nas", "mediaserver", 8080, "media-test")
    register_service(sandbox, "test-nas", "indexer", 8081)
    register_service(sandbox, "test-gpu", "inference", 8500, "inf-test")

    # Step 1: Create group 'home'
    resp = sandbox.api("POST", "/api/v1/groups", {
        "id": "home",
        "name": "Home Services",
        "description": "Media and home automation",
        "route_path": "/home",
    })
    r.check("Create home group",
            resp["status"] == 200 and resp["data"].get("group_id") == "home",
            f"resp={resp['data']}")

    # Step 2: Create group 'work'
    resp = sandbox.api("POST", "/api/v1/groups", {
        "id": "work",
        "name": "Work Services",
        "description": "GPU compute and ML",
        "route_path": "/work",
    })
    r.check("Create work group",
            resp["status"] == 200 and resp["data"].get("group_id") == "work")

    # Step 3: Duplicate group rejected
    resp = sandbox.api("POST", "/api/v1/groups", {
        "id": "home",
        "name": "Duplicate",
    })
    r.check("Duplicate group rejected",
            resp["status"] == 409)

    # Step 4: List groups
    resp = sandbox.api("GET", "/api/v1/groups")
    r.check("List shows 2 groups",
            resp["status"] == 200 and len(resp["data"]) == 2,
            f"count={len(resp['data'])}")

    # Step 5: Add services to home group
    resp = sandbox.api("POST", "/api/v1/groups/home/services", {
        "service_id": "test-nas/mediaserver",
    })
    r.check("Add mediaserver to home",
            resp["status"] == 200)

    resp = sandbox.api("POST", "/api/v1/groups/home/services", {
        "service_id": "test-nas/indexer",
    })
    r.check("Add indexer to home",
            resp["status"] == 200)

    # Step 6: Add service to work group
    resp = sandbox.api("POST", "/api/v1/groups/work/services", {
        "service_id": "test-gpu/inference",
    })
    r.check("Add inference to work",
            resp["status"] == 200)

    # Step 7: Duplicate service in group rejected
    resp = sandbox.api("POST", "/api/v1/groups/home/services", {
        "service_id": "test-nas/mediaserver",
    })
    r.check("Duplicate service in group rejected",
            resp["status"] == 409)

    # Step 8: Add nonexistent service rejected
    resp = sandbox.api("POST", "/api/v1/groups/home/services", {
        "service_id": "fake/nonexistent",
    })
    r.check("Nonexistent service rejected",
            resp["status"] == 404)

    # Step 9: Add members to home group
    # Get user IDs first
    users_resp = sandbox.api("GET", "/api/v1/users")
    admin_id = None
    robin_id = None
    for u in users_resp["data"]:
        if u["username"] == "admin":
            admin_id = u["id"]
        elif u["username"] == "robin":
            robin_id = u["id"]

    resp = sandbox.api("POST", "/api/v1/groups/home/members", {
        "user_id": admin_id,
        "role": "admin",
    })
    r.check("Add admin to home as admin",
            resp["status"] == 200)

    resp = sandbox.api("POST", "/api/v1/groups/home/members", {
        "user_id": robin_id,
        "role": "viewer",
    })
    r.check("Add robin to home as viewer",
            resp["status"] == 200)

    # Step 10: Duplicate member rejected
    resp = sandbox.api("POST", "/api/v1/groups/home/members", {
        "user_id": admin_id,
        "role": "viewer",
    })
    r.check("Duplicate member rejected",
            resp["status"] == 409)

    # Step 11: Get group detail
    resp = sandbox.api("GET", "/api/v1/groups/home")
    r.check("Get home group detail",
            resp["status"] == 200)
    r.check("Home has 2 services",
            len(resp["data"].get("services", [])) == 2,
            f"services={len(resp['data'].get('services', []))}")
    r.check("Home has 2 members",
            len(resp["data"].get("members", [])) == 2,
            f"members={len(resp['data'].get('members', []))}")

    # Step 12: Update group
    resp = sandbox.api("PATCH", "/api/v1/groups/home", {
        "description": "Updated description",
    })
    r.check("Update group description",
            resp["status"] == 200)

    resp = sandbox.api("GET", "/api/v1/groups/home")
    r.check("Description updated",
            resp["data"].get("description") == "Updated description")

    # Step 13: Remove service from group
    resp = sandbox.api("DELETE", "/api/v1/groups/home/services/test-nas/indexer")
    r.check("Remove indexer from home",
            resp["status"] == 200)

    resp = sandbox.api("GET", "/api/v1/groups/home")
    r.check("Home now has 1 service",
            len(resp["data"].get("services", [])) == 1)

    # Step 14: Remove member from group
    resp = sandbox.api("DELETE", f"/api/v1/groups/home/members/{robin_id}")
    r.check("Remove robin from home",
            resp["status"] == 200)

    resp = sandbox.api("GET", "/api/v1/groups/home")
    r.check("Home now has 1 member",
            len(resp["data"].get("members", [])) == 1)

    # Step 15: Get group not found
    resp = sandbox.api("GET", "/api/v1/groups/nonexistent")
    r.check("Nonexistent group returns 404",
            resp["status"] == 404)

    # Step 16: Add indexer back for later tests
    sandbox.api("POST", "/api/v1/groups/home/services", {
        "service_id": "test-nas/indexer",
    })

    return r


# ===================================================================
# Cycle 4: Group-Scoped Status
# ===================================================================

def cycle_4_group_scoped_status(sandbox: Sandbox) -> TestResult:
    """Test group-scoped status endpoint.

    - Verify filtering works (only group services returned)
    - Verify device filtering (only devices with group services)
    - Verify counts are correct
    - Compare with full status to confirm scoping
    """
    r = TestResult("Cycle 4: Group-Scoped Status")

    # Step 1: Get home group status
    resp = sandbox.api("GET", "/api/v1/groups/home/status")
    r.check("Home group status responds",
            resp["status"] == 200,
            f"status={resp['status']}")

    home_data = resp["data"]
    r.check("Home status has group info",
            home_data.get("group", {}).get("id") == "home",
            f"group={home_data.get('group')}")

    # Step 2: Home group should only have test-nas services
    service_ids = [s["id"] for s in home_data.get("services", [])]
    r.check("Home has 2 services (mediaserver + indexer)",
            len(service_ids) == 2,
            f"services={service_ids}")
    r.check("Home has mediaserver",
            "test-nas/mediaserver" in service_ids)
    r.check("Home has indexer",
            "test-nas/indexer" in service_ids)
    r.check("Home does NOT have inference",
            "test-gpu/inference" not in service_ids)

    # Step 3: Home devices should only be test-nas
    device_ids = [d["id"] for d in home_data.get("devices", [])]
    r.check("Home has 1 device",
            len(device_ids) == 1,
            f"devices={device_ids}")
    r.check("Home device is test-nas",
            "test-nas" in device_ids)
    r.check("Home does NOT have test-gpu",
            "test-gpu" not in device_ids)

    # Step 4: Counts are correct
    counts = home_data.get("counts", {})
    r.check("Home device count = 1",
            counts.get("devices") == 1)
    r.check("Home service count = 2",
            counts.get("services") == 2)

    # Step 5: Work group status
    resp = sandbox.api("GET", "/api/v1/groups/work/status")
    r.check("Work group status responds",
            resp["status"] == 200)

    work_data = resp["data"]
    work_service_ids = [s["id"] for s in work_data.get("services", [])]
    r.check("Work has 1 service (inference)",
            len(work_service_ids) == 1 and "test-gpu/inference" in work_service_ids,
            f"services={work_service_ids}")

    work_device_ids = [d["id"] for d in work_data.get("devices", [])]
    r.check("Work has 1 device (test-gpu)",
            len(work_device_ids) == 1 and "test-gpu" in work_device_ids)

    # Step 6: Compare with full status
    resp = sandbox.api("GET", "/api/v1/status")
    full_counts = resp["data"]["counts"]
    r.check("Full status has more services than any group",
            full_counts["services"] > counts["services"],
            f"full={full_counts['services']} > home={counts['services']}")

    # Step 7: Nonexistent group status
    resp = sandbox.api("GET", "/api/v1/groups/nonexistent/status")
    r.check("Nonexistent group status returns 404",
            resp["status"] == 404)

    return r


# ===================================================================
# Cycle 5: Full Flow
# ===================================================================

def cycle_5_full_flow(sandbox: Sandbox) -> TestResult:
    """End-to-end: init -> user -> group -> services -> scoped status.

    Uses a fresh set of resources to test the complete lifecycle.
    """
    r = TestResult("Cycle 5: Full Flow (E2E)")

    # The admin 'admin' already exists from cycle 1.
    # Create a new user for this test.
    resp = sandbox.api("POST", "/api/v1/users", {
        "username": "alice",
        "password": "alicepass",
        "role": "user",
    })
    r.check("Create user alice",
            resp["status"] == 200)
    alice_id = resp["data"].get("user_id")

    # Register a new device and services
    register_device(sandbox, "e2e-host", "E2E-HOST", "samcloud", "192.0.2.200")
    register_service(sandbox, "e2e-host", "webapp", 3000, "webapp-test")
    register_service(sandbox, "e2e-host", "dbproxy", 5432)

    # Create a group
    resp = sandbox.api("POST", "/api/v1/groups", {
        "id": "e2e-test",
        "name": "E2E Test Group",
        "description": "End-to-end test group",
        "route_path": "/e2e",
    })
    r.check("Create e2e-test group",
            resp["status"] == 200)

    # Add only webapp to group (not dbproxy)
    resp = sandbox.api("POST", "/api/v1/groups/e2e-test/services", {
        "service_id": "e2e-host/webapp",
    })
    r.check("Add webapp to e2e group",
            resp["status"] == 200)

    # Add alice as member
    resp = sandbox.api("POST", "/api/v1/groups/e2e-test/members", {
        "user_id": alice_id,
        "role": "editor",
    })
    r.check("Add alice as editor",
            resp["status"] == 200)

    # Get group detail
    resp = sandbox.api("GET", "/api/v1/groups/e2e-test")
    r.check("E2E group has 1 service",
            len(resp["data"].get("services", [])) == 1)
    r.check("E2E group has 1 member",
            len(resp["data"].get("members", [])) == 1)
    r.check("Member is alice with editor role",
            resp["data"]["members"][0]["username"] == "alice"
            and resp["data"]["members"][0]["group_role"] == "editor")

    # Get scoped status
    resp = sandbox.api("GET", "/api/v1/groups/e2e-test/status")
    r.check("E2E scoped status has 1 service",
            len(resp["data"].get("services", [])) == 1)
    r.check("Scoped service is webapp",
            resp["data"]["services"][0]["name"] == "webapp")
    r.check("E2E scoped status has 1 device",
            len(resp["data"].get("devices", [])) == 1)
    r.check("dbproxy NOT in scoped status",
            all(s["name"] != "dbproxy" for s in resp["data"]["services"]))

    # Verify user can see their groups
    resp = sandbox.api("GET", f"/api/v1/users/{alice_id}")
    r.check("Alice's groups include e2e-test",
            any(g["id"] == "e2e-test" for g in resp["data"].get("groups", [])),
            f"groups={resp['data'].get('groups')}")

    # Verify audit trail
    resp = sandbox.api("GET", "/api/v1/audit?limit=50")
    actions = [a["action"] for a in resp["data"]]
    r.check("Audit has group.create",
            "group.create" in actions)
    r.check("Audit has group.service.add",
            "group.service.add" in actions)
    r.check("Audit has group.member.add",
            "group.member.add" in actions)
    r.check("Audit has user.create",
            "user.create" in actions)

    return r


# ===================================================================
# Main
# ===================================================================

def main():
    parser = argparse.ArgumentParser(description="samcloud Users & Groups Test Suite")
    parser.add_argument("--port", type=int, default=SANDBOX_PORT + 3,
                        help="Sandbox registry port (default: 7781)")
    args = parser.parse_args()

    sandbox = Sandbox(port=args.port)

    print(f"Starting users/groups test sandbox on :{args.port}...")
    try:
        sandbox.start(auto_auth=False)
        print(f"Sandbox running at {sandbox.base_url}")
        print(f"Temp DB at {sandbox.db_path}")
        print()

        results = [cycle_1_admin_init(sandbox)]
        # After admin init, set auth token for subsequent cycles
        sandbox.init_auth(username="admin", password="testpass123")
        results += [
            cycle_2_user_management(sandbox),
            cycle_3_group_management(sandbox),
            cycle_4_group_scoped_status(sandbox),
            cycle_5_full_flow(sandbox),
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
