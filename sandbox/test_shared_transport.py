#!/usr/bin/env python3
"""Shared-transport enrolment (ticket #100, first deliverable).

A secondary user on an already-enrolled machine borrows that machine's reverse
tunnel instead of allocating its own — differentiated only by outbound_ssh_user
at gateway auth (the plane host's multi-user pattern, generalized to remote machines).

Contract under test:
  1. transport_owner mirrors the owner's tunnel_port (so the gateway routes the
     secondary through the same landing port) and coerces outbound_method.
  2. No NEW port is allocated and the ledger is NOT double-booked.
  3. Bootstrap injects an EMPTY tunnel port for a borrowing device (it skips
     standing up a tunnel) while a normal reverse-tunnel device still gets one.
  4. Validation: unknown owner / owner-without-tunnel are rejected 400.
  5. DELETE /ports/{port} reclaims an orphaned reservation.

Run: python3 sandbox/test_shared_transport.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox  # noqa: E402

PASS, FAIL = 0, 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"      \033[0;32m✓\033[0m {desc}")
    else:
        FAIL += 1
        print(f"      \033[0;31m✗\033[0m {desc}  {detail}")


def main():
    sb = Sandbox()
    sb.start()
    try:
        # ── owner machine: a normal reverse-tunnel device gets a real port ──
        r = sb.api("POST", "/api/v1/devices", {
            "id": "host-mac", "outbound_method": "reverse-tunnel",
            "outbound_ssh_user": "you"})
        check("Owner registered", r["status"] == 200, r["data"])
        owner_port = r["data"].get("tunnel_port")
        check("Owner got an allocated tunnel port", isinstance(owner_port, int), owner_port)

        # ── secondary user: borrows the owner's tunnel ──
        r = sb.api("POST", "/api/v1/devices", {
            "id": "host-claude", "transport_owner": "host-mac",
            "outbound_ssh_user": "claude-host"})
        check("Secondary registered", r["status"] == 200, r["data"])
        check("Secondary mirrors owner's tunnel_port",
              r["data"].get("tunnel_port") == owner_port,
              f'{r["data"].get("tunnel_port")} != {owner_port}')

        dev = sb.api("GET", "/api/v1/devices/host-claude")["data"]
        check("Secondary outbound_method coerced to reverse-tunnel",
              dev.get("outbound_method") == "reverse-tunnel", dev.get("outbound_method"))
        check("Secondary records transport_owner",
              dev.get("transport_owner") == "host-mac", dev.get("transport_owner"))

        # ── ledger: the shared port is owned once, by the owner only ──
        ports = sb.api("GET", "/api/v1/ports?type=tunnel")["data"]
        owners = [p["owner_id"] for p in ports if p["port"] == owner_port]
        check("Shared port not double-booked (owned once)", len(owners) == 1, owners)
        check("Shared port owned by the owner device, not the secondary",
              owners == ["host-mac"], owners)

        # ── bootstrap injection: the only signal that gates the tunnel step is
        # the injected `TUNNEL="..."` value at the top of the script — empty for
        # a borrower (skips the whole tunnel block), the port for an owner. The
        # literal "Tunnel" UI text lives in the source either way, so we assert
        # on the assignment line specifically.
        import urllib.request

        def bootstrap_tunnel_value(device_id):
            tok = sb.api("POST", f"/api/v1/enroll/{device_id}/retoken")
            t = tok["data"].get("token")
            # /bootstrap returns a shell script (text/x-shellscript), not JSON —
            # fetch raw rather than through the JSON-parsing api() helper.
            with urllib.request.urlopen(f"{sb.base_url}/api/v1/bootstrap/{t}", timeout=10) as r:
                body = r.read().decode()
            for line in body.splitlines():
                if line.startswith('TUNNEL="'):
                    return line.split('"', 2)[1]  # value between the quotes
            return None

        check("Owner bootstrap carries its tunnel port",
              bootstrap_tunnel_value("host-mac") == str(owner_port),
              bootstrap_tunnel_value("host-mac"))
        check("Borrowing bootstrap carries an EMPTY tunnel port (skips tunnel step)",
              bootstrap_tunnel_value("host-claude") == "",
              repr(bootstrap_tunnel_value("host-claude")))

        # ── validation ──
        r = sb.api("POST", "/api/v1/devices", {
            "id": "bad1", "transport_owner": "does-not-exist"})
        check("Unknown transport_owner rejected 400", r["status"] == 400, r["status"])

        sb.api("POST", "/api/v1/devices", {"id": "lan-box", "outbound_method": "direct-ssh",
                                           "outbound_ip": "192.0.2.9", "outbound_ssh_user": "you"})
        r = sb.api("POST", "/api/v1/devices", {"id": "bad2", "transport_owner": "lan-box"})
        check("transport_owner without a tunnel rejected 400", r["status"] == 400, r["status"])

        # ── port release endpoint ──
        r = sb.api("POST", "/api/v1/ports/allocate", {"port_type": "tunnel", "owner_id": "scratch"})
        p = r["data"].get("port")
        check("Scratch port allocated", isinstance(p, int), r["data"])
        r = sb.api("DELETE", f"/api/v1/ports/{p}")
        check("Port released 200", r["status"] == 200, r["data"])
        still = sb.api("GET", f"/api/v1/ports/check/{p}")["data"]
        check("Released port no longer allocated", still.get("allocated") == [], still.get("allocated"))
        r = sb.api("DELETE", "/api/v1/ports/19999")
        check("Releasing an unallocated port 404s", r["status"] == 404, r["status"])

        print()
        print("-" * 60)
        ok = FAIL == 0
        colour = "\033[0;32m" if ok else "\033[0;31m"
        print(f"  {colour}{'ALL PASSED' if ok else 'FAILURES'}: {PASS} passed, {FAIL} failed\033[0m")
        return 0 if ok else 1
    finally:
        sb.stop()


if __name__ == "__main__":
    sys.exit(main())
