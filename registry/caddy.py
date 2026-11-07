"""Caddy config generator — builds Caddyfile blocks from registry routes.

Supports staging isolation via environment variable:
  CADDYFILE_PATH  — the Caddyfile to manage (default: /var/lib/caddy/Caddyfile)
  CADDY_BIN       — caddy binary path (default: caddy)
  CADDY_ADMIN_PORT — admin API port (default: 2019)

Each environment (staging, production) runs its own Caddy process with its own config.
"""

import os
import re
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from db import query, audit


def _caddyfile() -> Path:
    return Path(os.environ.get("CADDYFILE_PATH", "/var/lib/caddy/Caddyfile"))


def _caddy_bin() -> str:
    return os.environ.get("CADDY_BIN", "caddy")


def _tls_snippet_name() -> str | None:
    """Find a TLS snippet in the Caddyfile by scanning for (xxx-tls) pattern."""
    caddyfile = _caddyfile()
    if not caddyfile.exists():
        return None
    content = caddyfile.read_text()
    import re
    match = re.search(r'\(([a-z0-9_-]+-tls)\)', content)
    return match.group(1) if match else None


def by_port(domain: str) -> bool:
    """True when routes under `domain` are reached by port, not by name (#1010).

    An IP address can't carry subdomains: `hello.192.0.2.10` names nothing. So
    with no domain configured, a plane on a bare address gives each route its
    own port from the `route` range, served plain HTTP at http://<address>:<port>.
    That needs no DNS and no certificate, which is what a LAN first run has.
    """
    import ipaddress
    try:
        ipaddress.ip_address(domain.strip("[]"))
        return True
    except ValueError:
        return False


def route_port(subdomain: str, domain: str) -> int:
    """The route's port on the plane, allocated the first time it is asked for."""
    import ports
    owner = f"route:{subdomain}.{domain}"
    return ports.owned("route", owner) or ports.allocate("route", owner)


def release_route_port(subdomain: str, domain: str) -> None:
    import ports
    port = ports.owned("route", f"route:{subdomain}.{domain}")
    if port:
        ports.release(port, "route")


def generate_block(subdomain: str, domain: str, target: str, extra: dict = None) -> str:
    """Generate a Caddy site block for a route.

    If the Caddyfile has a TLS snippet (stg-tls), imports it for DNS-01 cert provisioning.
    A route under a bare address is served on its own port, plain HTTP (by_port).
    """
    fqdn = f"{subdomain}.{domain}" if subdomain != "@" else domain
    if by_port(domain):
        lines = [f"http://:{route_port(subdomain, domain)} {{"]
    else:
        lines = [f"{fqdn} {{"]
    tls_snippet = _tls_snippet_name()
    if tls_snippet and not by_port(domain):
        lines.append(f"    import {tls_snippet}")
    lines.append(f"    reverse_proxy {target}")
    if extra:
        if extra.get("websocket"):
            pass
        if extra.get("tls_skip_verify"):
            lines[-1] = f"    reverse_proxy {target} {{"
            lines.append("        transport http {")
            lines.append("            tls_insecure_skip_verify")
            lines.append("        }")
            lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


def validate_config(config_path: str = None) -> tuple[bool, str]:
    """Run caddy validate on a config file."""
    path = config_path or str(_caddyfile())
    try:
        result = subprocess.run(
            [_caddy_bin(), "validate", "--config", path],
            capture_output=True, text=True, timeout=15,
        )
        valid = "Valid configuration" in result.stderr or result.returncode == 0
        return valid, result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def _reload_via_admin_api() -> tuple[bool, str]:
    """Reload Caddy via the admin API (hot-reload, provisions TLS certs immediately)."""
    admin_host = os.environ.get("CADDY_ADMIN_HOST", "localhost")
    admin_port = os.environ.get("CADDY_ADMIN_PORT", "2019")
    caddyfile = _caddyfile()
    if not caddyfile.exists():
        return False, "Caddyfile not found"

    url = f"http://{admin_host}:{admin_port}/load"
    data = caddyfile.read_text().encode("utf-8")

    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "text/caddyfile"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, f"Admin API reload OK (HTTP {resp.status}): {body[:500]}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return False, f"Admin API HTTP {e.code}: {body[:500]}"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, f"Admin API unreachable: {e}"


def _reload_via_cli() -> tuple[bool, str]:
    """Reload Caddy via CLI command (fallback)."""
    try:
        result = subprocess.run(
            [_caddy_bin(), "reload", "--config", str(_caddyfile())],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0, result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def reload_caddy() -> tuple[bool, str]:
    """Reload Caddy with current config.

    Tries admin API first (hot-reload, triggers TLS provisioning).
    Falls back to CLI reload if admin API is unreachable.
    """
    # Try admin API first
    success, api_msg = _reload_via_admin_api()
    if success:
        audit("system:caddy", "caddy.reload", None,
              {"method": "admin_api", "success": True, "output": api_msg[:500]})
        return True, api_msg

    # Fall back to CLI
    cli_success, cli_msg = _reload_via_cli()
    audit("system:caddy", "caddy.reload", None,
          {"method": "cli_fallback", "success": cli_success,
           "api_error": api_msg[:200], "output": cli_msg[:500]})
    if cli_success:
        return True, f"Admin API failed ({api_msg}), CLI fallback OK: {cli_msg}"
    return False, f"Both methods failed. API: {api_msg} | CLI: {cli_msg}"


def append_route(subdomain: str, domain: str, target: str, extra: dict = None) -> tuple[bool, str]:
    """Append a new route to the Caddyfile and reload.

    Returns (success, message). Rolls back on failure.
    """
    caddyfile = _caddyfile()
    backup = caddyfile.with_suffix(".bak.registry")
    block = generate_block(subdomain, domain, target, extra)

    current = caddyfile.read_text() if caddyfile.exists() else ""

    # Check if route already exists
    fqdn = f"{subdomain}.{domain}"
    if f"# {fqdn} (registry-managed)" in current:
        return True, f"Route {fqdn} already in Caddyfile"

    backup.write_text(current)

    new_config = current.rstrip() + "\n\n" + f"# {fqdn} (registry-managed)\n" + block + "\n"
    caddyfile.write_text(new_config)

    success, msg = reload_caddy()
    if not success:
        caddyfile.write_text(current)
        return False, f"Reload failed, rolled back: {msg}"

    audit("system:caddy", "route.append", fqdn,
          {"target": target, "extra": extra})
    return True, f"Route {fqdn} → {target} added and active"


def remove_route(subdomain: str, domain: str) -> tuple[bool, str]:
    """Remove a route block from the Caddyfile and reload.

    Returns (success, message). Rolls back on failure.
    """
    caddyfile = _caddyfile()
    if not caddyfile.exists():
        return False, "Caddyfile not found"

    backup = caddyfile.with_suffix(".bak.registry")
    current = caddyfile.read_text()
    backup.write_text(current)

    fqdn = f"{subdomain}.{domain}"
    # Match: comment line, then the site block (address may be fqdn or http://fqdn:port)
    # A route under a bare address is served on its port instead (by_port).
    pattern = rf'\n*# {re.escape(fqdn)} \(registry-managed\)\n(?:\S*{re.escape(fqdn)}\S*|http://:\d+) \{{[^}}]*\}}\n?'
    new_config = re.sub(pattern, '\n', current)

    if new_config == current:
        if by_port(domain):
            release_route_port(subdomain, domain)   # nothing serves it, so its port is free
        return False, f"Route {fqdn} not found in Caddyfile"

    caddyfile.write_text(new_config)

    success, msg = reload_caddy()
    if not success:
        caddyfile.write_text(current)
        return False, f"Reload failed, rolled back: {msg}"

    if by_port(domain):
        release_route_port(subdomain, domain)
    audit("system:caddy", "route.remove", fqdn, {})
    return True, f"Route {fqdn} removed"
