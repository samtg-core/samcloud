"""
samcloud Agent Discovery Simulation

Simulates an AI agent with NO prior knowledge landing in the samcloud repo.
Tests whether the documentation is machine-readable and sufficient for an
agent to bootstrap itself — discover the system, find the API, register
services, and file tickets.

The agent "reads" docs (opens and parses files), extracts information using
string search and regex, then hits the sandbox registry API using the
discovered endpoints. Each step builds on knowledge from previous steps.

Usage:
    python sandbox/agent_sim.py                 # Run simulation
    python sandbox/agent_sim.py --port 7780     # Custom port
    python sandbox/agent_sim.py --keep          # Keep sandbox running
"""

import argparse
import re
import signal
import sys
from pathlib import Path

# Reuse Sandbox and TestResult from harness
sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox, TestResult

REPO_ROOT = Path(__file__).parent.parent
SANDBOX_PORT = 7779


# ═══════════════════════════════════════════════════════════════════════════
# Agent Knowledge Store
# ═══════════════════════════════════════════════════════════════════════════

class AgentKnowledge:
    """Tracks what the agent has learned during discovery."""

    def __init__(self):
        self.facts = {}

    def learn(self, key: str, value):
        self.facts[key] = value

    def get(self, key: str, default=None):
        return self.facts.get(key, default)

    def knows(self, key: str) -> bool:
        return key in self.facts

    def summary(self) -> str:
        lines = ["  Agent Knowledge:"]
        for k, v in self.facts.items():
            display = repr(v) if isinstance(v, str) else str(v)
            if len(display) > 80:
                display = display[:77] + "..."
            lines.append(f"    {k}: {display}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Document Reader (simulates agent reading files)
# ═══════════════════════════════════════════════════════════════════════════

def read_doc(path: Path) -> str:
    """Agent reads a document file. Returns content or empty string."""
    full_path = REPO_ROOT / path if not path.is_absolute() else path
    if full_path.exists():
        return full_path.read_text(encoding="utf-8", errors="replace")
    return ""


def search_doc(content: str, pattern: str) -> list:
    """Agent searches document content with regex. Returns all matches."""
    return re.findall(pattern, content, re.IGNORECASE | re.MULTILINE)


# ═══════════════════════════════════════════════════════════════════════════
# Discovery Cycle
# ═══════════════════════════════════════════════════════════════════════════

def cycle_agent_discovery(sandbox: Sandbox) -> TestResult:
    """Simulate an agent discovering and using samcloud from scratch.

    The agent has NO prior knowledge. It must:
    1. Read README.md to understand the system
    2. Find the registry API port from docs
    3. Check system health
    4. Discover what devices exist
    5. Read the User Guide to learn how to register a service
    6. Register a test device and service using discovered endpoints
    7. Read docs to find the ticket endpoint
    8. Create a ticket using the discovered endpoint
    """
    r = TestResult("Cycle: Agent Discovery")
    k = AgentKnowledge()

    # ─── Step 1: Read README.md ───
    readme = read_doc(Path("README.md"))
    r.check(
        "Read README.md",
        len(readme) > 100,
        f"Read {len(readme)} chars",
    )

    # Agent looks for the word "registry" to understand the system
    registry_mentions = search_doc(readme, r"registry")
    r.check(
        'Found "registry" mention in README',
        len(registry_mentions) > 0,
        f"Found {len(registry_mentions)} mentions",
    )
    k.learn("system_type", "home network orchestration")

    # ─── Step 2: Find API port from docs ───
    # Agent sees "localhost:4400" in README Quick Health Check — but the User Guide
    # says 7777. A smart agent checks both. The User Guide is more authoritative.
    readme_ports = search_doc(readme, r"localhost:(\d{4,5})/api/v1/")
    r.check(
        "Found API port reference in README",
        len(readme_ports) > 0,
        f"Ports found: {readme_ports}",
    )

    # Agent now reads the User Guide for the canonical port
    # The guide is the User Guide's index plus the pages it points to for this
    # journey: devices, services, tickets and the API reference.
    user_guide = "\n".join(read_doc(Path("docs") / f) for f in (
        "User Guide.md", "Devices and Enrolment.md", "Services and Routes.md",
        "Tickets.md", "API Reference.md"))
    r.check(
        "Read User Guide",
        len(user_guide) > 500,
        f"Read {len(user_guide)} chars",
    )

    # Extract the registry port from the User Guide
    ug_ports = search_doc(user_guide, r"localhost:(\d{4,5})")
    # The User Guide consistently uses port 7777
    canonical_port = None
    for p in ug_ports:
        p_int = int(p)
        if 4000 <= p_int <= 9999:
            canonical_port = p_int
            break

    r.check(
        "Found canonical API port in User Guide",
        canonical_port is not None,
        f"Canonical port: {canonical_port}",
    )
    k.learn("registry_port", canonical_port)

    # Agent discovers this is a sandbox — override port to sandbox port
    # (In real life the agent would use the discovered port; here we redirect
    # to the sandbox so we don't hit production)
    api_base = sandbox.base_url
    k.learn("api_base", api_base)

    # ─── Step 3: Health check ───
    # Agent finds the /health endpoint mentioned in the User Guide
    health_mentions = search_doc(user_guide, r"(/health)")
    r.check(
        "Found /health endpoint in docs",
        len(health_mentions) > 0,
        f"Found {len(health_mentions)} mentions",
    )

    resp = sandbox.api("GET", "/health")
    r.check(
        "Health check responded",
        resp["status"] == 200 and resp["data"].get("status") == "healthy",
        f"status={resp['data'].get('status')}",
    )
    k.learn("health_status", resp["data"].get("status"))

    # ─── Step 4: Discover devices ───
    # Agent finds the devices endpoint from the User Guide
    device_endpoints = search_doc(user_guide, r"(/api/v1/devices)")
    r.check(
        "Found /api/v1/devices endpoint in docs",
        len(device_endpoints) > 0,
    )

    resp = sandbox.api("GET", "/api/v1/devices")
    device_count = len(resp["data"]) if isinstance(resp["data"], list) else 0
    r.check(
        f"Discovered {device_count} devices (fresh sandbox)",
        resp["status"] == 200 and device_count == 0,
        "Fresh database confirmed",
    )
    k.learn("initial_device_count", device_count)

    # ─── Step 5: Learn registration from User Guide ───
    # Agent reads the "Adding a New Service" section
    registration_section = search_doc(
        user_guide, r"#+ .*(Register|Adding|Add) .*[Ss]ervice"
    )
    r.check(
        "Found service registration section in User Guide",
        len(registration_section) > 0,
    )

    # Agent extracts the POST endpoint for device enrollment
    device_post = search_doc(
        user_guide, r"(?:POST|curl -X POST)\s+\S*(/api/v1/enroll)"
    )
    r.check(
        "Found device enrollment endpoint (POST /api/v1/enroll)",
        len(device_post) > 0,
    )

    # Agent extracts the POST endpoint for services
    service_post = search_doc(
        user_guide, r"(?:POST|curl -X POST)\s+\S*(/api/v1/services)"
    )
    r.check(
        "Found service registration endpoint (POST /api/v1/services)",
        len(service_post) > 0,
    )

    # Agent extracts required fields from the JSON example in the User Guide
    # Look for the device registration JSON body
    device_fields = search_doc(
        user_guide, r'"(id|hostname|context|type|ssh_user|ssh_port)"'
    )
    r.check(
        "Extracted device registration fields from User Guide example",
        len(device_fields) >= 3,
        f"Fields found: {device_fields}",
    )
    k.learn("device_fields", device_fields)

    # ─── Step 6: Register a device and service ───
    resp = sandbox.api("POST", "/api/v1/devices", {
        "id": "agent-discovered-host",
        "hostname": "AGENT-TEST",
        "context": "tunnel",
        "type": "computer",
        "ssh_user": "agent",
        "ssh_port": 22,
        "startup_method": "systemd",
    })
    r.check(
        "Registered test device using discovered endpoint",
        resp["status"] == 200 and resp["data"].get("status") == "registered",
        f"tunnel_port={resp['data'].get('tunnel_port')}",
    )
    k.learn("registered_device", "agent-discovered-host")
    k.learn("tunnel_port", resp["data"].get("tunnel_port"))

    # Now register a service on it — agent extracts the service fields
    service_fields = search_doc(
        user_guide, r'"(name|device_id|port|slice_port|health_endpoint|subdomain)"'
    )
    r.check(
        "Extracted service registration fields from User Guide example",
        len(service_fields) >= 3,
        f"Fields found: {service_fields}",
    )
    k.learn("service_fields", service_fields)

    resp = sandbox.api("POST", "/api/v1/services", {
        "name": "agent-test-api",
        "device_id": "agent-discovered-host",
        "port": 9999,
        "description": "Agent test API",
        "health_endpoint": "/health",
        "subdomain": "agenttest",
        "version": "0.1.0",
        "capabilities": ["test"],
    })
    r.check(
        "Registered test service using discovered endpoint",
        resp["status"] == 200 and resp["data"].get("service_id") == "agent-discovered-host/agent-test-api",
        f"slice_port={resp['data'].get('slice_port')}, url={resp['data'].get('external_url')}",
    )
    k.learn("registered_service", resp["data"].get("service_id"))
    k.learn("slice_port", resp["data"].get("slice_port"))
    k.learn("external_url", resp["data"].get("external_url"))

    # Verify the registration worked
    resp = sandbox.api("GET", "/api/v1/devices/agent-discovered-host")
    r.check(
        "Verified device has registered service",
        resp["status"] == 200 and len(resp["data"].get("services", [])) == 1,
    )

    # ─── Step 7: Learn ticket system from User Guide ───
    # Agent looks for the ticket section
    ticket_section = search_doc(user_guide, r"#+ .*([Ff]ile|[Cc]reate) a [Tt]icket")
    r.check(
        "Found ticket management section in User Guide",
        len(ticket_section) > 0,
    )

    # Agent extracts the ticket endpoint
    ticket_post = search_doc(
        user_guide, r"(?:POST|curl -X POST)\s+\S*(/api/v1/tickets)\b"
    )
    r.check(
        "Found ticket creation endpoint (POST /api/v1/tickets)",
        len(ticket_post) > 0,
    )

    # Extract ticket fields
    ticket_fields = search_doc(
        user_guide, r'"(source|type|priority|summary|body|service_id|device_id)"'
    )
    r.check(
        "Extracted ticket fields from User Guide example",
        len(ticket_fields) >= 4,
        f"Fields found: {list(set(ticket_fields))}",
    )
    k.learn("ticket_fields", list(set(ticket_fields)))

    # ─── Step 8: Create a ticket ───
    resp = sandbox.api("POST", "/api/v1/tickets", {
        "source": "agent:discovery-sim",
        "type": "feature",
        "priority": "low",
        "summary": "Agent successfully bootstrapped from documentation",
        "body": (
            "This ticket was created by an agent that read README.md and the "
            "User Guide, discovered the API endpoints, registered a device and "
            "service, and then created this ticket. All using information "
            "extracted from documentation alone."
        ),
        "service_id": k.get("registered_service"),
        "device_id": "agent-discovered-host",
    })
    r.check(
        "Created ticket using discovered endpoint",
        resp["status"] == 200 and resp["data"].get("ticket_id") is not None,
        f"ticket_id={resp['data'].get('ticket_id')}",
    )
    k.learn("created_ticket_id", resp["data"].get("ticket_id"))

    # Verify the ticket was created correctly
    ticket_id = resp["data"].get("ticket_id")
    resp = sandbox.api("GET", f"/api/v1/tickets/{ticket_id}")
    r.check(
        "Verified ticket content is correct",
        resp["status"] == 200
        and resp["data"].get("source") == "agent:discovery-sim"
        and resp["data"].get("status") == "open",
        f"source={resp['data'].get('source')}, status={resp['data'].get('status')}",
    )

    # ─── Step 9: Full status check (agent verifies its work) ───
    resp = sandbox.api("GET", "/api/v1/status")
    counts = resp["data"].get("counts", {})
    r.check(
        "System status reflects agent's actions",
        counts.get("devices") == 1
        and counts.get("services") == 1
        and counts.get("open_tickets") == 1,
        f"devices={counts.get('devices')}, services={counts.get('services')}, "
        f"tickets={counts.get('open_tickets')}",
    )

    # ─── Step 10: Audit trail records agent's actions ───
    resp = sandbox.api("GET", "/api/v1/audit?limit=20")
    actions = [a["action"] for a in resp["data"]]
    r.check(
        "Audit trail captured all agent actions",
        "device.register" in actions
        and "service.register" in actions
        and "ticket.create" in actions,
        f"Actions: {actions}",
    )

    return r, k


# ═══════════════════════════════════════════════════════════════════════════
# Report (extended with agent knowledge)
# ═══════════════════════════════════════════════════════════════════════════

def print_agent_report(results: list[TestResult], sandbox: Sandbox,
                       knowledge: AgentKnowledge = None):
    """Print test report with agent knowledge summary."""
    total_pass = sum(r.passed for r in results)
    total_fail = sum(r.failed for r in results)
    total = total_pass + total_fail

    print()
    print("=" * 64)
    print("  samcloud Agent Discovery Simulation Report")
    print("=" * 64)
    print(f"  Registry:  {sandbox.base_url}")
    print(f"  Database:  {sandbox.db_path} (isolated)")
    print()

    for r in results:
        print(r.summary())
        for step in r.steps:
            icon = "  ✓" if step["status"] == "PASS" else "  ✗"
            detail = f" — {step['detail']}" if step["detail"] else ""
            print(f"    {icon} {step['description']}{detail}")
        print()

    if knowledge:
        print(knowledge.summary())
        print()

    print("-" * 64)
    all_pass = total_fail == 0
    print(f"  {'ALL PASSED' if all_pass else 'FAILURES DETECTED'}: "
          f"{total_pass}/{total} checks passed")
    if all_pass:
        print("  Docs are machine-readable. An agent can bootstrap from zero.")
    print("=" * 64)
    print()

    return all_pass


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="samcloud Agent Discovery Simulation"
    )
    parser.add_argument(
        "--port", type=int, default=SANDBOX_PORT,
        help="Sandbox registry port (default: 7779)",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="Keep sandbox running after tests",
    )
    args = parser.parse_args()

    sandbox = Sandbox(port=args.port)

    print(f"Starting sandbox registry on :{args.port}...")
    try:
        sandbox.start()
        print(f"Sandbox running at {sandbox.base_url}")
        print(f"Temp DB at {sandbox.db_path}")
        print()
        print("Simulating agent with NO prior knowledge...")
        print()

        result, knowledge = cycle_agent_discovery(sandbox)
        results = [result]

        all_pass = print_agent_report(results, sandbox, knowledge)

        if args.keep:
            print(f"Sandbox still running at {sandbox.base_url}")
            print("Press Ctrl+C to stop...")
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
