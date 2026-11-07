"""Port allocator — atomic allocation with range enforcement and lsof cross-check."""

import os
import subprocess
from db import query, query_one, transaction, audit

# Port ranges — env vars override defaults so staging/production can split ranges
RANGES = {
    "tunnel":   (int(os.environ.get("PORT_RANGE_TUNNEL_START", "10001")),
                 int(os.environ.get("PORT_RANGE_TUNNEL_END", "10099"))),
    "lifeline": (10013, 10099),
    "forward":  (int(os.environ.get("PORT_RANGE_FORWARD_START", "18001")),
                 int(os.environ.get("PORT_RANGE_FORWARD_END", "18099"))),
    # A service's own port on the plane when there is no domain to name it under
    # (#1010): the bundled Caddy publishes this range and serves each one plain HTTP.
    "route":    (int(os.environ.get("PORT_RANGE_ROUTE_START", "18101")),
                 int(os.environ.get("PORT_RANGE_ROUTE_END", "18110"))),
    "local":    (3000, 3099),
    "monitor":  (19001, 19099),
}


def allocate(port_type: str, owner_id: str, preferred: int = None) -> int:
    """Atomically allocate the next available port in range.

    Args:
        port_type: One of 'tunnel', 'lifeline', 'forward', 'route', 'local', 'monitor'
        owner_id: Device or service ID that owns this port
        preferred: Optional preferred port (used if available)

    Returns:
        Allocated port number

    Raises:
        ValueError: If port_type is invalid or range is full
        RuntimeError: If preferred port is already allocated
    """
    if port_type not in RANGES:
        raise ValueError(f"Unknown port type: {port_type}. Must be one of {list(RANGES.keys())}")

    lo, hi = RANGES[port_type]

    with transaction() as conn:
        # Try preferred port first
        if preferred is not None:
            if not (lo <= preferred <= hi):
                raise ValueError(f"Preferred port {preferred} outside {port_type} range [{lo}-{hi}]")

            existing = conn.execute(
                "SELECT owner_id FROM ports WHERE port = ? AND type = ?",
                (preferred, port_type),
            ).fetchone()

            if existing:
                if existing["owner_id"] == owner_id:
                    return preferred  # Already allocated to this owner
                raise RuntimeError(
                    f"Port {preferred} already allocated to {existing['owner_id']}"
                )

            # Check lsof
            if _port_in_use(preferred):
                raise RuntimeError(f"Port {preferred} is in use by another process (lsof)")

            conn.execute(
                "INSERT INTO ports (port, type, owner_id) VALUES (?, ?, ?)",
                (preferred, port_type, owner_id),
            )
            audit("system:registry", "port.allocate", owner_id,
                  {"port": preferred, "type": port_type, "method": "preferred"}, conn=conn)
            return preferred

        # Find next available in range
        allocated = {
            r["port"]
            for r in conn.execute(
                "SELECT port FROM ports WHERE type = ? AND port BETWEEN ? AND ?",
                (port_type, lo, hi),
            ).fetchall()
        }

        for port in range(lo, hi + 1):
            if port not in allocated and not _port_in_use(port):
                conn.execute(
                    "INSERT INTO ports (port, type, owner_id) VALUES (?, ?, ?)",
                    (port, port_type, owner_id),
                )
                audit("system:registry", "port.allocate", owner_id,
                      {"port": port, "type": port_type, "method": "auto"}, conn=conn)
                return port

        raise ValueError(f"No available ports in {port_type} range [{lo}-{hi}]")


def release(port: int, port_type: str):
    """Release a port allocation."""
    with transaction() as conn:
        conn.execute("DELETE FROM ports WHERE port = ? AND type = ?", (port, port_type))
        audit("system:registry", "port.release", None, {"port": port, "type": port_type}, conn=conn)


def check(port: int, port_type: str) -> dict | None:
    """Check if a port is allocated."""
    return query_one(
        "SELECT port, type, owner_id, allocated_at FROM ports WHERE port = ? AND type = ?",
        (port, port_type),
    )


def list_all(port_type: str = None) -> list[dict]:
    """List all allocated ports, optionally filtered by type."""
    if port_type:
        return query("SELECT * FROM ports WHERE type = ? ORDER BY port", (port_type,))
    return query("SELECT * FROM ports ORDER BY type, port")


def _port_in_use(port: int) -> bool:
    """Check if a port is in use via lsof."""
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{port}", "-P", "-n"],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0 and "LISTEN" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def owned(port_type: str, owner_id: str) -> int | None:
    """The port of this type already allocated to owner_id, if any."""
    row = query_one("SELECT port FROM ports WHERE type = ? AND owner_id = ? ORDER BY port LIMIT 1",
                    (port_type, owner_id))
    return row["port"] if row else None
