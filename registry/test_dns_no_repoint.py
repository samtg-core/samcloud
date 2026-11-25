"""Registering a service never takes over a DNS name that points elsewhere (#1157, #44 review).

dns.add_record used to repoint an existing A record of the same name to the
plane, and registration ran the DNS step even when Caddy had refused the block.
A device manager could take any name in the zone that wasn't a registry route.
"""
import pytest

from test_registry import client  # noqa: F401  (the registry test client fixture)

PLANE = "203.0.113.7"


@pytest.fixture
def zone(monkeypatch):
    import dns
    records = [
        {"name": "cloud", "type": "A", "address": PLANE, "ttl": "1800"},
        {"name": "mail", "type": "A", "address": "198.51.100.9", "ttl": "1800"},
        {"name": "docs", "type": "CNAME", "address": "elsewhere.example.", "ttl": "1800"},
    ]
    writes = []
    monkeypatch.setattr(dns, "_get_public_ip", lambda: PLANE)
    monkeypatch.setattr(dns, "get_records", lambda domain: [dict(r) for r in records])

    def set_hosts(domain, new):
        writes.append([dict(r) for r in new])
        records[:] = [dict(r) for r in new]
        return True

    monkeypatch.setattr(dns, "_set_hosts", set_hosts)
    monkeypatch.setattr(dns, "audit", lambda *a, **k: None)
    return records, writes


@pytest.mark.parametrize("name", ["mail", "MAIL", "docs"])
def test_a_name_that_points_elsewhere_is_left_alone(zone, name):
    import dns
    records, writes = zone
    ok, msg = dns.add_record(name, "example.org")
    assert ok is False and "Only the admin repoints" in msg
    assert writes == []


def test_the_admin_may_repoint(zone):
    import dns
    records, writes = zone
    ok, msg = dns.add_record("mail", "example.org", repoint=True)
    assert ok is True
    assert {"name": "mail", "type": "A", "address": PLANE} in [
        {k: r[k] for k in ("name", "type", "address")} for r in records]


def test_a_new_name_and_one_already_at_the_plane_are_fine(zone):
    import dns
    records, writes = zone
    assert dns.add_record("new-svc", "example.org")[0] is True
    assert len(writes) == 1
    ok, msg = dns.add_record("cloud", "example.org")
    assert ok is True and "already exists" in msg
    assert len(writes) == 1


def test_no_dns_when_caddy_refused_the_route(monkeypatch):
    import main
    called = []
    monkeypatch.setattr(main, "_dns_module_on", lambda: True)
    monkeypatch.setattr(main.caddy, "by_port", lambda domain: False)
    import dns
    monkeypatch.setattr(dns, "add_record", lambda *a, **k: called.append((a, k)) or (True, "added"))
    r = main._dns_record("add", "svc", "example.org", caddy_status={"success": False, "message": "bad host"})
    assert r["skipped"] is True and "Caddy refused" in r["message"]
    assert called == []
    r = main._dns_record("add", "svc", "example.org", caddy_status={"success": True}, repoint=False)
    assert r["success"] is True
    assert called == [(("svc", "example.org"), {"repoint": False})]


def test_a_device_token_cannot_register_a_claimed_subdomain(client):
    """It used to store the service with the other's URL and drop the route silently."""
    from unittest.mock import patch
    import enrollment
    import ports as port_alloc
    from test_registry import _register_device, _register_service
    with patch.object(port_alloc, "_port_in_use", return_value=False):
        _register_device(client, "owner-host", "owner-host")
        _register_device(client, "tok-host2", "tok-host2")
        assert _register_service(client, "models", "owner-host", 8800,
                                 subdomain="taken-sub").status_code in (200, 201)
        tok = enrollment.generate_device_token("tok-host2")
        r = client.post("/api/v1/services/register", headers={"Authorization": f"Bearer {tok}"},
                        json={"name": "shadow", "port": 9201, "subdomain": "taken-sub"})
        assert r.status_code == 409, r.text
        assert client.get("/api/v1/services/tok-host2%2Fshadow").status_code == 404


def test_remove_takes_only_the_planes_record(zone):
    import dns
    records, writes = zone
    ok, msg = dns.remove_record("mail", "example.org")
    assert ok is True and "left alone" in msg and writes == []
    assert any(r["name"] == "mail" for r in records)
    ok, msg = dns.remove_record("cloud", "example.org")
    assert ok is True and msg.startswith("Removed") and not any(r["name"] == "cloud" for r in records)


@pytest.fixture
def dns_on(zone, monkeypatch):
    import main
    monkeypatch.setattr(main, "_dns_module_on", lambda: True)
    monkeypatch.setattr(main.caddy, "by_port", lambda domain: False)
    return zone


def test_a_registration_on_a_name_pointing_elsewhere_stores_nothing(client, dns_on):
    from unittest.mock import patch
    import db
    import main
    import ports as port_alloc
    from fastapi.testclient import TestClient
    from test_registry import _register_device, _invite_and_activate, AuthedClient
    records, writes = dns_on
    with patch.object(port_alloc, "_port_in_use", return_value=False):
        _register_device(client, "grab-host", "grab-host")
        with TestClient(main.app) as raw:
            token, _ = _invite_and_activate(client, raw, "grab-agent", ["device:grab-host"])
            mgr = AuthedClient(raw, token)
            r = mgr.post("/api/v1/services", json={
                "name": "grab", "device_id": "grab-host", "port": 8301, "description": "t",
                "subdomain": "mail", "domain": main._service_domain()})
            assert r.status_code == 409 and "already points at A 198.51.100.9" in r.text, r.text
    assert db.query_one("SELECT id FROM services WHERE id = 'grab-host/grab'") is None
    assert db.query_one("SELECT subdomain FROM routes WHERE subdomain = 'mail'") is None
    assert writes == []


def test_an_unreadable_zone_refuses_the_registration(client, dns_on, monkeypatch):
    import dns
    import main
    import enrollment
    from unittest.mock import patch
    import ports as port_alloc
    from test_registry import _register_device

    def down(domain):
        raise RuntimeError("namecheap timeout")
    monkeypatch.setattr(dns, "get_records", down)
    with patch.object(port_alloc, "_port_in_use", return_value=False):
        _register_device(client, "down-host", "down-host")
        tok = enrollment.generate_device_token("down-host")
        r = client.post("/api/v1/services/register", headers={"Authorization": f"Bearer {tok}"},
                        json={"name": "x", "port": 9301, "subdomain": "fresh-name"})
    assert r.status_code == 503 and "nothing was registered" in r.text, r.text


def test_deleting_a_service_leaves_a_name_that_points_elsewhere(client, zone, monkeypatch):
    """Registered while the name was free (DNS off here), then the name went to
    another host: deleting the service must not take that host's record down."""
    import main
    from unittest.mock import patch
    import ports as port_alloc
    from test_registry import _register_device, _register_service
    records, writes = zone
    monkeypatch.setattr(main.caddy, "append_route", lambda *a, **k: (True, "added"))
    monkeypatch.setattr(main.caddy, "remove_route", lambda *a, **k: (True, "removed"))
    monkeypatch.setattr(main, "_dns_module_on", lambda: False)
    with patch.object(port_alloc, "_port_in_use", return_value=False):
        _register_device(client, "old-host", "old-host")
        assert _register_service(client, "old", "old-host", 8401, subdomain="mail").status_code in (200, 201)
    monkeypatch.setattr(main, "_dns_module_on", lambda: True)
    monkeypatch.setattr(main.caddy, "by_port", lambda domain: False)
    r = client.delete("/api/v1/services/old-host%2Fold")
    assert r.status_code == 200, r.text
    assert "left alone" in r.json()["route"]["dns"]["message"]
    assert any(rec["name"] == "mail" and rec["address"] == "198.51.100.9" for rec in records)
    assert writes == []
