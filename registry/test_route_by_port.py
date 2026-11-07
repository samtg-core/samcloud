"""Services under a bare address are reached by port (#1010).

With no SAMCLOUD_DOMAIN and an IP address in PUBLIC_URL, the service domain is
that address, which can't carry subdomains. Each route gets its own port from
the `route` range instead, and Caddy serves it plain HTTP on that port.
"""
from unittest.mock import patch

import caddy
import ports as port_alloc
from test_registry import client, _register_device, _register_service  # noqa: F401

ADDR = "192.0.2.10"


def _caddyfile(tmp_path, monkeypatch):
    path = tmp_path / "Caddyfile"
    path.write_text("{\n    admin 0.0.0.0:2019\n}\n")
    monkeypatch.setenv("CADDYFILE_PATH", str(path))
    return path


def test_an_address_routes_by_port_and_a_name_does_not():
    assert caddy.by_port("192.0.2.10") and caddy.by_port("[2001:db8::1]")
    assert not caddy.by_port("example.org") and not caddy.by_port("cloud.example.org")


def test_a_service_under_the_address_gets_its_own_port(client, tmp_path, monkeypatch):
    path = _caddyfile(tmp_path, monkeypatch)
    monkeypatch.setenv("PUBLIC_URL", f"http://{ADDR}:7778")
    monkeypatch.delenv("SAMCLOUD_DOMAIN", raising=False)
    _register_device(client, "d1", "host1", ip="192.0.2.50")
    lo, _ = port_alloc.RANGES["route"]
    with patch.object(port_alloc, "_port_in_use", return_value=False), \
         patch.object(caddy, "reload_caddy", return_value=(True, "ok")):
        a = _register_service(client, "web", "d1", 8080, subdomain="web")
        b = _register_service(client, "api", "d1", 9090, subdomain="api")
    assert a.status_code == 200, a.text
    assert a.json()["external_url"] == f"http://{ADDR}:{lo}"
    assert b.json()["external_url"] == f"http://{ADDR}:{lo + 1}"
    text = path.read_text()
    assert f"# web.{ADDR} (registry-managed)\nhttp://:{lo} {{\n    reverse_proxy http://192.0.2.50:8080\n}}" in text
    assert f"web.{ADDR} {{" not in text            # no site named under the address

    with patch.object(caddy, "reload_caddy", return_value=(True, "ok")):
        r = client.delete("/api/v1/services/d1/web")
    assert r.status_code == 200, r.text
    assert f"http://:{lo} {{" not in path.read_text() and f"http://:{lo + 1} {{" in path.read_text()
    assert port_alloc.owned("route", f"route:web.{ADDR}") is None

    with patch.object(port_alloc, "_port_in_use", return_value=False), \
         patch.object(caddy, "reload_caddy", return_value=(True, "ok")):
        c = _register_service(client, "web2", "d1", 8081, subdomain="web2")
    assert c.json()["external_url"] == f"http://{ADDR}:{lo}"   # the freed port is reused


def test_a_service_under_a_domain_is_unchanged(client, tmp_path, monkeypatch):
    path = _caddyfile(tmp_path, monkeypatch)
    _register_device(client, "d1", "host1", ip="192.0.2.50")
    with patch.object(port_alloc, "_port_in_use", return_value=False), \
         patch.object(caddy, "reload_caddy", return_value=(True, "ok")):
        r = _register_service(client, "web", "d1", 8080, subdomain="web", domain="example.org")
    assert r.json()["external_url"].startswith("https://web.example.org")
    assert "web.example.org {" in path.read_text()
    assert port_alloc.list_all("route") == []
