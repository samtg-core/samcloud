"""DNS record management via Namecheap API.

Supports staging isolation — only manages records for the configured domain.
Credentials loaded from NAMECHEAP_API_USER / NAMECHEAP_API_KEY env vars
or from ~/.config/samcloud/namecheap.env.

CRITICAL: Namecheap's setHosts replaces ALL records. Every operation
must GET existing records first and include them all in the SET call.
"""

import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from db import audit

API_URL = "https://api.namecheap.com/xml.response"

# Verify-and-retry: Caddy's libdns Namecheap plugin and this module both do
# read-modify-write against the same zone via setHosts (replaces all records).
# When their windows overlap, one clobbers the other's append. After every
# write we re-read and confirm our record is in the expected state; if not,
# retry from a fresh snapshot. See ticket #58.
_MAX_RETRIES = 3
_RETRY_DELAY_S = 2.0


def _load_credentials() -> tuple[str, str]:
    """Load Namecheap API credentials from env or config file."""
    user = os.environ.get("NAMECHEAP_API_USER")
    key = os.environ.get("NAMECHEAP_API_KEY")

    if not user or not key:
        creds_file = Path.home() / ".config" / "samcloud" / "namecheap.env"
        if creds_file.exists():
            for line in creds_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "NAMECHEAP_API_USER":
                    user = v
                elif k == "NAMECHEAP_API_KEY":
                    key = v

    if not user or not key:
        raise RuntimeError("NAMECHEAP_API_USER and NAMECHEAP_API_KEY must be set")

    return user, key


def _get_public_ip() -> str:
    """Get current public IP address."""
    try:
        return urlopen("https://ifconfig.me", timeout=10).read().decode().strip()
    except Exception:
        return urlopen("https://api.ipify.org", timeout=10).read().decode().strip()


def _split_domain(domain: str) -> tuple[str, str]:
    """Split domain into SLD and TLD (e.g. 'example.org' -> ('example', 'org'))."""
    parts = domain.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Cannot split domain: {domain}")
    return parts[0], parts[1]


def _api_call(command: str, extra_params: dict = None) -> ET.Element:
    """Make a Namecheap API call and return parsed XML root.

    Uses POST for setHosts (large payloads exceed URL length limits with 100+ records).
    Uses GET for everything else.
    """
    user, key = _load_credentials()
    ip = _get_public_ip()

    params = {
        "ApiUser": user,
        "ApiKey": key,
        "UserName": user,
        "ClientIp": ip,
        "Command": command,
    }
    if extra_params:
        params.update(extra_params)

    if "setHosts" in command:
        # POST to avoid URL length limits with many records
        data = urlencode(params).encode("utf-8")
        req = Request(API_URL, data=data, method="POST")
        response = urlopen(req, timeout=30).read().decode()
    else:
        url = f"{API_URL}?{urlencode(params)}"
        response = urlopen(url, timeout=15).read().decode()
    root = ET.fromstring(response)

    status = root.attrib.get("Status", "")
    if status != "OK":
        errors = root.findall(".//{http://api.namecheap.com/xml.response}Error")
        if not errors:
            errors = root.findall(".//Error")
        msg = "; ".join(e.text or "unknown" for e in errors) if errors else response[:200]
        raise RuntimeError(f"Namecheap API error: {msg}")

    return root


def get_records(domain: str) -> list[dict]:
    """Get all DNS host records for a domain."""
    sld, tld = _split_domain(domain)
    root = _api_call("namecheap.domains.dns.getHosts", {"SLD": sld, "TLD": tld})

    records = []
    # Namecheap XML uses a namespace
    for ns in ["", "{http://api.namecheap.com/xml.response}"]:
        for host in root.iter(f"{ns}host"):
            records.append({
                "name": host.attrib.get("Name", ""),
                "type": host.attrib.get("Type", ""),
                "address": host.attrib.get("Address", ""),
                "ttl": host.attrib.get("TTL", "1800"),
                "mxpref": host.attrib.get("MXPref", "10"),
            })

    return records


def _set_hosts(domain: str, records: list[dict]) -> bool:
    """Set ALL host records for a domain. Replaces everything."""
    sld, tld = _split_domain(domain)
    params = {"SLD": sld, "TLD": tld}

    for i, rec in enumerate(records, 1):
        params[f"HostName{i}"] = rec["name"]
        params[f"RecordType{i}"] = rec["type"]
        params[f"Address{i}"] = rec["address"]
        params[f"TTL{i}"] = rec.get("ttl", "1800")
        if rec["type"] == "MX":
            params[f"MXPref{i}"] = rec.get("mxpref", "10")

    _api_call("namecheap.domains.dns.setHosts", params)
    return True


def _has_record(records: list[dict], subdomain: str, record_type: str, address: str) -> bool:
    return any(
        r["name"] == subdomain and r["type"] == record_type and r["address"] == address
        for r in records
    )


def add_record(subdomain: str, domain: str, record_type: str = "A",
               address: str = None, ttl: str = "1800") -> tuple[bool, str]:
    """Add a DNS record, preserving all existing records.

    If address is None, uses current public IP (for A records).
    Verifies the record is present after writing; retries on clobber.
    Returns (success, message).
    """
    if address is None:
        address = _get_public_ip()

    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            existing = get_records(domain)
        except Exception as e:
            return False, f"Failed to get existing records: {e}"

        action = None
        for rec in existing:
            if rec["name"] == subdomain and rec["type"] == record_type:
                if rec["address"] == address:
                    return True, f"{subdomain}.{domain} already exists with correct address"
                rec["address"] = address
                rec["ttl"] = ttl
                action = "updated"
                break

        if action is None:
            existing.append({
                "name": subdomain,
                "type": record_type,
                "address": address,
                "ttl": ttl,
            })
            action = "added"

        try:
            _set_hosts(domain, existing)
        except Exception as e:
            last_err = f"setHosts failed: {e}"
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_S)
            continue

        try:
            current = get_records(domain)
        except Exception as e:
            last_err = f"post-write verify read failed: {e}"
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_S)
            continue

        if _has_record(current, subdomain, record_type, address):
            audit("system:dns", f"record.{action}", f"{subdomain}.{domain}",
                  {"type": record_type, "address": address,
                   "total_records": len(current), "attempt": attempt})
            suffix = f" (attempt {attempt})" if attempt > 1 else ""
            return True, f"{action.capitalize()} {subdomain}.{domain} → {address} ({len(current)} total records){suffix}"

        audit("system:dns", "record.clobbered", f"{subdomain}.{domain}",
              {"type": record_type, "address": address, "attempt": attempt})
        last_err = "post-write verify: record absent (concurrent writer clobbered)"
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_DELAY_S)

    return False, f"Failed to add {subdomain}.{domain} after {_MAX_RETRIES} attempts: {last_err}"


def remove_record(subdomain: str, domain: str, record_type: str = "A") -> tuple[bool, str]:
    """Remove a DNS record, preserving all other records.

    Verifies the record is absent after writing; retries on clobber-revival
    (where a concurrent writer's stale snapshot brought our removed record
    back). Returns (success, message).
    """
    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            existing = get_records(domain)
        except Exception as e:
            return False, f"Failed to get existing records: {e}"

        filtered = [r for r in existing
                    if not (r["name"] == subdomain and r["type"] == record_type)]

        if len(filtered) == len(existing):
            return True, f"{subdomain}.{domain} not found (already removed)"

        try:
            _set_hosts(domain, filtered)
        except Exception as e:
            last_err = f"setHosts failed: {e}"
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_S)
            continue

        try:
            current = get_records(domain)
        except Exception as e:
            last_err = f"post-write verify read failed: {e}"
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY_S)
            continue

        still_present = any(
            r["name"] == subdomain and r["type"] == record_type for r in current
        )
        if not still_present:
            audit("system:dns", "record.removed", f"{subdomain}.{domain}",
                  {"type": record_type, "remaining_records": len(current),
                   "attempt": attempt})
            suffix = f" (attempt {attempt})" if attempt > 1 else ""
            return True, f"Removed {subdomain}.{domain} ({len(current)} records remaining){suffix}"

        audit("system:dns", "record.remove_clobbered", f"{subdomain}.{domain}",
              {"type": record_type, "attempt": attempt})
        last_err = "post-write verify: record still present (concurrent writer revived)"
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_DELAY_S)

    return False, f"Failed to remove {subdomain}.{domain} after {_MAX_RETRIES} attempts: {last_err}"
