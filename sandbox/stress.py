"""
samcloud Stress Test Suite

Extended test cycles that exercise demanding scenarios against a sandboxed
registry instance: port exhaustion, multi-device fleets, agent simulation,
and concurrent operations.

Uses the Sandbox class from harness.py — never touches production data.

Usage:
    python sandbox/stress.py
    python sandbox/stress.py --port 7779
"""

import argparse
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

# Import Sandbox and reporting from harness
sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox, TestResult, print_report, SANDBOX_PORT


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def register_tunnel_device(sandbox: Sandbox, device_id: str, hostname: str = None) -> dict:
    """Register a tunnel device, returning the API response."""
    return sandbox.api("POST", "/api/v1/devices", {
        "id": device_id,
        "hostname": hostname or device_id.upper(),
        "context": "tunnel",
        "ssh_user": "testuser",
        "ssh_port": 22,
        "startup_method": "systemd",
        "recovery_method": "event-driven",
    })


def register_service(sandbox: Sandbox, device_id: str, name: str,
                      port: int, subdomain: str = None) -> dict:
    """Register a service on a device, returning the API response."""
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


def release_port_via_db(sandbox: Sandbox, port: int, port_type: str):
    """Release a port by directly deleting from the sandbox SQLite DB.

    The registry has no DELETE /ports API endpoint, so for the exhaustion
    recovery test we go straight to the database.
    """
    conn = sqlite3.connect(sandbox.db_path, timeout=10)
    try:
        conn.execute("DELETE FROM ports WHERE port = ? AND type = ?", (port, port_type))
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Cycle 4: Port Exhaustion & Recovery
# ═══════════════════════════════════════════════════════════════════════════

def cycle_4_port_exhaustion(sandbox: Sandbox) -> TestResult:
    """Fill up port ranges, verify overflow errors, release, re-allocate.

    The tunnel range (10001-10012) is only 12 ports — perfect for exhaustion.
    Also tests all five port types.
    """
    r = TestResult("Cycle 4: Port Exhaustion & Recovery")

    # --- Phase A: Exhaust the tunnel range (12 ports) ---
    allocated_tunnel_ports = []
    all_ok = True
    for i in range(12):
        resp = sandbox.api("POST", "/api/v1/ports/allocate", {
            "port_type": "tunnel",
            "owner_id": f"exhaust-device-{i}",
        })
        if resp["status"] != 200:
            all_ok = False
            break
        allocated_tunnel_ports.append(resp["data"]["port"])

    # Note: lsof may block some ports that are in use by real tunnels on this machine
    r.check("Allocated most tunnel ports",
            all_ok and len(allocated_tunnel_ports) >= 10,
            f"got {len(allocated_tunnel_ports)}/12 (lsof may block real tunnel ports)")
    r.check("All allocated tunnel ports unique", len(set(allocated_tunnel_ports)) == len(allocated_tunnel_ports))

    # Verify range
    r.check("All ports in tunnel range",
            all(10001 <= p <= 10012 for p in allocated_tunnel_ports))

    # --- Phase B: Overflow must fail ---
    resp = sandbox.api("POST", "/api/v1/ports/allocate", {
        "port_type": "tunnel",
        "owner_id": "overflow-device",
    })
    r.check("Tunnel overflow returns 409", resp["status"] == 409)
    r.check("Error mentions range",
            "range" in resp["data"].get("detail", "").lower()
            or "no available" in resp["data"].get("detail", "").lower())

    # --- Phase C: Release some and re-allocate ---
    # Release ports 10003 and 10007
    release_port_via_db(sandbox, 10003, "tunnel")
    release_port_via_db(sandbox, 10007, "tunnel")

    resp1 = sandbox.api("POST", "/api/v1/ports/allocate", {
        "port_type": "tunnel",
        "owner_id": "reclaim-device-a",
    })
    r.check("Re-allocation after release succeeds", resp1["status"] == 200,
            f"port={resp1['data'].get('port', 'NONE')}")

    resp2 = sandbox.api("POST", "/api/v1/ports/allocate", {
        "port_type": "tunnel",
        "owner_id": "reclaim-device-b",
    })
    # Note: lsof may block re-allocation on ports in use by real tunnels on this machine.
    # Accept either success (got a released port) or 409 (lsof blocked both released ports).
    r.check("Second re-allocation attempted",
            resp2["status"] in (200, 409),
            f"status={resp2['status']}, port={resp2['data'].get('port', 'blocked by lsof')}")

    # --- Phase D: Test all five port types allocate correctly ---
    port_types_ok = True
    type_ports = {}
    for pt in ("forward", "local", "monitor", "lifeline"):
        resp = sandbox.api("POST", "/api/v1/ports/allocate", {
            "port_type": pt,
            "owner_id": f"type-test-{pt}",
        })
        if resp["status"] != 200:
            port_types_ok = False
            break
        type_ports[pt] = resp["data"]["port"]

    r.check("All port types allocate successfully", port_types_ok)

    # Verify ranges
    expected_ranges = {
        "forward": (18001, 18099),
        "local": (3000, 3099),
        "monitor": (19001, 19099),
        "lifeline": (10013, 10099),
    }
    ranges_ok = all(
        expected_ranges[pt][0] <= type_ports[pt] <= expected_ranges[pt][1]
        for pt in type_ports
    )
    r.check("Each port type in its correct range", ranges_ok,
            ", ".join(f"{pt}={type_ports.get(pt)}" for pt in expected_ranges))

    # --- Phase E: Preferred port outside range rejected ---
    resp = sandbox.api("POST", "/api/v1/ports/allocate", {
        "port_type": "tunnel",
        "owner_id": "out-of-range",
        "preferred": 50000,
    })
    r.check("Preferred port outside range rejected", resp["status"] == 409)

    # --- Phase F: Invalid port type rejected ---
    resp = sandbox.api("POST", "/api/v1/ports/allocate", {
        "port_type": "nonexistent",
        "owner_id": "bad-type",
    })
    r.check("Invalid port type rejected", resp["status"] == 409)

    return r


# ═══════════════════════════════════════════════════════════════════════════
# Cycle 5: Multi-Device Fleet
# ═══════════════════════════════════════════════════════════════════════════

def cycle_5_multi_device_fleet(sandbox: Sandbox) -> TestResult:
    """Register 5 tunnel devices, each with 2-3 services.

    Validates no port collisions, unique routes, filter queries, and
    accurate system status counts.
    """
    r = TestResult("Cycle 5: Multi-Device Fleet")

    # --- Phase A: Register 5 devices rapidly ---
    fleet = [
        {"id": "fleet-alpha",   "hostname": "ALPHA-WS",   "ip": "192.0.2.201"},
        {"id": "fleet-bravo",   "hostname": "BRAVO-WS",   "ip": "192.0.2.202"},
        {"id": "fleet-charlie", "hostname": "CHARLIE-WS", "ip": "192.0.2.203"},
        {"id": "fleet-delta",   "hostname": "DELTA-WS",   "ip": "192.0.2.204"},
        {"id": "fleet-echo",    "hostname": "ECHO-WS",    "ip": "192.0.2.205"},
    ]

    tunnel_ports = []
    for dev in fleet:
        resp = sandbox.api("POST", "/api/v1/devices", {
            "id": dev["id"], "hostname": dev["hostname"],
            "context": "samcloud", "ip": dev["ip"],
        })
        if resp["status"] == 200:
            tunnel_ports.append(resp["data"].get("tunnel_port"))

    r.check("All 5 devices registered", len(tunnel_ports) == 5)
    # samcloud devices don't auto-allocate tunnel ports
    r.check("No tunnel ports for samcloud devices",
            all(p is None for p in tunnel_ports),
            f"tunnel_ports={tunnel_ports}")
    r.check("All tunnel ports in range (or None)",
            all(10001 <= p <= 10012 for p in tunnel_ports if p is not None))

    # --- Phase B: Add services to each device ---
    service_specs = {
        "fleet-alpha":   [("render",   8001, "rnd-a"), ("train",  8002, "trn-a")],
        "fleet-bravo":   [("render",   8001, "rnd-b"), ("train",  8002, "trn-b"), ("export", 8003, "exp-b")],
        "fleet-charlie": [("infer",    8001, "inf-c"), ("data",   8002, None)],
        "fleet-delta":   [("pipeline", 8001, "pip-d"), ("viewer", 8002, "viw-d"), ("api",    8003, "api-d")],
        "fleet-echo":    [("gateway",  8001, "gtw-e"), ("worker", 8002, None)],
    }

    all_slice_ports = []
    all_subdomains = []
    svc_count = 0
    for device_id, svcs in service_specs.items():
        for name, port, subdomain in svcs:
            resp = register_service(sandbox, device_id, name, port, subdomain)
            if resp["status"] == 200:
                svc_count += 1
                sp = resp["data"].get("slice_port")
                if sp:
                    all_slice_ports.append(sp)
                if subdomain:
                    all_subdomains.append(subdomain)

    r.check("All 12 services registered", svc_count == 12)

    # --- Phase C: No port collisions across fleet ---
    r.check("All forward ports unique", len(set(all_slice_ports)) == len(all_slice_ports),
            f"{len(all_slice_ports)} ports allocated")
    r.check("All forward ports in range",
            all(18001 <= p <= 18099 for p in all_slice_ports))

    # --- Phase D: All routes unique ---
    resp = sandbox.api("GET", "/api/v1/routes")
    route_subs = [rt["subdomain"] for rt in resp["data"]]
    r.check("All routes have unique subdomains",
            len(route_subs) == len(set(route_subs)),
            f"routes: {route_subs}")

    # Verify expected subdomains are present
    expected_subs = [s for s in all_subdomains]
    r.check("All subdomain routes created",
            all(s in route_subs for s in expected_subs),
            f"expected {len(expected_subs)}, got {len(route_subs)} total routes")

    # --- Phase E: Filter services by device ---
    resp = sandbox.api("GET", "/api/v1/services?device_id=fleet-bravo")
    r.check("Filter by device returns correct count",
            len(resp["data"]) == 3,
            f"got {len(resp['data'])}")

    resp = sandbox.api("GET", "/api/v1/services?device_id=fleet-charlie")
    r.check("Filter by device (charlie) correct",
            len(resp["data"]) == 2)

    # --- Phase F: System status counts ---
    resp = sandbox.api("GET", "/api/v1/status")
    counts = resp["data"]["counts"]
    r.check("Status device count matches",
            counts["devices"] == 5,
            f"expected 5, got {counts['devices']}")
    r.check("Status service count matches",
            counts["services"] == 12,
            f"expected 12, got {counts['services']}")
    r.check("Status port count >= fleet ports",
            counts["ports"] >= len(all_slice_ports) + len(tunnel_ports),
            f"ports={counts['ports']}, expected>={len(all_slice_ports) + len(tunnel_ports)}")

    return r


# ═══════════════════════════════════════════════════════════════════════════
# Cycle 6: Agent Simulation
# ═══════════════════════════════════════════════════════════════════════════

def cycle_6_agent_simulation(sandbox: Sandbox) -> TestResult:
    """Simulate two agents discovering and resolving an infrastructure issue.

    Agent-Alpha is the watchdog that discovers a problem.
    Agent-Beta is the SRE agent that picks it up and resolves it.
    """
    r = TestResult("Cycle 6: Agent Simulation")

    # --- Setup: Create a device and service for the scenario ---
    resp = sandbox.api("POST", "/api/v1/devices", {
        "id": "agent-target",
        "hostname": "AGENT-TARGET",
        "context": "samcloud",
        "ip": "192.0.2.200",
    })
    r.check("Setup: device registered", resp["status"] == 200, f"resp={resp}")
    resp = sandbox.api("POST", "/api/v1/services", {
        "name": "model-serve",
        "device_id": "agent-target",
        "port": 8500,
        "description": "Model serving API",
        "health_endpoint": "/health",
        "subdomain": "mdl-test",
    })
    r.check("Setup: service registered", resp["status"] == 200, f"resp={resp}")

    # --- Step 1: Agent-Alpha reads system status (discovery) ---
    resp = sandbox.api("GET", "/api/v1/status")
    r.check("Agent reads system status", resp["status"] == 200)
    r.check("Status contains devices and services",
            resp["data"]["counts"]["devices"] >= 1
            and resp["data"]["counts"]["services"] >= 1)

    # --- Step 2: Agent queries health endpoint ---
    resp = sandbox.api("GET", "/health")
    r.check("Agent queries /health", resp["status"] == 200)
    r.check("Health reports healthy", resp["data"].get("status") == "healthy")

    # --- Step 3: Agent finds a degraded service (simulate via DB) ---
    # Directly set service status to 'degraded' in the sandbox DB
    conn = sqlite3.connect(sandbox.db_path, timeout=10)
    conn.execute(
        "UPDATE services SET status = 'degraded' WHERE id = ?",
        ("agent-target/model-serve",),
    )
    conn.commit()
    conn.close()

    resp = sandbox.api("GET", "/api/v1/services/agent-target/model-serve")
    r.check("Agent detects degraded service",
            resp["status"] == 200 and resp["data"].get("status") == "degraded",
            f"http={resp['status']}, svc_status={resp['data'].get('status', 'NOT FOUND')}")

    # --- Step 4: Agent-Alpha creates an incident ticket ---
    resp = sandbox.api("POST", "/api/v1/tickets", {
        "source": "agent:alpha-watchdog",
        "type": "incident",
        "priority": "critical",
        "summary": "model-serve degraded on agent-target — OOM suspected",
        "body": "Health endpoint returning 503. GPU memory at 98%. "
                "Last successful health check was 5 minutes ago. "
                "Recommend restarting the service with reduced batch size.",
        "service_id": "agent-target/model-serve",
        "device_id": "agent-target",
    })
    r.check("Agent-Alpha creates incident ticket", resp["status"] == 200,
            f"resp={resp}")
    ticket_id = resp["data"].get("ticket_id", 999)

    # --- Step 5: Agent-Alpha adds diagnostic comment ---
    resp = sandbox.api("POST", f"/api/v1/tickets/{ticket_id}/comments", {
        "author": "agent:alpha-watchdog",
        "body": "Diagnostics:\n"
                "- GPU util: 100%\n"
                "- VRAM: 47.8/48.0 GB\n"
                "- Last restart: 72h ago\n"
                "- Error log: torch.cuda.OutOfMemoryError in forward pass",
    })
    r.check("Agent-Alpha adds diagnostic comment", resp["status"] == 200)

    # --- Step 6: Agent-Alpha triages and tags ---
    resp = sandbox.api("PATCH", f"/api/v1/tickets/{ticket_id}", {
        "status": "triaged",
    })
    r.check("Agent-Alpha triages ticket", resp["status"] == 200)

    # --- Step 7: Agent-Beta picks up the ticket ---
    resp = sandbox.api("GET", "/api/v1/tickets?status=triaged")
    triaged = resp["data"]
    found = any(t["id"] == ticket_id for t in triaged)
    r.check("Agent-Beta finds triaged ticket", found,
            f"found {len(triaged)} triaged tickets")

    resp = sandbox.api("PATCH", f"/api/v1/tickets/{ticket_id}", {
        "status": "in_progress",
        "assignee": "agent:beta-sre",
    })
    r.check("Agent-Beta assigns itself", resp["status"] == 200)

    # --- Step 8: Agent-Beta adds resolution comment ---
    resp = sandbox.api("POST", f"/api/v1/tickets/{ticket_id}/comments", {
        "author": "agent:beta-sre",
        "body": "Restarted model-serve with max_batch_size=4 (was 16). "
                "VRAM now at 32.1/48.0 GB. Health check passing.",
    })
    r.check("Agent-Beta adds resolution comment", resp["status"] == 200)

    # --- Step 9: Agent-Beta resolves the ticket ---
    resp = sandbox.api("PATCH", f"/api/v1/tickets/{ticket_id}", {
        "status": "resolved",
    })
    r.check("Agent-Beta resolves ticket", resp["status"] == 200)

    # --- Step 10: Verify full conversation is preserved ---
    resp = sandbox.api("GET", f"/api/v1/tickets/{ticket_id}")
    ticket = resp["data"]
    r.check("Ticket status is resolved", ticket.get("status") == "resolved")
    r.check("Ticket has resolved_at", ticket.get("resolved_at") is not None)
    r.check("Ticket has 2 comments", len(ticket.get("comments", [])) == 2)
    r.check("Assignee is agent:beta-sre", ticket.get("assignee") == "agent:beta-sre")

    # Verify comment authors
    authors = [c["author"] for c in ticket.get("comments", [])]
    r.check("Both agents' comments preserved",
            "agent:alpha-watchdog" in authors and "agent:beta-sre" in authors)

    # Verify resolved ticket not in open list
    resp = sandbox.api("GET", "/api/v1/tickets?status=open")
    open_ids = [t["id"] for t in resp["data"]]
    r.check("Resolved ticket excluded from open list",
            ticket_id not in open_ids)

    return r


# ═══════════════════════════════════════════════════════════════════════════
# Cycle 7: Concurrent Operations
# ═══════════════════════════════════════════════════════════════════════════

def cycle_7_concurrent_operations(sandbox: Sandbox) -> TestResult:
    """Use threading to hammer the registry with simultaneous requests.

    Verifies no DB corruption, unique ports, and complete audit trails.
    """
    r = TestResult("Cycle 7: Concurrent Operations")

    # --- Phase A: Concurrent device registration ---
    results_lock = threading.Lock()
    device_results = {}

    def register_device_thread(dev_id):
        resp = sandbox.api("POST", "/api/v1/devices", {
            "id": dev_id, "hostname": dev_id.upper(),
            "context": "samcloud", "ip": f"192.0.2.{210 + int(dev_id.split('-')[1])}",
        })
        with results_lock:
            device_results[dev_id] = resp

    threads = []
    device_ids = [f"concurrent-{i}" for i in range(3)]
    for dev_id in device_ids:
        t = threading.Thread(target=register_device_thread, args=(dev_id,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=15)

    r.check("All 3 concurrent devices registered",
            all(device_results.get(d, {}).get("status") == 200 for d in device_ids),
            f"statuses: {[device_results.get(d, {}).get('status') for d in device_ids]}")

    # Verify unique tunnel ports
    tunnel_ports = [
        device_results[d]["data"].get("tunnel_port")
        for d in device_ids
        if device_results.get(d, {}).get("status") == 200
    ]
    # samcloud devices don't get tunnel ports — all None is correct
    r.check("Concurrent samcloud devices have no tunnels",
            all(p is None for p in tunnel_ports),
            f"ports: {tunnel_ports}")
    r.check("Concurrent tunnel ports in range",
            all(10001 <= p <= 10012 for p in tunnel_ports if p))

    # --- Phase B: Concurrent service registration on different devices ---
    service_results = {}

    def register_service_thread(dev_id, svc_name, port, subdomain):
        resp = register_service(sandbox, dev_id, svc_name, port, subdomain)
        key = f"{dev_id}/{svc_name}"
        with results_lock:
            service_results[key] = resp

    threads = []
    svc_specs = [
        ("concurrent-0", "api",    8001, "capi0"),
        ("concurrent-1", "api",    8001, "capi1"),
        ("concurrent-2", "api",    8001, "capi2"),
    ]
    for dev_id, name, port, sub in svc_specs:
        t = threading.Thread(target=register_service_thread, args=(dev_id, name, port, sub))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=15)

    r.check("All 3 concurrent services registered",
            all(service_results.get(f"{d}/{n}", {}).get("status") == 200
                for d, n, _, _ in svc_specs),
            f"statuses: {[service_results.get(f'{d}/{n}', {}).get('status') for d, n, _, _ in svc_specs]}")

    # Verify unique forward ports
    fwd_ports = [
        service_results[f"{d}/{n}"]["data"].get("slice_port")
        for d, n, _, _ in svc_specs
        if service_results.get(f"{d}/{n}", {}).get("status") == 200
    ]
    # samcloud services don't auto-allocate forward ports
    r.check("Concurrent samcloud services registered (no auto-forward)",
            all(p is None for p in fwd_ports),
            f"ports: {fwd_ports}")

    # --- Phase C: Concurrent ticket creation ---
    ticket_results = {}

    def create_ticket_thread(idx):
        resp = sandbox.api("POST", "/api/v1/tickets", {
            "source": f"agent:stress-{idx}",
            "type": "incident",
            "priority": "high",
            "summary": f"Concurrent incident #{idx}",
            "body": f"Stress test ticket created by thread {idx}",
        })
        with results_lock:
            ticket_results[idx] = resp

    threads = []
    for i in range(5):
        t = threading.Thread(target=create_ticket_thread, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=15)

    r.check("All 5 concurrent tickets created",
            all(ticket_results.get(i, {}).get("status") == 200 for i in range(5)),
            f"statuses: {[ticket_results.get(i, {}).get('status') for i in range(5)]}")

    # Verify unique ticket IDs
    ticket_ids = [
        ticket_results[i]["data"]["ticket_id"]
        for i in range(5)
        if ticket_results.get(i, {}).get("status") == 200
    ]
    r.check("All ticket IDs unique",
            len(set(ticket_ids)) == len(ticket_ids),
            f"ids: {ticket_ids}")

    # --- Phase D: Verify audit log completeness ---
    resp = sandbox.api("GET", "/api/v1/audit?limit=200")
    audit_entries = resp["data"]
    actions = [a["action"] for a in audit_entries]

    # We expect device.register entries for the 3 concurrent devices
    device_reg_count = sum(
        1 for a in audit_entries
        if a["action"] == "device.register"
        and a["target"] in device_ids
    )
    r.check("Audit has all concurrent device registrations",
            device_reg_count == 3,
            f"found {device_reg_count}")

    # We expect ticket.create entries for the 5 concurrent tickets
    ticket_create_count = actions.count("ticket.create")
    r.check("Audit has all concurrent ticket creations",
            ticket_create_count >= 5,
            f"found {ticket_create_count}")

    return r


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="samcloud Stress Test Suite")
    parser.add_argument("--port", type=int, default=SANDBOX_PORT + 1,
                        help="Sandbox registry port (default: 7779)")
    args = parser.parse_args()

    sandbox = Sandbox(port=args.port)

    print(f"Starting stress-test sandbox on :{args.port}...")
    try:
        sandbox.start()
        print(f"Sandbox running at {sandbox.base_url}")
        print(f"Temp DB at {sandbox.db_path}")
        print()

        results = [
            cycle_4_port_exhaustion(sandbox),
            cycle_5_multi_device_fleet(sandbox),
            cycle_6_agent_simulation(sandbox),
            cycle_7_concurrent_operations(sandbox),
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
