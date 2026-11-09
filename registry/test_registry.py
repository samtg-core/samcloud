"""Comprehensive test suite for the samcloud Registry API.

Uses FastAPI TestClient for HTTP-level tests. All tests run against a
temporary SQLite database — production DB is never touched.
"""

import errno
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import conftest
import db
import ports as port_alloc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_app():
    """Import and return the FastAPI app (after DB_PATH is patched)."""
    # Re-import to pick up patched db module
    import main
    return main.app


def _get_admin_token(client):
    """Create admin user and return auth headers."""
    resp = client.post("/api/v1/init", json={"username": "admin", "password": "test"})
    if resp.status_code == 409:
        # Already initialized — login instead
        resp = client.post("/api/v1/auth/login", json={"username": "admin", "password": "test"})
    token = resp.json().get("token", "")
    return {"Authorization": f"Bearer {token}"}


def _register_device(client, device_id="test-device", hostname="testhost",
                     context="samcloud", **kwargs):
    """Helper to register a device. Accepts both old and new schema fields."""
    payload = {
        "id": device_id,
        "hostname": hostname,
        "context": context,
        **kwargs,
    }
    # Map old 'ip' to 'outbound_ip' for compat
    if "ip" in payload and "outbound_ip" not in payload:
        payload["outbound_ip"] = payload.pop("ip")
    return client.post("/api/v1/devices", json=payload)


def _register_service(client, name="test-svc", device_id="test-device",
                      port=8080, description="Test service", **kwargs):
    """Helper to register a service."""
    payload = {
        "name": name,
        "device_id": device_id,
        "port": port,
        "description": description,
        **kwargs,
    }
    return client.post("/api/v1/services", json=payload)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _session_fp_expected(nonce):
    """What the wire should carry for a session nonce (#264): an 8-char sha256
    prefix, never the nonce itself."""
    import hashlib
    return hashlib.sha256(nonce.encode()).hexdigest()[:8]


class AuthedClient:
    """Wraps TestClient to auto-inject auth headers."""
    def __init__(self, client, token):
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _merge_headers(self, kwargs):
        headers = {**self._headers, **(kwargs.pop("headers", {}) or {})}
        kwargs["headers"] = headers
        return kwargs

    def get(self, *args, **kwargs): return self._client.get(*args, **self._merge_headers(kwargs))
    def post(self, *args, **kwargs): return self._client.post(*args, **self._merge_headers(kwargs))
    def put(self, *args, **kwargs): return self._client.put(*args, **self._merge_headers(kwargs))
    def patch(self, *args, **kwargs): return self._client.patch(*args, **self._merge_headers(kwargs))
    def delete(self, *args, **kwargs): return self._client.delete(*args, **self._merge_headers(kwargs))


@pytest.fixture()
def client(tmp_db):
    """FastAPI TestClient with admin auth — tests use real authentication."""
    app = _get_app()
    with TestClient(app) as c:
        resp = c.post("/api/v1/init", json={"username": "admin", "password": "test"})
        token = resp.json().get("token", "")
        yield AuthedClient(c, token)


@pytest.fixture()
def seeded_client(seeded_db):
    """FastAPI TestClient with admin auth wired to seeded DB."""
    app = _get_app()
    with TestClient(app) as c:
        resp = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = resp.json().get("token", "")
        yield AuthedClient(c, token)


# ===========================================================================
# Device CRUD
# ===========================================================================

class TestDeviceRegistration:
    def test_register_device(self, client):
        r = _register_device(client, "box1", "box1-host", "samcloud",
                             ip="192.0.2.45", ssh_user="you")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "registered"
        assert body["device_id"] == "box1"

    def test_register_duplicate_device_rejected(self, client):
        _register_device(client, "box1", "box1-host")
        r = _register_device(client, "box1", "other-host")
        assert r.status_code == 409

    def test_list_devices_empty(self, client):
        r = client.get("/api/v1/devices")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_devices(self, client):
        _register_device(client, "d1", "host1")
        _register_device(client, "d2", "host2")
        r = client.get("/api/v1/devices")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_list_devices_filter_context(self, client):
        _register_device(client, "d1", "host1", context="samcloud")
        _register_device(client, "d2", "host2", context="tunnel")
        r = client.get("/api/v1/devices?context=tunnel")
        devices = r.json()
        assert len(devices) == 1
        assert devices[0]["id"] == "d2"

    def test_workcloud_is_the_old_name_for_tunnel(self, client):
        """`workcloud` was the early label for a reverse-tunnel device (#1010).
        It is still accepted, stored as `tunnel`, and still filters."""
        _register_device(client, "d2", "host2", context="workcloud")
        assert client.get("/api/v1/devices/d2").json()["context"] == "tunnel"
        assert [d["id"] for d in client.get("/api/v1/devices?context=workcloud").json()] == ["d2"]

    def test_existing_workcloud_rows_become_tunnel(self, client):
        _register_device(client, "d2", "host2", context="samcloud")
        _register_device(client, "d3", "host3", context="samcloud", outbound_method="direct-ssh")
        db.execute("UPDATE devices SET context = 'workcloud', outbound_method = NULL WHERE id = 'd2'")
        db.execute("UPDATE devices SET context = 'workcloud' WHERE id = 'd3'")
        with db.transaction() as conn:
            db._migrate(conn)
        d2, d3 = (client.get(f"/api/v1/devices/{i}").json() for i in ("d2", "d3"))
        assert (d2["context"], d2["outbound_method"]) == ("tunnel", "reverse-tunnel")
        assert (d3["context"], d3["outbound_method"]) == ("tunnel", "direct-ssh")   # an explicit method stays

    def test_a_legacy_workcloud_register_still_gets_its_forward_port(self, client):
        """codex, #1010: the old label with no method allocated a tunnel AND,
        through the label, a service forward port. The label now implies the method."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = _register_device(client, "legacy-box", "legacy", context="workcloud", ssh_user="you")
            assert r.json()["tunnel_port"] is not None
            assert client.get("/api/v1/devices/legacy-box").json()["outbound_method"] == "reverse-tunnel"
            assert _register_service(client, "api", "legacy-box", 9000).json()["slice_port"] is not None
            # and through the device's own token, the path codex reproduced on
            import enrollment
            tok = enrollment.generate_device_token("legacy-box")
            r = client.post("/api/v1/services/register", json={"name": "web", "port": 9001},
                            headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code == 200 and r.json()["slice_port"] is not None, r.text
            _register_device(client, "legacy-ds", "ds", context="workcloud", outbound_method="direct-ssh")
            assert client.get("/api/v1/devices/legacy-ds").json()["outbound_method"] == "direct-ssh"

    def test_get_device(self, client):
        _register_device(client, "box1", "box1-host", outbound_ip="192.0.2.45")
        r = client.get("/api/v1/devices/box1")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "box1"
        assert body["outbound_ip"] == "192.0.2.45"
        assert "services" in body

    def test_get_device_not_found(self, client):
        r = client.get("/api/v1/devices/nonexistent")
        assert r.status_code == 404

    def test_get_device_includes_services(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "svc-a", "d1", 8080)
            _register_service(client, "svc-b", "d1", 8081)
        r = client.get("/api/v1/devices/d1")
        assert len(r.json()["services"]) == 2


class TestDeviceAutoTunnelPort:
    """Tunnel devices get auto-allocated tunnel ports."""

    def test_tunnel_device_gets_tunnel_port(self, client):
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = _register_device(client, "work-new", "WORK-NEW", "tunnel")
        body = r.json()
        assert body["tunnel_port"] is not None
        # Should be in tunnel range
        lo, hi = port_alloc.RANGES["tunnel"]
        assert lo <= body["tunnel_port"] <= hi

    def test_samcloud_device_no_auto_tunnel(self, client):
        r = _register_device(client, "new-alice", "host", "samcloud")
        assert r.json()["tunnel_port"] is None


class TestEnrollNamesTheTunnelEndpoint:
    """#1010: the console's manual-tunnel command used a fixed host and port.
    The enroll answer now carries this plane's endpoint, as bootstrap fills it in."""

    def test_reverse_tunnel_enroll_names_host_port_and_user(self, client, monkeypatch):
        monkeypatch.setenv("PLANE_HOST", "gw.example.org")
        monkeypatch.setenv("PLANE_SSH_PORT", "2200")
        monkeypatch.delenv("PLANE_SSH_USER", raising=False)
        monkeypatch.delenv("SLICE_SSH_USER", raising=False)
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = client.post("/api/v1/enroll", json={
                "device_id": "tun-dev", "access_target": "reverse-tunnel", "target_ssh_user": "you"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tunnel_port"]
        assert body["tunnel"] == {"host": "gw.example.org", "port": 2200, "user": "samcloud"}

    def test_direct_ssh_enroll_has_no_tunnel(self, client):
        r = client.post("/api/v1/enroll", json={
            "device_id": "direct-dev", "access_target": "direct-ssh",
            "target_ip": "198.51.100.9", "target_ssh_user": "you"})
        assert r.status_code == 200, r.text
        assert "tunnel" not in r.json()


# ===========================================================================
# Service Registration
# ===========================================================================

class TestServiceRegistration:
    def test_register_service(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = _register_service(client, "web", "d1", 8080)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "registered"
        assert body["service_id"] == "d1/web"

    def test_register_service_device_not_found(self, client):
        r = _register_service(client, "web", "nonexistent", 8080)
        assert r.status_code == 404

    def test_register_duplicate_service_rejected(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080)
            r = _register_service(client, "web", "d1", 8081)
        assert r.status_code == 409

    def test_service_auto_port_for_tunnel(self, client):
        """Reverse-tunnel devices get an auto-allocated forward port."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_device(client, "wk1", "WK1", "tunnel",
                             outbound_method="reverse-tunnel")
            r = _register_service(client, "api", "wk1", 9000)
        body = r.json()
        assert body["slice_port"] is not None
        lo, hi = port_alloc.RANGES["forward"]
        assert lo <= body["slice_port"] <= hi

    def test_the_forward_port_follows_the_tunnel_not_the_label(self, client):
        """The port keys on outbound_method, as the slot path does; it used to
        key on the `workcloud` label (#1010)."""
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_device(client, "rt", "RT", "samcloud", outbound_method="reverse-tunnel")
            _register_device(client, "ds", "DS", "samcloud", outbound_method="direct-ssh")
            assert _register_service(client, "a", "rt", 9000).json()["slice_port"] is not None
            assert _register_service(client, "b", "ds", 9001).json()["slice_port"] is None

    def test_service_explicit_slice_port(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = _register_service(client, "web", "d1", 8080, slice_port=18099)
        assert r.json()["slice_port"] == 18099

    def test_service_with_subdomain_creates_route(self, client):
        _register_device(client, "d1", "host1", ip="192.0.2.50")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080,
                              subdomain="test", domain="example.org")
        body = client.get("/api/v1/routes").json()
        assert any(r["subdomain"] == "test" for r in body)

    def test_service_external_url(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = _register_service(client, "web", "d1", 8080,
                                  subdomain="mytest", domain="example.org")
        assert r.json()["external_url"] == "https://mytest.example.org"

    def test_list_services_empty(self, client):
        r = client.get("/api/v1/services")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_services_filter_device(self, client):
        _register_device(client, "d1", "host1")
        _register_device(client, "d2", "host2")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080)
            _register_service(client, "api", "d2", 9090)
        r = client.get("/api/v1/services?device_id=d1")
        svcs = r.json()
        assert len(svcs) == 1
        assert svcs[0]["device_id"] == "d1"

    def test_get_service(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080)
        r = client.get("/api/v1/services/d1/web")
        assert r.status_code == 200
        assert r.json()["name"] == "web"

    def test_get_service_not_found(self, client):
        r = client.get("/api/v1/services/no/such")
        assert r.status_code == 404


# ===========================================================================
# Port Allocator (via API)
# ===========================================================================

class TestPortAPI:
    def test_allocate_port_via_api(self, client):
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = client.post("/api/v1/ports/allocate", json={
                "port_type": "forward",
                "owner_id": "test/svc",
            })
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "forward"
        assert body["owner"] == "test/svc"
        lo, hi = port_alloc.RANGES["forward"]
        assert lo <= body["port"] <= hi

    def test_allocate_port_preferred_via_api(self, client):
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            r = client.post("/api/v1/ports/allocate", json={
                "port_type": "tunnel",
                "owner_id": "test-dev",
                "preferred": 10005,
            })
        assert r.status_code == 200
        assert r.json()["port"] == 10005

    def test_allocate_port_collision_via_api(self, client):
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            client.post("/api/v1/ports/allocate", json={
                "port_type": "tunnel",
                "owner_id": "owner-a",
                "preferred": 10005,
            })
            r = client.post("/api/v1/ports/allocate", json={
                "port_type": "tunnel",
                "owner_id": "owner-b",
                "preferred": 10005,
            })
        assert r.status_code == 409

    def test_allocate_invalid_type_via_api(self, client):
        r = client.post("/api/v1/ports/allocate", json={
            "port_type": "invalid",
            "owner_id": "test",
        })
        assert r.status_code == 409

    def test_list_ports(self, seeded_client):
        r = seeded_client.get("/api/v1/ports")
        assert r.status_code == 200
        ports = r.json()
        assert len(ports) > 0

    def test_list_ports_filter(self, seeded_client):
        r = seeded_client.get("/api/v1/ports?type=tunnel")
        assert r.status_code == 200
        assert all(p["type"] == "tunnel" for p in r.json())

    def test_check_port(self, client):
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            client.post("/api/v1/ports/allocate", json={
                "port_type": "forward",
                "owner_id": "test/svc",
                "preferred": 18050,
            })
            r = client.get("/api/v1/ports/check/18050")
        assert r.status_code == 200
        body = r.json()
        assert body["port"] == 18050
        assert len(body["allocated"]) == 1
        assert body["in_use_lsof"] is False


# ===========================================================================
# Tickets
# ===========================================================================

class TestTickets:
    def test_create_ticket(self, client):
        r = client.post("/api/v1/tickets", json={
            "type": "bug",
            "priority": "high",
            "summary": "Media server offline",
            "body": "Cannot connect to the media server since 3pm.",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "created"
        assert body["ticket_id"] > 0

    def test_get_ticket(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Test ticket",
        })
        tid = create.json()["ticket_id"]

        r = client.get(f"/api/v1/tickets/{tid}")
        assert r.status_code == 200
        body = r.json()
        assert body["summary"] == "Test ticket"
        assert body["status"] == "open"
        assert "comments" in body
        assert body["comments"] == []

    def test_get_ticket_not_found(self, client):
        r = client.get("/api/v1/tickets/99999")
        assert r.status_code == 404

    def test_list_tickets_empty(self, client):
        r = client.get("/api/v1/tickets")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_tickets(self, client):
        client.post("/api/v1/tickets", json={
            "summary": "Ticket 1",
        })
        client.post("/api/v1/tickets", json={
            "summary": "Ticket 2",
        })
        r = client.get("/api/v1/tickets")
        assert len(r.json()) == 2

    def test_list_tickets_filter_status(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Open ticket",
        })
        tid = create.json()["ticket_id"]
        # Resolve it
        client.patch(f"/api/v1/tickets/{tid}", json={"status": "resolved"})

        client.post("/api/v1/tickets", json={
            "summary": "Still open",
        })

        open_tickets = client.get("/api/v1/tickets?status=open").json()
        assert len(open_tickets) == 1
        assert open_tickets[0]["summary"] == "Still open"

    def test_update_ticket_status(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Fix bug",
        })
        tid = create.json()["ticket_id"]

        r = client.patch(f"/api/v1/tickets/{tid}", json={
            "status": "in_progress",
        })
        assert r.status_code == 200

        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert ticket["status"] == "in_progress"

    def test_update_ticket_resolved_sets_timestamp(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Resolve me",
        })
        tid = create.json()["ticket_id"]

        client.patch(f"/api/v1/tickets/{tid}", json={"status": "resolved"})
        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert ticket["resolved_at"] is not None
        # A reopen clears it: the field says the ticket IS resolved (staging
        # #100 read status open with resolved_at still set).
        client.patch(f"/api/v1/tickets/{tid}", json={"status": "open"})
        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert ticket["status"] == "open" and ticket["resolved_at"] is None

    def test_update_ticket_assignee(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Assign me",
        })
        tid = create.json()["ticket_id"]

        # assignee must be a valid username; admin always exists in the test fixture.
        client.patch(f"/api/v1/tickets/{tid}", json={"assignee": "admin"})
        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert ticket["assignee"] == "admin"

    def test_update_ticket_unknown_assignee_400(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Bad assignee",
        })
        tid = create.json()["ticket_id"]

        r = client.patch(f"/api/v1/tickets/{tid}",
                         json={"assignee": "this-user-does-not-exist"})
        assert r.status_code == 400
        assert "Unknown assignee" in r.json()["detail"]

    def test_create_ticket_unknown_service_id_400(self, client):
        r = client.post("/api/v1/tickets", json={
            "summary": "Bad svc",
            "service_id": "ghost-device/ghost-service",
        })
        assert r.status_code == 400
        assert "Unknown service_id" in r.json()["detail"]

    def test_list_tickets_assignee_filter(self, client):
        """?assignee= narrows the list to tickets assigned to that username."""
        c1 = client.post("/api/v1/tickets",
                         json={"summary": "a"}).json()["ticket_id"]
        c2 = client.post("/api/v1/tickets",
                         json={"summary": "b"}).json()["ticket_id"]
        client.patch(f"/api/v1/tickets/{c1}", json={"assignee": "admin"})
        # c2 stays unassigned

        filtered = client.get("/api/v1/tickets?assignee=admin").json()
        ids = {t["id"] for t in filtered}
        assert c1 in ids
        assert c2 not in ids

    def test_update_ticket_priority(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Escalate", "priority": "low",
        })
        tid = create.json()["ticket_id"]

        # v0.7.84: priorities normalize to the canonical trio — known aliases
        # fold ("critical"→"high"), unknown values 422 instead of persisting
        # as unfilterable variants.
        client.patch(f"/api/v1/tickets/{tid}", json={"priority": "critical"})
        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert ticket["priority"] == "high"

        r = client.patch(f"/api/v1/tickets/{tid}", json={"priority": "sideways"})
        assert r.status_code == 422

        r = client.post("/api/v1/tickets", json={
            "summary": "Alias on create", "priority": "med",
        })
        assert r.json().get("id") == r.json().get("ticket_id")   # #482 canonical id
        assert client.get(f"/api/v1/tickets/{r.json()['id']}").json()["priority"] == "medium"

    def test_update_ticket_not_found(self, client):
        r = client.patch("/api/v1/tickets/99999", json={"status": "closed"})
        assert r.status_code == 404

    def test_add_comment(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Comment test",
        })
        tid = create.json()["ticket_id"]

        r = client.post(f"/api/v1/tickets/{tid}/comments", json={
            "body": "Looks like a DNS issue.",
        })
        assert r.status_code == 200
        assert r.json()["comment_id"] > 0

    def test_add_comment_appears_in_get(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "With comments",
        })
        tid = create.json()["ticket_id"]

        client.post(f"/api/v1/tickets/{tid}/comments", json={
            "body": "First comment",
        })
        client.post(f"/api/v1/tickets/{tid}/comments", json={
            "body": "Second comment",
        })

        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert len(ticket["comments"]) == 2
        assert ticket["comments"][0]["body"] == "First comment"
        # author is bearer-derived (admin in test fixture), not body-supplied.
        assert ticket["comments"][1]["author"] == "admin"

    def test_add_comment_ticket_not_found(self, client):
        r = client.post("/api/v1/tickets/99999/comments", json={
            "body": "Ghost comment",
        })
        assert r.status_code == 404

    def test_ticket_with_service_and_device(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080)

        r = client.post("/api/v1/tickets", json={
            "type": "incident",
            "priority": "critical",
            "summary": "Service unhealthy",
            "service_id": "d1/web",
            "device_id": "d1",
        })
        assert r.status_code == 200
        tid = r.json()["ticket_id"]
        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert ticket["service_id"] == "d1/web"
        assert ticket["device_id"] == "d1"
        assert ticket["type"] == "incident"

    # ----- ticket #23: body aliases -----

    def test_body_alias_description(self, client):
        """Clients sending `description` should have it stored as body."""
        r = client.post("/api/v1/tickets", json={
            "summary": "alias test",
            "description": "this should land in body",
        })
        assert r.status_code == 200, r.text
        tid = r.json()["ticket_id"]
        ticket = client.get(f"/api/v1/tickets/{tid}").json()
        assert ticket["body"] == "this should land in body"

    def test_body_alias_content(self, client):
        r = client.post("/api/v1/tickets", json={
            "summary": "content alias",
            "content": "stored as body",
        })
        assert r.status_code == 200, r.text
        ticket = client.get(f"/api/v1/tickets/{r.json()['ticket_id']}").json()
        assert ticket["body"] == "stored as body"

    def test_body_alias_message(self, client):
        r = client.post("/api/v1/tickets", json={
            "summary": "message alias",
            "message": "stored as body",
        })
        assert r.status_code == 200, r.text
        ticket = client.get(f"/api/v1/tickets/{r.json()['ticket_id']}").json()
        assert ticket["body"] == "stored as body"

    def test_explicit_body_wins_over_alias(self, client):
        """If both body and an alias are sent, body is authoritative."""
        r = client.post("/api/v1/tickets", json={
            "summary": "both fields",
            "body": "explicit body",
            "description": "alias description",
        })
        assert r.status_code == 200, r.text
        ticket = client.get(f"/api/v1/tickets/{r.json()['ticket_id']}").json()
        assert ticket["body"] == "explicit body"

    def test_empty_body_falls_through_to_alias(self, client):
        """body='' (whitespace-only) should fall through to alias, not stay empty."""
        r = client.post("/api/v1/tickets", json={
            "summary": "empty body",
            "body": "   ",
            "description": "real content here",
        })
        assert r.status_code == 200, r.text
        ticket = client.get(f"/api/v1/tickets/{r.json()['ticket_id']}").json()
        assert ticket["body"] == "real content here"

    def test_no_body_no_alias_creates_empty_ticket(self, client):
        """Existing semantic preserved — body=null is allowed."""
        r = client.post("/api/v1/tickets", json={
            "summary": "summary only",
        })
        assert r.status_code == 200, r.text
        ticket = client.get(f"/api/v1/tickets/{r.json()['ticket_id']}").json()
        assert ticket["body"] is None


# ===========================================================================
# Routes
# ===========================================================================

class TestRoutes:
    def test_create_route(self, client):
        r = client.post("/api/v1/routes", json={
            "subdomain": "test",
            "domain": "example.org",
            "target": "localhost:9999",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "created"
        assert body["route"] == "test.example.org"

    def test_create_route_duplicate_rejected(self, client):
        client.post("/api/v1/routes", json={
            "subdomain": "test",
            "domain": "example.org",
            "target": "localhost:9999",
        })
        r = client.post("/api/v1/routes", json={
            "subdomain": "test",
            "domain": "example.org",
            "target": "localhost:8888",
        })
        assert r.status_code == 409

    def test_same_subdomain_different_domain_ok(self, client):
        r1 = client.post("/api/v1/routes", json={
            "subdomain": "test",
            "domain": "example.org",
            "target": "localhost:9999",
        })
        r2 = client.post("/api/v1/routes", json={
            "subdomain": "test",
            "domain": "example.com",
            "target": "localhost:8888",
        })
        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_list_routes_empty(self, client):
        r = client.get("/api/v1/routes")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_routes(self, client):
        client.post("/api/v1/routes", json={
            "subdomain": "a", "target": "localhost:1",
        })
        client.post("/api/v1/routes", json={
            "subdomain": "b", "target": "localhost:2",
        })
        r = client.get("/api/v1/routes")
        assert len(r.json()) == 2

    def test_list_routes_seeded(self, seeded_client):
        r = seeded_client.get("/api/v1/routes")
        routes = r.json()
        assert len(routes) > 10
        subdomains = {r["subdomain"] for r in routes}
        assert "hme" in subdomains
        assert "jly" in subdomains
        assert "spl" in subdomains


# ===========================================================================
# Audit Log
# ===========================================================================

class TestAuditLog:
    def test_limit_is_bounded_on_every_list_that_hands_it_to_sql_or_a_slice(self, client):
        """#690 (2026-09-07): `/audit?limit=-1` returned a scoped bearer the whole
        scan (SQLite reads `LIMIT -1` as no limit; `rows[:-1]` is everything but
        one). The visibility predicate still applied; the 500-row scan cap did
        not. Same shape on /notifications (straight to SQL) and /events/recent
        (a slice). One guard, the 400 `/tickets` has always raised."""
        for path in ("/api/v1/audit?limit=-1", "/api/v1/audit?limit=0", "/api/v1/audit?limit=501",
                     "/api/v1/notifications?limit=-1", "/api/v1/events/recent?limit=-1",
                     "/api/v1/tickets?limit=-1"):
            r = client.get(path)
            assert r.status_code == 400, (path, r.status_code, r.text[:120])
            assert "Must be 1-500" in r.json()["detail"], r.json()
        for path in ("/api/v1/audit?limit=1", "/api/v1/notifications?limit=1", "/api/v1/events/recent?limit=1"):
            assert client.get(path).status_code == 200, path

    def test_device_register_creates_audit_entry(self, client):
        _register_device(client, "d1", "host1")
        r = client.get("/api/v1/audit?action=device.register")
        logs = r.json()
        assert len(logs) == 1
        assert logs[0]["target"] == "d1"
        assert logs[0]["actor"] == "system:registry"

    def test_service_register_creates_audit_entry(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080)
        r = client.get("/api/v1/audit?action=service.register")
        logs = r.json()
        assert len(logs) == 1
        assert logs[0]["target"] == "d1/web"

    def test_ticket_create_creates_audit_entry(self, client):
        client.post("/api/v1/tickets", json={
            "summary": "Audit test",
        })
        r = client.get("/api/v1/audit?action=ticket.create")
        logs = r.json()
        assert len(logs) == 1
        # source/actor is bearer-derived (admin user in test fixture).
        assert logs[0]["actor"] == "admin"

    def test_port_allocate_creates_audit_entry(self, client):
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            client.post("/api/v1/ports/allocate", json={
                "port_type": "forward",
                "owner_id": "test/svc",
            })
        r = client.get("/api/v1/audit?action=port.allocate")
        logs = r.json()
        assert len(logs) >= 1

    def test_audit_log_limit(self, client):
        for i in range(5):
            _register_device(client, f"d{i}", f"host{i}")
        r = client.get("/api/v1/audit?limit=3")
        assert len(r.json()) == 3

    def test_audit_log_detail_is_json(self, client):
        _register_device(client, "d1", "host1", ip="198.51.100.1")
        logs = client.get("/api/v1/audit?action=device.register").json()
        detail = json.loads(logs[0]["detail"])
        assert detail["hostname"] == "host1"


# ===========================================================================
# System Status
# ===========================================================================

class TestSystemStatus:
    def test_status_empty(self, client):
        r = client.get("/api/v1/status")
        assert r.status_code == 200
        body = r.json()
        assert body["counts"]["devices"] == 0
        assert body["counts"]["services"] == 0
        assert body["counts"]["ports"] == 0
        assert body["counts"]["routes"] == 0
        assert body["counts"]["open_tickets"] == 0

    def test_status_with_data(self, client):
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080, subdomain="web")
        client.post("/api/v1/tickets", json={
            "summary": "Open issue",
        })

        r = client.get("/api/v1/status")
        body = r.json()
        assert body["counts"]["devices"] == 1
        assert body["counts"]["services"] == 1
        assert body["counts"]["open_tickets"] == 1
        assert body["counts"]["routes"] >= 1

    def test_status_excludes_resolved_tickets(self, client):
        create = client.post("/api/v1/tickets", json={
            "summary": "Will resolve",
        })
        tid = create.json()["ticket_id"]
        client.patch(f"/api/v1/tickets/{tid}", json={"status": "resolved"})

        r = client.get("/api/v1/status")
        assert r.json()["counts"]["open_tickets"] == 0

    def test_status_seeded(self, seeded_client):
        r = seeded_client.get("/api/v1/status")
        body = r.json()
        assert body["counts"]["devices"] > 5
        assert body["counts"]["services"] > 10
        assert body["counts"]["ports"] > 10
        assert body["counts"]["routes"] > 10

    def test_status_unauthenticated_sees_nothing(self, client):
        """Security regression guard: a tokenless caller must never see the mesh.

        The dashboard once hit /status with no auth and got everything back.
        Fail closed — no token, empty fleet (counts 0, no rows leaked).
        """
        _register_device(client, "d1", "host1")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "d1", 8080, subdomain="web")
        # Raw underlying client = no bearer token injected.
        r = client._client.get("/api/v1/status")
        assert r.status_code == 200
        body = r.json()
        assert body["devices"] == []
        assert body["services"] == []
        assert body["routes"] == []
        assert body["counts"]["devices"] == 0
        assert body["counts"]["services"] == 0
        assert body["counts"]["routes"] == 0

    def test_status_scoped_user_sees_only_group(self, client, tmp_db):
        """A group-scoped user's dashboard shows only their devices/services."""
        _register_device(client, "dev-a", "host-a")
        _register_device(client, "dev-b", "host-b")
        _create_group(client, "dash-group", "Dash Group")
        client.post("/api/v1/groups/dash-group/devices", json={"device_id": "dev-a"})
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "svc-a", "dev-a", 8080, subdomain="svc-a")
            _register_service(client, "svc-b", "dev-b", 8081, subdomain="svc-b")

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "dash-agent", ["group:dash-group"])
            scoped = AuthedClient(raw, token)
            body = scoped.get("/api/v1/status").json()
            dev_ids = [d["id"] for d in body["devices"]]
            svc_ids = [s["id"] for s in body["services"]]
            assert dev_ids == ["dev-a"]
            assert "dev-a/svc-a" in svc_ids
            assert "dev-b/svc-b" not in svc_ids
            assert body["counts"]["devices"] == 1
            assert body["counts"]["services"] == 1

    def test_audit_unauthenticated_sees_nothing(self, client):
        """Same leak as /status: the dashboard activity feed must fail closed."""
        _register_device(client, "d1", "host1")  # writes a device.register entry
        r = client._client.get("/api/v1/audit")
        assert r.status_code == 200
        assert r.json() == []

    def test_audit_scoped_user_excludes_out_of_scope(self, client, tmp_db):
        """A scoped user's activity feed never surfaces out-of-scope targets."""
        _register_device(client, "dev-a", "host-a")
        _register_device(client, "dev-b", "host-b")
        _create_group(client, "aud-group", "Aud Group")
        client.post("/api/v1/groups/aud-group/devices", json={"device_id": "dev-a"})

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "aud-agent", ["group:aud-group"])
            scoped = AuthedClient(raw, token)
            entries = scoped.get("/api/v1/audit?limit=200").json()
            targets = [e.get("target") for e in entries]
            # dev-b registration is audited with target="dev-b" — must stay hidden.
            assert "dev-b" not in targets


# ===========================================================================
# Health / Root
# ===========================================================================

class TestHealth:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "samcloud-registry"
        # the release the image was built from (scope v1 train 2), never the FastAPI default
        assert body["version"] not in ("0.1.0", "unknown") and body["schema"] == "v20"

    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "healthy"
        assert body["devices"] == 0

    def test_health_with_devices(self, client):
        _register_device(client, "d1", "host1")
        _register_device(client, "d2", "host2")
        r = client.get("/health")
        assert r.json()["devices"] == 2


# ===========================================================================
# Helpers for group scope tests
# ===========================================================================

def _create_group(client, group_id="test-group", name="Test Group"):
    """Admin creates a group."""
    return client.post("/api/v1/groups", json={
        "id": group_id, "name": name,
    })


def _invite_and_activate(client, raw_client, username, scopes, role="agent"):
    """Invite a user, activate them, return their token."""
    inv = client.post("/api/v1/users/invite", json={
        "username": username,
        "scopes": scopes,
        "role": role,
    })
    assert inv.status_code == 200, f"Invite failed: {inv.json()}"
    invite_token = inv.json()["token"]
    # Activate
    act = raw_client.post("/api/v1/users/activate", json={
        "token": invite_token,
        "password": "test123",
    })
    assert act.status_code == 200, f"Activate failed: {act.json()}"
    if not scopes:
        # Scope v1 (doc 261): nothing unscoped is shared, so a seat invited with
        # no scopes at all belongs to the plane's own work, `samcloud-contributors`, as an
        # editor — the tests that use `[]` describe threads, not scope, and
        # assume the fleet can see one another's work.
        r = client.post("/api/v1/groups/samcloud-contributors/members", json={"user_id": act.json()["user_id"], "role": "editor"})
        assert r.status_code == 200, f"core membership failed: {r.text}"
    return act.json()["token"], act.json()["user_id"]


# ===========================================================================
# Group Scope — Read Scoping (Step 1)
# ===========================================================================

class TestGroupScope:
    """Test that list endpoints are scoped by group membership."""

    def test_visible_device_ids_admin_sees_all(self, client):
        """Admin should see all devices."""
        _register_device(client, "dev-a", "host-a")
        _register_device(client, "dev-b", "host-b")
        r = client.get("/api/v1/devices")
        assert len(r.json()) == 2

    def test_scoped_user_sees_only_group_devices(self, client, tmp_db):
        """User with group:X scope sees only devices in that group."""
        # Setup: create devices and group
        _register_device(client, "visible-dev", "host1")
        _register_device(client, "hidden-dev", "host2")
        _create_group(client, "my-group", "My Group")

        # Add only visible-dev to the group
        client.post("/api/v1/groups/my-group/devices", json={"device_id": "visible-dev"})

        # Create scoped user
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "scoped-agent", ["group:my-group"])
            scoped = AuthedClient(raw, token)

            # Scoped user should see only visible-dev
            r = scoped.get("/api/v1/devices")
            assert r.status_code == 200
            device_ids = [d["id"] for d in r.json()]
            assert "visible-dev" in device_ids
            assert "hidden-dev" not in device_ids

    def test_scoped_user_sees_only_group_services(self, client, tmp_db):
        """User with group:X scope sees only services on devices in that group."""
        _register_device(client, "dev-a", "host-a")
        _register_device(client, "dev-b", "host-b")
        _create_group(client, "svc-group", "Service Group")
        client.post("/api/v1/groups/svc-group/devices", json={"device_id": "dev-a"})

        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "svc-on-a", "dev-a", 8080)
            _register_service(client, "svc-on-b", "dev-b", 8081)

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "svc-agent", ["group:svc-group"])
            scoped = AuthedClient(raw, token)

            r = scoped.get("/api/v1/services")
            assert r.status_code == 200
            svc_ids = [s["id"] for s in r.json()]
            assert "dev-a/svc-on-a" in svc_ids
            assert "dev-b/svc-on-b" not in svc_ids

    def test_scoped_user_sees_only_visible_routes(self, client, tmp_db):
        """User with group scope sees only routes for services on visible devices."""
        _register_device(client, "dev-r1", "host-r1")
        _register_device(client, "dev-r2", "host-r2")
        _create_group(client, "route-group", "Route Group")
        client.post("/api/v1/groups/route-group/devices", json={"device_id": "dev-r1"})

        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "web", "dev-r1", 8080, subdomain="web-test")
            _register_service(client, "api", "dev-r2", 8081, subdomain="api-test")

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "route-agent", ["group:route-group"])
            scoped = AuthedClient(raw, token)

            r = scoped.get("/api/v1/routes")
            assert r.status_code == 200
            subdomains = [rt.get("subdomain") for rt in r.json()]
            assert "web-test" in subdomains
            assert "api-test" not in subdomains

    def test_scoped_user_list_users_sees_only_self(self, client, tmp_db):
        """Non-admin user sees only themselves in /users."""
        app = _get_app()
        _create_group(client, "users-group", "UG")
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "limited-user", ["group:users-group"])
            scoped = AuthedClient(raw, token)

            r = scoped.get("/api/v1/users")
            assert r.status_code == 200
            users = r.json()
            assert len(users) == 1
            assert users[0]["username"] == "limited-user"

    def test_scoped_user_sees_ticket_assigned_to_fellow_group_member(self, client, tmp_db):
        """#255 under scope v1 (doc 261): an admin filing a ticket to a group member
        without naming a group lands it in the assignee's group, so the member's
        group-mates see it; moving the assignee to someone outside that group is
        refused with the note."""
        _create_group(client, "containers", "Containers")
        _create_group(client, "other", "Other")

        app = _get_app()
        with TestClient(app) as raw:
            _invite_and_activate(client, raw, "claude-containers", ["group:containers"])
            _invite_and_activate(client, raw, "stranger", ["group:other"])
            caller_token, _ = _invite_and_activate(client, raw, "claude-producer", ["group:containers"])
            caller = AuthedClient(raw, caller_token)

            made = client.post("/api/v1/tickets", json={"summary": "fleet ticket", "assignee": "claude-containers"}).json()
            tid = made["ticket_id"]
            assert made["group_scope"] == "containers"

            assert caller.get(f"/api/v1/tickets/{tid}").status_code == 200
            assert tid in {t["id"] for t in caller.get("/api/v1/tickets").json()}

            moved = client.patch(f"/api/v1/tickets/{tid}", json={"assignee": "stranger"})
            assert moved.status_code == 422
            assert "not in group 'containers'" in moved.json()["detail"]

    def test_unaddressed_ticket_is_not_commons(self, client, tmp_db):
        """Scope v1 retires #339: a ticket that names no group goes to the
        writer's group (`samcloud-contributors` for the admin) and is hidden from everyone
        outside it — nothing unscoped is shared."""
        _create_group(client, "other", "Other")

        app = _get_app()
        with TestClient(app) as raw:
            caller_token, _ = _invite_and_activate(client, raw, "outsider", ["group:other"])
            caller = AuthedClient(raw, caller_token)

            made = client.post("/api/v1/tickets", json={"summary": "fleet ticket"}).json()
            tid = made["ticket_id"]
            assert made["group_scope"] == "samcloud-contributors"

            assert caller.get(f"/api/v1/tickets/{tid}").status_code == 404
            assert tid not in {t["id"] for t in caller.get("/api/v1/tickets").json()}

    def test_device_addressed_ticket_stays_out_of_scope(self, client, tmp_db):
        """A device-addressed ticket filed by the admin lands in the contributors group and stays
        hidden outside it (404 — doc 261, decision 4)."""
        _register_device(client, "dev-x", "host-x")
        _create_group(client, "containers", "Containers")
        _create_group(client, "other", "Other")
        client.post("/api/v1/groups/containers/devices", json={"device_id": "dev-x"})

        app = _get_app()
        with TestClient(app) as raw:
            caller_token, _ = _invite_and_activate(client, raw, "outsider", ["group:other"])
            caller = AuthedClient(raw, caller_token)

            tid = client.post("/api/v1/tickets",
                              json={"summary": "device thread", "device_id": "dev-x"}).json()["ticket_id"]
            assert caller.get(f"/api/v1/tickets/{tid}").status_code == 404

    def test_ticket_explicit_group_scope_visible_to_member(self, client, tmp_db):
        """#330: a ticket with group_scope set is visible to that group's members
        even with no assignee/device/service tying them to it."""
        _create_group(client, "containers", "Containers")
        app = _get_app()
        with TestClient(app) as raw:
            caller_token, _ = _invite_and_activate(client, raw, "claude-producer", ["group:containers"])
            caller = AuthedClient(raw, caller_token)
            # Admin files a ticket scoped to containers, no assignee/device.
            tid = client.post("/api/v1/tickets",
                              json={"summary": "shared", "group_scope": "containers"}).json()["ticket_id"]
            assert caller.get(f"/api/v1/tickets/{tid}").status_code == 200
            assert tid in {t["id"] for t in caller.get("/api/v1/tickets").json()}

    def test_ticket_group_scope_not_visible_outside_group(self, client, tmp_db):
        """A caller outside the ticket's group_scope still can't see it."""
        _create_group(client, "containers", "Containers")
        _create_group(client, "other", "Other")
        app = _get_app()
        with TestClient(app) as raw:
            caller_token, _ = _invite_and_activate(client, raw, "outsider", ["group:other"])
            caller = AuthedClient(raw, caller_token)
            tid = client.post("/api/v1/tickets",
                              json={"summary": "shared", "group_scope": "containers"}).json()["ticket_id"]
            assert caller.get(f"/api/v1/tickets/{tid}").status_code == 404

    def test_out_of_scope_identity_gets_no_ticket_notification(self, client, tmp_db):
        """A mention (or watcher fan-out) must not deliver a scoped ticket's
        summary/body to an identity that 403s on the ticket — and under rail
        3.9 a mention births an instance that could only 403 (#637)."""
        _create_group(client, "containers", "Containers")
        _create_group(client, "other", "Other")
        app = _get_app()
        with TestClient(app) as raw:
            out_token, _ = _invite_and_activate(client, raw, "outsider2", ["group:other"])
            in_token, _ = _invite_and_activate(client, raw, "insider", ["group:containers"])
            outsider, insider = AuthedClient(raw, out_token), AuthedClient(raw, in_token)
            tid = client.post("/api/v1/tickets",
                              json={"summary": "scoped secret", "group_scope": "containers"}).json()["ticket_id"]
            client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "@outsider2 @insider look"})
            def rows(who):
                inbox = who.get("/api/v1/notifications").json()["notifications"]
                return [n for n in inbox if n.get("ticket_id") == tid]
            assert rows(outsider) == []                       # cannot see it: no row at all
            assert any(n["event_type"] == "ticket.mentioned" for n in rows(insider))

    def test_ticket_unknown_group_scope_rejected(self, client, tmp_db):
        """An unknown group_scope is a clean 400 (mirrors docs/files, #330)."""
        r = client.post("/api/v1/tickets",
                        json={"summary": "x", "group_scope": "no-such-group"})
        assert r.status_code == 400

    def test_ticket_group_scope_outside_membership_forbidden(self, client, tmp_db):
        """A non-admin can't scope a ticket into a group they don't belong to."""
        _create_group(client, "containers", "Containers")
        _create_group(client, "secret", "Secret")
        app = _get_app()
        with TestClient(app) as raw:
            tok, _ = _invite_and_activate(client, raw, "member", ["group:containers"])
            member = AuthedClient(raw, tok)
            r = member.post("/api/v1/tickets",
                            json={"summary": "x", "group_scope": "secret"})
            assert r.status_code == 403

    def test_admin_sees_all_users(self, client, tmp_db):
        """Admin sees all users."""
        app = _get_app()
        _create_group(client, "admin-test", "AT")
        with TestClient(app) as raw:
            _invite_and_activate(client, raw, "user-one", ["group:admin-test"])
            _invite_and_activate(client, raw, "user-two", ["group:admin-test"])

        r = client.get("/api/v1/users")
        assert r.status_code == 200
        usernames = [u["username"] for u in r.json()]
        assert "admin" in usernames
        assert "user-one" in usernames
        assert "user-two" in usernames


# ===========================================================================
# Group Invite Without Admin (Step 2)
# ===========================================================================

class TestGroupInvite:
    """Test that group members can invite users into their group."""

    def test_group_owner_can_invite(self, client, tmp_db):
        """Owner of a group can invite new users into it."""
        _create_group(client, "invite-group", "IG")

        app = _get_app()
        with TestClient(app) as raw:
            # Create an owner-level user
            token, uid = _invite_and_activate(client, raw, "group-owner", ["group:invite-group"])
            # Promote to owner
            client.patch(f"/api/v1/groups/invite-group/members/{uid}",
                         json={"role": "owner"})
            owner = AuthedClient(raw, token)

            # Owner invites a new user
            r = owner.post("/api/v1/users/invite", json={
                "username": "new-agent",
                "scopes": ["group:invite-group"],
                "role": "agent",
            })
            assert r.status_code == 200
            assert r.json()["username"] == "new-agent"

    def test_editor_can_invite(self, client, tmp_db):
        """Editor of a group can invite new users into it."""
        _create_group(client, "editor-group", "EG")

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "group-editor", ["group:editor-group"])
            # Promote to editor
            client.patch(f"/api/v1/groups/editor-group/members/{uid}",
                         json={"role": "editor"})
            editor = AuthedClient(raw, token)

            r = editor.post("/api/v1/users/invite", json={
                "username": "invited-by-editor",
                "scopes": ["group:editor-group"],
                "role": "agent",
            })
            assert r.status_code == 200

    def test_viewer_cannot_invite(self, client, tmp_db):
        """Viewer cannot invite users."""
        _create_group(client, "viewer-group", "VG")

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "group-viewer", ["group:viewer-group"])
            # Leave as viewer (default)
            viewer = AuthedClient(raw, token)

            r = viewer.post("/api/v1/users/invite", json={
                "username": "should-fail",
                "scopes": ["group:viewer-group"],
                "role": "agent",
            })
            assert r.status_code == 403

    def test_cannot_grant_admin_scope(self, client, tmp_db):
        """Non-admin cannot grant admin (*) scope."""
        _create_group(client, "escalate-group", "EG2")

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "escalator", ["group:escalate-group"])
            client.patch(f"/api/v1/groups/escalate-group/members/{uid}",
                         json={"role": "owner"})
            user = AuthedClient(raw, token)

            r = user.post("/api/v1/users/invite", json={
                "username": "would-be-admin",
                "scopes": ["*"],
                "role": "admin",
            })
            assert r.status_code == 403

    def test_cannot_invite_into_other_group(self, client, tmp_db):
        """User cannot invite into a group they don't belong to."""
        _create_group(client, "my-grp", "MG")
        _create_group(client, "other-grp", "OG")

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "my-user", ["group:my-grp"])
            client.patch(f"/api/v1/groups/my-grp/members/{uid}",
                         json={"role": "owner"})
            user = AuthedClient(raw, token)

            r = user.post("/api/v1/users/invite", json={
                "username": "cross-group",
                "scopes": ["group:other-grp"],
                "role": "agent",
            })
            assert r.status_code == 403

    def test_admin_can_grant_per_group_role(self, client, tmp_db):
        """Admin invite with group_roles={gid: editor} stores editor in group_members."""
        _create_group(client, "role-grant", "RG")

        r = client.post("/api/v1/users/invite", json={
            "username": "new-editor",
            "scopes": ["group:role-grant"],
            "role": "agent",
            "group_roles": {"role-grant": "editor"},
        })
        assert r.status_code == 200
        uid = r.json()["user_id"]

        group = client.get("/api/v1/groups/role-grant").json()
        new_member = next(m for m in group["members"] if m["id"] == uid)
        assert new_member["group_role"] == "editor"

    def test_invalid_group_role_rejected(self, client, tmp_db):
        """group_roles with unknown role value → 400."""
        _create_group(client, "bad-role-grp", "BR")
        r = client.post("/api/v1/users/invite", json={
            "username": "bad-role-user",
            "scopes": ["group:bad-role-grp"],
            "role": "agent",
            "group_roles": {"bad-role-grp": "superuser"},
        })
        assert r.status_code == 400

    def test_editor_cannot_grant_owner(self, client, tmp_db):
        """Editor inviting with group_roles={gid: owner} → 403 (above own role)."""
        _create_group(client, "no-escalate", "NE")

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "editor-user", ["group:no-escalate"])
            client.patch(f"/api/v1/groups/no-escalate/members/{uid}",
                         json={"role": "editor"})
            editor = AuthedClient(raw, token)

            r = editor.post("/api/v1/users/invite", json={
                "username": "would-be-owner",
                "scopes": ["group:no-escalate"],
                "role": "agent",
                "group_roles": {"no-escalate": "owner"},
            })
            assert r.status_code == 403


# ===========================================================================
# Group Device Management (Step 3)
# ===========================================================================

class TestGroupDevices:
    """Test group device add/remove/list endpoints."""

    def test_list_group_devices(self, client, tmp_db):
        _register_device(client, "gd1", "host-gd1")
        _create_group(client, "dev-group", "DG")
        client.post("/api/v1/groups/dev-group/devices", json={"device_id": "gd1"})

        r = client.get("/api/v1/groups/dev-group/devices")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["id"] == "gd1"

    def test_add_device_to_group(self, client, tmp_db):
        _register_device(client, "gd2", "host-gd2")
        _create_group(client, "add-dg", "ADG")

        r = client.post("/api/v1/groups/add-dg/devices", json={"device_id": "gd2"})
        assert r.status_code == 200
        assert r.json()["status"] == "added"

    def test_add_device_duplicate(self, client, tmp_db):
        _register_device(client, "gd3", "host-gd3")
        _create_group(client, "dup-dg", "DDG")
        client.post("/api/v1/groups/dup-dg/devices", json={"device_id": "gd3"})

        r = client.post("/api/v1/groups/dup-dg/devices", json={"device_id": "gd3"})
        assert r.status_code == 409

    def test_remove_device_from_group(self, client, tmp_db):
        _register_device(client, "gd4", "host-gd4")
        _create_group(client, "rm-dg", "RDG")
        client.post("/api/v1/groups/rm-dg/devices", json={"device_id": "gd4"})

        r = client.delete("/api/v1/groups/rm-dg/devices/gd4")
        assert r.status_code == 200
        assert r.json()["status"] == "removed"

        # Verify it's gone
        r = client.get("/api/v1/groups/rm-dg/devices")
        assert len(r.json()) == 0

    def test_remove_nonexistent_device(self, client, tmp_db):
        _create_group(client, "nodev-dg", "NDG")
        r = client.delete("/api/v1/groups/nodev-dg/devices/nonexistent")
        assert r.status_code == 404

    def test_get_group_includes_devices(self, client, tmp_db):
        """GET /groups/{id} should include devices list."""
        _register_device(client, "gd5", "host-gd5")
        _create_group(client, "full-dg", "FDG")
        client.post("/api/v1/groups/full-dg/devices", json={"device_id": "gd5"})

        r = client.get("/api/v1/groups/full-dg")
        assert r.status_code == 200
        body = r.json()
        assert "devices" in body
        assert len(body["devices"]) == 1
        assert body["devices"][0]["id"] == "gd5"


# ===========================================================================
# Auth on Group Endpoints (Step 4)
# ===========================================================================

class TestGroupAuth:
    """Test auth is enforced on all group endpoints."""

    def test_list_groups_requires_auth(self, tmp_db):
        """GET /groups should require auth."""
        app = _get_app()
        with TestClient(app) as raw:
            r = raw.get("/api/v1/groups")
            assert r.status_code == 401

    def test_create_group_by_any_user_makes_them_owner(self, client, tmp_db):
        """POST /groups takes any user (scope v1, train 2): the maker owns it and
        nobody else is in it — a group is not a way into anyone else's."""
        app = _get_app()
        _create_group(client, "auth-grp", "AG")
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "non-admin", ["group:auth-grp"])
            user = AuthedClient(raw, token)

            r = user.post("/api/v1/groups", json={"id": "made", "name": "Made"})
            assert r.status_code == 200 and r.json()["owner"] == "non-admin"
            assert [m["username"] for m in user.get("/api/v1/groups/made").json()["members"]] == ["non-admin"]
            assert user.get("/api/v1/groups/auth-grp").json()["your_role"] == "viewer"

    def test_update_group_requires_owner(self, client, tmp_db):
        """PATCH /groups/{id} requires owner role."""
        _create_group(client, "upd-grp", "UG")
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "just-viewer", ["group:upd-grp"])
            viewer = AuthedClient(raw, token)

            r = viewer.patch("/api/v1/groups/upd-grp", json={"name": "Renamed"})
            assert r.status_code == 403

    def test_group_status_requires_membership(self, client, tmp_db):
        """GET /groups/{id}/status requires membership."""
        _create_group(client, "status-grp", "SG")
        _create_group(client, "other-grp-s", "OGS")
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "outsider", ["group:other-grp-s"])
            outsider = AuthedClient(raw, token)

            r = outsider.get("/api/v1/groups/status-grp/status")
            assert r.status_code == 403


# ===========================================================================
# Member Role PATCH (Step 5)
# ===========================================================================

class TestMemberRolePatch:
    """Test PATCH /groups/{id}/members/{user_id} for role changes."""

    def test_admin_can_change_role(self, client, tmp_db):
        _create_group(client, "role-grp", "RG")
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "role-target", ["group:role-grp"])

        r = client.patch(f"/api/v1/groups/role-grp/members/{uid}",
                         json={"role": "editor"})
        assert r.status_code == 200
        assert r.json()["role"] == "editor"

    def test_owner_can_promote_to_editor(self, client, tmp_db):
        _create_group(client, "promo-grp", "PG")
        app = _get_app()
        with TestClient(app) as raw:
            owner_token, owner_uid = _invite_and_activate(
                client, raw, "promo-owner", ["group:promo-grp"])
            client.patch(f"/api/v1/groups/promo-grp/members/{owner_uid}",
                         json={"role": "owner"})

            target_token, target_uid = _invite_and_activate(
                client, raw, "promo-target", ["group:promo-grp"])

            owner = AuthedClient(raw, target_token)  # reuse raw client
            owner_client = AuthedClient(raw, owner_token)

            r = owner_client.patch(f"/api/v1/groups/promo-grp/members/{target_uid}",
                                    json={"role": "editor"})
            assert r.status_code == 200
            assert r.json()["role"] == "editor"

    def test_editor_cannot_change_roles(self, client, tmp_db):
        """Editors don't have owner role, so they can't change roles."""
        _create_group(client, "noperm-grp", "NP")
        app = _get_app()
        with TestClient(app) as raw:
            editor_token, editor_uid = _invite_and_activate(
                client, raw, "editor-changer", ["group:noperm-grp"])
            client.patch(f"/api/v1/groups/noperm-grp/members/{editor_uid}",
                         json={"role": "editor"})

            target_token, target_uid = _invite_and_activate(
                client, raw, "target-user", ["group:noperm-grp"])

            editor = AuthedClient(raw, editor_token)
            r = editor.patch(f"/api/v1/groups/noperm-grp/members/{target_uid}",
                              json={"role": "owner"})
            assert r.status_code == 403

    def test_invalid_role_rejected(self, client, tmp_db):
        _create_group(client, "invalid-grp", "IG")
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "inv-role-user", ["group:invalid-grp"])

        r = client.patch(f"/api/v1/groups/invalid-grp/members/{uid}",
                         json={"role": "superadmin"})
        assert r.status_code == 422

    def test_patch_nonexistent_member(self, client, tmp_db):
        _create_group(client, "no-member-grp", "NMG")
        r = client.patch("/api/v1/groups/no-member-grp/members/99999",
                         json={"role": "editor"})
        assert r.status_code == 404


# ===========================================================================
# Auto-Populate group_services (Step 6)
# ===========================================================================

class TestAutoGroupServices:
    """Test that services auto-join groups containing their device."""

    def test_service_auto_added_to_group(self, client, tmp_db):
        """When a service registers on a device in a group, it auto-joins that group."""
        _register_device(client, "auto-dev", "host-auto")
        _create_group(client, "auto-grp", "Auto Group")
        client.post("/api/v1/groups/auto-grp/devices", json={"device_id": "auto-dev"})

        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "auto-svc", "auto-dev", 9090)

        # Check the group now includes the service
        r = client.get("/api/v1/groups/auto-grp")
        assert r.status_code == 200
        svc_ids = [s["id"] for s in r.json()["services"]]
        assert "auto-dev/auto-svc" in svc_ids

    def test_service_not_added_to_unrelated_group(self, client, tmp_db):
        """Service on a device NOT in a group should not join that group."""
        _register_device(client, "unrel-dev", "host-unrel")
        _register_device(client, "other-dev", "host-other")
        _create_group(client, "unrel-grp", "Unrelated Group")
        client.post("/api/v1/groups/unrel-grp/devices", json={"device_id": "other-dev"})

        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "unrel-svc", "unrel-dev", 9091)

        r = client.get("/api/v1/groups/unrel-grp")
        svc_ids = [s["id"] for s in r.json()["services"]]
        assert "unrel-dev/unrel-svc" not in svc_ids


# ===========================================================================
# Resources & leases — memory-key fallback (#7)
# ===========================================================================


class TestExclusiveLeaseIsAtomic:
    """#806 — `claude-services/gpu-pool` is declared exclusive and granted
    TWO active leases 20 ms apart on 2026-09-22, putting two generations into a
    single-slot pool for 53 seconds; a third request 53 s later was correctly
    refused, which is what a check-then-act race looks like from outside.

    Two defects, both here: the SELECT-then-INSERT was not atomic, and
    `allocation_mode` was never read at all, so a resource declared exclusive
    handed out shared leases to anyone who did not ask for exclusivity.

    (`service_id` is left unset throughout — `leases.service_id` is a foreign
    key, and inventing one produces `FOREIGN KEY constraint failed`, not a
    lease conflict. Worth knowing: for ten minutes this endpoint reported that
    error as a confident 409 "Exclusive lease held by unknown".)"""

    def _resource(self, client, dev_id, mode):
        _register_device(client, dev_id, dev_id)
        r = client.post("/api/v1/resources", json={
            "device_id": dev_id, "name": "pool", "type": "accelerator",
            "provider": "exo", "allocation_mode": mode,
            "specs": {"unified_memory_mb": 65536},
        })
        assert r.status_code == 200, r.text
        return f"/api/v1/resources/{dev_id}/pool/leases"

    def test_declared_exclusive_resource_grants_only_one(self, client):
        """A caller that does not pass exclusive=true still gets an exclusive
        lease, because the RESOURCE says so — and the second caller is refused."""
        url = self._resource(client, "excl-host-a", "exclusive")
        first = client.post(url, json={})
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "active"

        import db as _db
        row = _db.query_one("SELECT exclusive FROM leases WHERE id = ?",
                            (first.json()["lease_id"],))
        assert row["exclusive"] == 1, "allocation_mode was not honoured"

        second = client.post(url, json={})
        assert second.status_code == 409, second.text

    def test_the_guarantee_is_the_index_not_the_select(self, client):
        """The SELECT is a courtesy; the database is the guarantee. Insert an
        active exclusive lease behind the API's back — as a racing request would,
        between another's check and its insert — and the index must refuse it."""
        import sqlite3
        import db as _db
        self._resource(client, "excl-host-b", "exclusive")
        url = "/api/v1/resources/excl-host-b/pool/leases"
        assert client.post(url, json={}).status_code == 200

        with pytest.raises(sqlite3.IntegrityError) as e:
            _db.execute(
                "INSERT INTO leases (id, resource_id, exclusive, status, created_at) "
                "VALUES ('lease_race', 'excl-host-b/pool', 1, 'active', '2026-09-22')")
        # SQLite names the column, not the index — and the endpoint's handler
        # has to match on the same string, so pin it here.
        assert "UNIQUE constraint failed: leases.resource_id" in str(e.value), str(e.value)

    def test_a_shared_resource_is_still_shared(self, client):
        """The guard must not turn every pool into a single-slot one."""
        url = self._resource(client, "excl-host-c", "shared")
        assert client.post(url, json={}).status_code == 200
        assert client.post(url, json={}).status_code == 200

    def test_a_released_exclusive_lease_frees_the_slot(self, client):
        """The index is partial on status='active', so releasing must let the
        next caller in — otherwise the fix trades a race for a deadlock."""
        import db as _db
        url = self._resource(client, "excl-host-d", "exclusive")
        first = client.post(url, json={})
        assert first.status_code == 200
        _db.execute("UPDATE leases SET status = 'released' WHERE id = ?",
                    (first.json()["lease_id"],))
        assert client.post(url, json={}).status_code == 200


# ===========================================================================


class TestExclusiveLeaseAlwaysExpires:
    """#827 — an exclusive lease outliving its holder closed a healthy pool, and
    the worse shape had nothing to outlive: 2026-09-25 00:05Z,
    lease_2c50d13a01b120ac on claude-services/gpu-pool, exclusive,
    `expires_at` NULL, because the caller sent no duration and "null = indefinite"
    is what LeaseRequest documents. For a memory reservation that is honest; for a
    mutex it is an unbounded outage, since the only party that can retract the
    claim is the one that has gone away.

    Four incidents by 2026-09-27: two dead holders with a TTL (12 min and 2 min of
    an idle pool), one with no TTL at all, and one killed 21 ms after a predicate
    read the pool free."""

    def _pool(self, client, dev_id):
        _register_device(client, dev_id, dev_id)
        r = client.post("/api/v1/resources", json={
            "device_id": dev_id, "name": "pool", "type": "accelerator",
            "provider": "exo", "allocation_mode": "exclusive",
            "specs": {"unified_memory_mb": 65536},
        })
        assert r.status_code == 200, r.text
        return f"/api/v1/resources/{dev_id}/pool"

    def test_an_exclusive_lease_with_no_duration_still_expires(self, client):
        """The 00:05Z shape. `{}` on an exclusive resource must not be immortal."""
        url = self._pool(client, "ttl-host-a")
        r = client.post(url + "/leases", json={})
        assert r.status_code == 200, r.text
        assert r.json()["expires_at"], "an exclusive lease was granted with no expiry"

    def test_the_default_is_audited_so_the_caller_can_be_found(self, client):
        """Clamping rather than refusing keeps a seat working mid-restart; the
        audit row is the part that stops it being silent."""
        import db as _db
        url = self._pool(client, "ttl-host-b")
        lid = client.post(url + "/leases", json={}).json()["lease_id"]
        rows = _db.query("SELECT * FROM audit_log WHERE action = 'lease.ttl_defaulted' "
                         "AND target = ?", (lid,))
        assert rows, "no audit row naming the caller that relied on no expiry"

    def test_a_caller_that_asks_for_a_duration_keeps_it(self, client):
        """Clamping must not overwrite an explicit TTL."""
        url = self._pool(client, "ttl-host-c")
        r = client.post(url + "/leases", json={"ttl_seconds": 900})
        assert r.status_code == 200, r.text
        import datetime as _dt
        expires = _dt.datetime.fromisoformat(r.json()["expires_at"])
        granted = _dt.datetime.fromisoformat(r.json()["granted_at"])
        assert 800 < (expires - granted).total_seconds() < 1000, r.json()

    def test_an_unregistered_service_id_is_a_422_naming_it(self, client):
        """#849: a label in service_id was a FOREIGN KEY failure surfacing as 500."""
        url = self._pool(client, "ttl-host-svc")
        r = client.post(url + "/leases", json={"ttl_seconds": 60, "service_id": "restart-when-idle"})
        assert r.status_code == 422, (r.status_code, r.text)
        assert "restart-when-idle" in r.text and "service_id" in r.text, r.text

    def test_a_lease_with_no_service_id_is_still_granted(self, client):
        """The control: omitting the field is the supported way to lease as yourself."""
        url = self._pool(client, "ttl-host-nosvc")
        r = client.post(url + "/leases", json={"ttl_seconds": 60})
        assert r.status_code == 200, r.text

    def test_an_expired_exclusive_lease_does_not_block_the_next_grant(self, client):
        """The GRANT side of expiry. The renewal side (an expired lease can't be renewed
        back) is pinned by test_a_renewal_cannot_resurrect_a_lease_expired_under_it.
        Exclusivity is idx_leases_uq_exclusive, keyed on status rather than expires_at,
        so an expired-but-unreaped lease still reads active and the index refuses the
        next caller with a 409. Only request_lease's reap before the insert keeps an
        expired holder from closing the pool: an availability failure, not an
        exclusivity one. With the reap removed this test gets that 409 (#827 b2,
        claude-studio-services)."""
        import db as _db
        url = self._pool(client, "ttl-host-expired")
        first = client.post(url + "/leases", json={"ttl_seconds": 60})
        assert first.status_code == 200, first.text
        lid = first.json()["lease_id"]
        _db.execute("UPDATE leases SET expires_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00+00:00", lid))
        second = client.post(url + "/leases", json={"ttl_seconds": 60})
        assert second.status_code == 200, (second.status_code, second.text)
        assert second.json()["lease_id"] != lid, second.json()
        row = _db.query_one("SELECT status FROM leases WHERE id = ?", (lid,))
        assert row["status"] == "expired", row

    def test_a_shared_lease_may_still_be_indefinite(self, client):
        """"Indefinite" is honest for a memory reservation — only the mutex case
        changes."""
        _register_device(client, "ttl-host-d", "ttl-host-d")
        client.post("/api/v1/resources", json={
            "device_id": "ttl-host-d", "name": "pool", "type": "accelerator",
            "provider": "exo", "allocation_mode": "shared",
            "specs": {"unified_memory_mb": 65536}})
        r = client.post("/api/v1/resources/ttl-host-d/pool/leases", json={})
        assert r.status_code == 200, r.text
        assert r.json()["expires_at"] is None, r.json()


class _AdminAuth:
    """The admin AuthContext a function-level call needs. The `client` fixture
    authenticates as admin over HTTP; calling a handler directly has to pass one."""

    def identity(self):
        return "admin"

    def is_admin(self):
        return True

    owner_id = "user:1"


class TestRenewExtendsInPlace:
    """#827 P3 — a holder proves it is alive by restating its claim, and the claim
    is extended IN PLACE.

    In place is the property, not an implementation detail: the gateway's own
    `manager.py:_renew_leases()` releases and re-requests, which for an exclusive
    lease is a window in which another caller takes the resource — the failure
    renewal exists to prevent. And renewal needs a ceiling, because a live-but-
    stalled holder is real: 2026-09-27 14:17, a gateway held the pool for the full
    1500s while its generation was wedged and would have answered every
    heartbeat."""

    def _lease(self, client, dev_id, ttl, mode="exclusive"):
        _register_device(client, dev_id, dev_id)
        client.post("/api/v1/resources", json={
            "device_id": dev_id, "name": "pool", "type": "accelerator",
            "provider": "exo", "allocation_mode": mode,
            "specs": {"unified_memory_mb": 65536}})
        body = {} if ttl is None else {"ttl_seconds": ttl}
        r = client.post(f"/api/v1/resources/{dev_id}/pool/leases", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def test_a_renewal_extends_and_says_by_how_much(self, client):
        got = self._lease(client, "renew-host-a", 60)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 600})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["extended_by_s"] > 400, body
        assert body["capped"] is False, body
        assert body["expires_at"] > got["expires_at"], body

    def test_the_renewal_response_keeps_the_keys_its_consumers_read(self, client):
        """A CONTRACT, not a detail: samcloud-services `ollama/manager.py`
        `_renew_pool_leases` reads these keys BY NAME from every renewal. Renaming
        one breaks the gateway silently, because a missing `extended_by_s` reads as
        0 there and logs "NO extension" forever. Add keys freely. Rename or remove
        one only together with the gateway (#827)."""
        consumed = {"lease_id", "status", "granted_at", "expires_at",
                    "extended_by_s", "capped", "reason", "max_total_s"}
        got = self._lease(client, "renew-host-contract", 60)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew", json={"ttl_seconds": 600})
        assert r.status_code == 200, r.text
        missing = consumed - set(r.json())
        assert not missing, f"renewal response dropped consumed keys {sorted(missing)}"
        assert r.json()["reason"] in {"extended", "at_ceiling", "ttl_below_granted", "indefinite"}, r.json()

    def test_a_renewal_within_a_second_of_its_grant_is_an_extension(self, client):
        """2026-09-27 21:15:19Z: renewed 486 ms after the grant, asking the same TTL.
        The expiry moved by a fraction of a second, int() made it 0, and the
        registry answered `ttl_below_granted`, a misconfiguration nobody made."""
        got = self._lease(client, "renew-host-subsec", 600)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew", json={"ttl_seconds": 600})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reason"] == "extended", body
        assert body["extended_by_s"] >= 1, body
        assert body["expires_at"] > got["expires_at"], body

    def test_a_renewal_shorter_than_the_grant_still_says_so(self, client):
        """The control: the real misconfiguration keeps its name."""
        got = self._lease(client, "renew-host-short", 900)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew", json={"ttl_seconds": 60})
        assert r.status_code == 200, r.text
        assert r.json()["reason"] == "ttl_below_granted", r.json()
        assert r.json()["extended_by_s"] == 0, r.json()

    def test_the_audit_row_records_the_expiry_the_row_actually_holds(self, client):
        """Before 0.12.56 the audit logged the TARGET expiry even when the UPDATE was
        skipped, so the trail claimed an expiry the lease never had, and nothing
        read the audit row to notice. Checked for an extension and for a
        renewal that moves nothing. The sub-second case is the one that diverged: the
        old code skipped the UPDATE on int() == 0 and still audited the target
        (claude-studio-services, #827). Without it this test passes on 0.12.55."""
        import db as _db
        for dev, grant, ask in (("renew-audit-ext", 60, 600),       # a real extension
                               ("renew-audit-subsec", 600, 600),  # moves by milliseconds: the case that diverged
                               ("renew-audit-none", 900, 60)):    # moves nothing
            got = self._lease(client, dev, grant)
            r = client.post(f"/api/v1/leases/{got['lease_id']}/renew", json={"ttl_seconds": ask})
            assert r.status_code == 200, r.text
            row = _db.query_one("SELECT expires_at FROM leases WHERE id = ?", (got["lease_id"],))
            audit = _db.query("SELECT detail FROM audit_log WHERE action = 'lease.renewed' "
                              "AND target = ? ORDER BY id DESC LIMIT 1", (got["lease_id"],))
            assert audit, f"no lease.renewed audit row for {dev}"
            import json as _json
            logged = _json.loads(audit[0]["detail"])["expires_at"]
            assert logged == row["expires_at"] == r.json()["expires_at"], (dev, logged, row, r.json())

    def test_the_same_row_is_extended_nothing_is_reacquired(self, client):
        """The negative assertion is the one that matters: no release, no new
        lease. A renewal that released and re-requested would pass a test that
        only checked the expiry moved."""
        import db as _db
        got = self._lease(client, "renew-host-b", 60)
        before = _db.query("SELECT id, status FROM leases WHERE resource_id = ?",
                           ("renew-host-b/pool",))
        client.post(f"/api/v1/leases/{got['lease_id']}/renew", json={"ttl_seconds": 600})
        after = _db.query("SELECT id, status FROM leases WHERE resource_id = ?",
                          ("renew-host-b/pool",))
        assert len(after) == len(before) == 1, (before, after)
        assert after[0]["id"] == got["lease_id"], after
        assert after[0]["status"] == "active", after

    def test_the_ceiling_caps_a_renewal_and_says_so(self, client):
        """Granted at the ceiling, a renewal is accepted and extends nothing. It
        must report that rather than look like an extension — this is exactly
        stage (a) on the gateway, whose TTL is the ceiling."""
        got = self._lease(client, "renew-host-c", 1800)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 1800})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["capped"] is True, body
        assert body["extended_by_s"] == 0, body
        assert body["expires_at"] == got["expires_at"], body
        assert body["max_total_s"] == 1800, body

    def test_a_renewal_never_shortens(self, client):
        """A small ttl on renew must not hand an exclusive resource away early."""
        got = self._lease(client, "renew-host-d", 900)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 5})
        assert r.status_code == 200, r.text
        assert r.json()["expires_at"] == got["expires_at"], r.json()
        assert r.json()["extended_by_s"] == 0, r.json()

    def test_an_expired_lease_cannot_be_renewed_back_to_life(self, client):
        """The reap runs first. Otherwise a holder that stopped renewing could
        resurrect a claim another caller has since been granted."""
        import db as _db
        got = self._lease(client, "renew-host-e", 900)
        _db.execute("UPDATE leases SET expires_at = ? WHERE id = ?",
                    ("2020-01-01T00:00:00+00:00", got["lease_id"]))
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 900})
        assert r.status_code == 404, r.text
        row = _db.query_one("SELECT status FROM leases WHERE id = ?", (got["lease_id"],))
        assert row["status"] == "expired", row

    def test_only_the_holder_may_renew(self, client):
        """A lease is not a shared handle. Another identity restating someone
        else's claim is how a mutex stops meaning anything.

        Called at the function level, because the `client` fixture authenticates
        as admin and `is_admin()` legitimately bypasses the holder check — so
        through the API this branch is unreachable. That is also why the identical
        rule on `release_lease` has no test: worth knowing rather than leaving the
        rule asserted only in prose."""
        import main as _main
        got = self._lease(client, "renew-host-f", 900)
        stranger = _main.AuthContext.__new__(_main.AuthContext)
        stranger.identity = lambda: "someone-else"
        stranger.is_admin = lambda: False
        stranger.owner_id = "user:99999"
        from fastapi import HTTPException as _HTTPException
        with pytest.raises(_HTTPException) as e:
            _main.renew_lease(got["lease_id"],
                              _main.LeaseRenew(ttl_seconds=900), auth=stranger)
        assert e.value.status_code == 403, e.value.detail

    def test_the_holder_itself_may_renew_without_being_admin(self, client):
        """The other half: the rule must not refuse the holder. Without this the
        403 above passes on a function that refuses everyone."""
        import main as _main
        import db as _db
        got = self._lease(client, "renew-host-i", 60)
        agent = _db.query_one("SELECT agent_id FROM leases WHERE id = ?",
                              (got["lease_id"],))["agent_id"]
        holder = _main.AuthContext.__new__(_main.AuthContext)
        holder.identity = lambda: "the-holder"
        holder.is_admin = lambda: False
        holder.owner_id = agent
        out = _main.renew_lease(got["lease_id"],
                                _main.LeaseRenew(ttl_seconds=600), auth=holder)
        assert out["extended_by_s"] > 400, out

    def test_a_renewal_cannot_resurrect_a_lease_expired_under_it(self, client):
        """The read-then-write window, closed and asserted rather than described.

        Handlers run in a threadpool, so any other request's reap can expire this
        row between the SELECT and the UPDATE. The interleaving is made
        deterministic here by expiring the row at the moment the handler has just
        read it as active — which is precisely what a concurrent reap does — and
        the write is guarded on `status = 'active'`, so it changes no rows and the
        renewal answers 404 instead of extending a lease the plane has already
        given up."""
        import main as _main
        import db as _db
        from fastapi import HTTPException as _HTTPException
        got = self._lease(client, "renew-host-race", 60)
        real = _main.db.query_one

        def expire_after_read(sql, params=()):
            row = real(sql, params)
            if "FROM leases WHERE id" in sql:
                # the concurrent reap, landing between this SELECT and the UPDATE
                _db.execute("UPDATE leases SET status = 'expired' WHERE id = ?",
                            (got["lease_id"],))
            return row

        _main.db.query_one = expire_after_read
        try:
            with pytest.raises(_HTTPException) as e:
                _main.renew_lease(got["lease_id"],
                                  _main.LeaseRenew(ttl_seconds=600),
                                  auth=_AdminAuth())
        finally:
            _main.db.query_one = real
        assert e.value.status_code == 404, e.value.detail
        row = _db.query_one("SELECT status, expires_at FROM leases WHERE id = ?",
                            (got["lease_id"],))
        assert row["status"] == "expired", row
        assert row["expires_at"] == got["expires_at"], \
            "the expiry moved on a lease that was already expired"

    def test_ttl_seconds_is_required(self, client):
        """Nothing is inferred: a defaulted ttl would either shorten a long lease
        or need the original span, which is lost after the first renewal."""
        got = self._lease(client, "renew-host-g", 900)
        assert client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                           json={}).status_code == 422

    def test_the_response_names_its_own_reason(self, client):
        """`extended_by_s: 0` has three causes and a caller cannot tell them apart
        from the numbers. One seat's renewal loop inferred "0 means
        ceiling" and would have logged "capped at the 1800s ceiling" for an
        indefinite lease — a reason the registry never gave. So the reason is
        returned rather than derived."""
        got = self._lease(client, "reason-host-a", 60)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 600})
        assert r.json()["reason"] == "extended", r.json()

        at_ceiling = self._lease(client, "reason-host-b", 1800)
        r = client.post(f"/api/v1/leases/{at_ceiling['lease_id']}/renew",
                        json={"ttl_seconds": 1800})
        assert r.json()["reason"] == "at_ceiling", r.json()

        indefinite = self._lease(client, "reason-host-c", None, mode="shared")
        r = client.post(f"/api/v1/leases/{indefinite['lease_id']}/renew",
                        json={"ttl_seconds": 600})
        assert r.json()["reason"] == "indefinite", r.json()

        # Asking for LESS than the lease was granted: reachable by configuration
        # (a renewal ttl below the granted one), and a misconfiguration rather
        # than an anomaly — so it must not read as the ceiling.
        further_out = self._lease(client, "reason-host-d", 1800)
        r = client.post(f"/api/v1/leases/{further_out['lease_id']}/renew",
                        json={"ttl_seconds": 600})
        assert r.json()["extended_by_s"] == 0, r.json()
        assert r.json()["capped"] is False, r.json()
        assert r.json()["reason"] == "ttl_below_granted", r.json()

    def test_an_extension_can_reach_the_ceiling_in_the_same_renewal(self, client):
        """`capped` and a real extension arrive together on the last renewal
        before the ceiling, so a caller that branches on `capped` first prints
        "no extension" for a renewal that extended. The reason resolves it:
        `extended` wins."""
        import db as _db
        got = self._lease(client, "reason-host-e", 600)
        # Granted 1750s ago, so the ceiling is 50s away — and still live, with 20s
        # to run. Asking for 600 can only reach the ceiling: +30s, and capped.
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        _db.execute("UPDATE leases SET granted_at = ?, expires_at = ? WHERE id = ?",
                    ((now - _dt.timedelta(seconds=1750)).isoformat(),
                     (now + _dt.timedelta(seconds=20)).isoformat(),
                     got["lease_id"]))
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 600})
        body = r.json()
        assert body["extended_by_s"] > 0, body
        assert body["capped"] is True, body
        assert body["reason"] == "extended", body

    def test_a_long_grant_can_renew_within_its_own_ceiling(self, client):
        """The adapter's shape, and the reason the ceiling is per lease: a short TTL
        for liveness and a long `max_total_s` for exposure. A single global 1800s
        ceiling made this impossible — renewals of a 5400s hold extended nothing,
        forever, and reported the wrong cause for it."""
        _register_device(client, "maxtot-host-a", "maxtot-host-a")
        client.post("/api/v1/resources", json={
            "device_id": "maxtot-host-a", "name": "pool", "type": "accelerator",
            "provider": "exo", "allocation_mode": "exclusive",
            "specs": {"unified_memory_mb": 65536}})
        got = client.post("/api/v1/resources/maxtot-host-a/pool/leases",
                          json={"ttl_seconds": 120, "max_total_s": 5400}).json()
        assert got["max_total_s"] == 5400, got
        # Age it: granted 1700s ago, 20s left to run. A 1200s renewal now takes the
        # expiry to granted+2900 — past the old global 1800s ceiling, which is the
        # whole point. Under that ceiling the same call would have capped at
        # granted+1800, i.e. 80s, and reported `at_ceiling`.
        import datetime as _dt
        import db as _db
        now = _dt.datetime.now(_dt.timezone.utc)
        granted = now - _dt.timedelta(seconds=1700)
        _db.execute("UPDATE leases SET granted_at = ?, expires_at = ? WHERE id = ?",
                    (granted.isoformat(),
                     (now + _dt.timedelta(seconds=20)).isoformat(),
                     got["lease_id"]))
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 1200})
        body = r.json()
        assert body["reason"] == "extended", body
        assert body["capped"] is False, body
        assert body["max_total_s"] == 5400, body
        assert body["extended_by_s"] > 1100, body
        held = (_dt.datetime.fromisoformat(body["expires_at"]) - granted).total_seconds()
        assert held > 1800, f"did not pass the old global ceiling: {held}s"

    def test_the_ceiling_defaults_to_the_greater_of_the_default_and_the_ttl(self, client):
        """A caller naming only a TTL gets what it always got; one naming a TTL above
        the default is not silently capped below its own grant."""
        short = self._lease(client, "maxtot-host-b", 60)
        assert short["max_total_s"] == 1800, short
        long_one = self._lease(client, "maxtot-host-c", 3600)
        assert long_one["max_total_s"] == 3600, long_one

    def test_a_row_granted_before_the_column_falls_back_to_the_default(self, client):
        """NULL means "the registry default" — every lease granted before this
        column says NULL, and they must keep renewing rather than fail."""
        import db as _db
        got = self._lease(client, "maxtot-host-d", 60)
        _db.execute("UPDATE leases SET max_total_s = NULL WHERE id = ?",
                    (got["lease_id"],))
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 600})
        assert r.status_code == 200, r.text
        assert r.json()["max_total_s"] == 1800, r.json()
        assert r.json()["reason"] == "extended", r.json()

    def test_max_total_s_is_clamped_to_the_registry_hard_limit(self, client):
        """Without a hard limit the field undoes the ceiling it implements: any
        caller could send 10**9 and renew forever. Clamped rather than refused, and
        audited so the caller asking for too much is findable."""
        import db as _db
        _register_device(client, "hard-host-a", "hard-host-a")
        client.post("/api/v1/resources", json={
            "device_id": "hard-host-a", "name": "pool", "type": "accelerator",
            "provider": "exo", "allocation_mode": "exclusive",
            "specs": {"unified_memory_mb": 65536}})
        got = client.post("/api/v1/resources/hard-host-a/pool/leases",
                          json={"ttl_seconds": 120, "max_total_s": 10 ** 9}).json()
        assert got["max_total_s"] == 21600, got
        rows = _db.query("SELECT * FROM audit_log WHERE action = 'lease.max_total_clamped' "
                         "AND target = ?", (got["lease_id"],))
        assert rows, "a clamp with no audit row is a limit nobody can trace"
        # And asking for a billion seconds on the renewal gets exactly the limit:
        # it extends (so `extended` wins, as it must when both are true) and stops
        # at granted + 21600 rather than anywhere near what was asked for.
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 10 ** 9})
        body = r.json()
        assert body["max_total_s"] == 21600, body
        assert body["capped"] is True, body
        assert body["reason"] == "extended", body
        import datetime as _dt
        held = (_dt.datetime.fromisoformat(body["expires_at"])
                - _dt.datetime.fromisoformat(body["granted_at"])).total_seconds()
        assert abs(held - 21600) < 2, f"ceiling not honoured: held {held}s"

    def test_a_renewal_ttl_below_the_grant_names_itself(self, client):
        """Distinct from at_ceiling, because the two had one name and the adapter's
        >ceiling grant was reported as a too-small renewal ttl."""
        got = self._lease(client, "maxtot-host-e", 1200)
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 60})
        body = r.json()
        assert body["extended_by_s"] == 0, body
        assert body["capped"] is False, body
        assert body["reason"] == "ttl_below_granted", body

    def test_an_indefinite_lease_has_nothing_to_renew(self, client):
        got = self._lease(client, "renew-host-h", None, mode="shared")
        assert got["expires_at"] is None, got
        r = client.post(f"/api/v1/leases/{got['lease_id']}/renew",
                        json={"ttl_seconds": 600})
        assert r.status_code == 200, r.text
        assert r.json()["expires_at"] is None, r.json()
        assert r.json()["extended_by_s"] == 0, r.json()


# ===========================================================================


class TestAnExpiredLeaseNeverReadsActive:
    """#827 — the read is what the guard trusts, and it lied.

    `_expire_stale_leases()` expires everything past its `expires_at`, but its
    only caller was `_get_available_memory()`. `GET /resources/{id}` selected the
    lease rows and THEN called that, so the response carried `status: "active"`
    for a lease the same request was about to expire; `GET /leases` never reaped at
    all. A lease-aware guard reads exactly that array to decide whether the
    pool is leased, and it fails closed — so one stale "active" suppresses a wedge
    recovery for a pass, which couples this ticket to #823's recovery path."""

    def _leased_pool(self, client, dev_id, expires_at):
        import db as _db
        _register_device(client, dev_id, dev_id)
        client.post("/api/v1/resources", json={
            "device_id": dev_id, "name": "pool", "type": "accelerator",
            "provider": "exo", "allocation_mode": "exclusive",
            "specs": {"unified_memory_mb": 65536}})
        rid = f"{dev_id}/pool"
        lid = client.post(f"/api/v1/resources/{rid}/leases",
                          json={"ttl_seconds": 1800}).json()["lease_id"]
        # Into the past, behind the API's back — a holder that died is
        # indistinguishable from one whose clock simply passed.
        _db.execute("UPDATE leases SET expires_at = ? WHERE id = ?", (expires_at, lid))
        return rid, lid

    def test_the_resource_read_does_not_report_it_active(self, client):
        rid, lid = self._leased_pool(client, "reap-host-a", "2020-01-01T00:00:00+00:00")
        r = client.get(f"/api/v1/resources/{rid}")
        assert r.status_code == 200, r.text
        assert [l for l in r.json()["leases"] if l["id"] == lid] == [], \
            "an expired lease was reported active to the reader the guard uses"

    def test_the_listing_does_not_report_it_active(self, client):
        rid, lid = self._leased_pool(client, "reap-host-b", "2020-01-01T00:00:00+00:00")
        r = client.get("/api/v1/leases?status=active")
        assert r.status_code == 200, r.text
        assert [l for l in r.json() if l["id"] == lid] == [], \
            "GET /leases?status=active listed a lease that expired in 2020"

    def test_a_lease_that_has_NOT_expired_still_reads_active(self, client):
        """The reap must not free a live holder — that would trade a stale claim
        for a stolen one."""
        rid, lid = self._leased_pool(client, "reap-host-c", "2099-01-01T00:00:00+00:00")
        r = client.get(f"/api/v1/resources/{rid}")
        assert [l["id"] for l in r.json()["leases"]] == [lid], r.json()["leases"]


# ===========================================================================


class TestResourceMemoryFallback:
    """Regression for ticket #7 — _get_available_memory must read gpu_memory_mb,
    not just unified_memory_mb / vram_mb / ram_mb. Discrete GPUs report
    gpu_memory_mb; previously the lookup chain skipped it and total fell to 0."""

    def _register_gpu(self, client, dev_id, res_name, specs):
        _register_device(client, dev_id, dev_id)
        return client.post(
            "/api/v1/resources",
            json={
                "device_id": dev_id,
                "name": res_name,
                "type": "gpu",
                "provider": "test",
                "model": "test-gpu",
                "allocation_mode": "shared",
                "specs": specs,
            },
        )

    def test_gpu_memory_mb_resolves_total(self, client):
        """Resource registered with only gpu_memory_mb should expose its total.

        Pre-fix: the lookup chain was vram_mb → unified_memory_mb → ram_mb, so
        gpu_memory_mb yielded total=0 and any non-zero lease was queued. Post-fix
        the lease is granted active. Available capacity subtracts it once.
        """
        r = self._register_gpu(client, "gpu-host-a", "gpu-0", {"gpu_memory_mb": 24576})
        assert r.status_code == 200, r.text
        lease = client.post(
            "/api/v1/resources/gpu-host-a/gpu-0/leases",
            json={"memory_mb": 8192},
        )
        assert lease.status_code == 200, lease.text
        body = lease.json()
        # The fix: status is 'active' (not 'queued'), proving total > 0.
        assert body["status"] == "active"
        assert body["resource_available_mb"] == 16384

    def test_unified_memory_mb_still_works(self, client):
        """Apple-Silicon spec with only unified_memory_mb should still resolve (regression guard)."""
        r = self._register_gpu(client, "gpu-host-b", "gpu-0", {"unified_memory_mb": 65536})
        assert r.status_code == 200, r.text
        lease = client.post(
            "/api/v1/resources/gpu-host-b/gpu-0/leases",
            json={"memory_mb": 16384},
        )
        assert lease.status_code == 200, lease.text
        body = lease.json()
        assert body["status"] == "active"
        assert body["resource_available_mb"] == 49152

    def test_vram_mb_takes_precedence(self, client):
        """When multiple keys are present, vram_mb wins (most explicit)."""
        r = self._register_gpu(
            client,
            "gpu-host-c",
            "gpu-0",
            {"vram_mb": 12288, "gpu_memory_mb": 24576, "unified_memory_mb": 65536},
        )
        assert r.status_code == 200, r.text
        lease = client.post(
            "/api/v1/resources/gpu-host-c/gpu-0/leases",
            json={"memory_mb": 4096},
        )
        assert lease.status_code == 200, lease.text
        # vram_mb=12288 wins; subtract the granted 4096 once.
        assert lease.json()["resource_available_mb"] == 8192

    def test_ttl_seconds_alias_for_duration_s(self, client):
        """Ticket #6 — clients posting ttl_seconds should get a non-null expires_at.

        Pre-fix: only `duration_s` was accepted → ttl_seconds silently ignored →
        expires_at returned null. Post-fix the alias works; duration_s still wins
        when both are present.
        """
        from datetime import datetime, timezone, timedelta

        r = self._register_gpu(client, "gpu-host-ttl-a", "gpu-0", {"gpu_memory_mb": 8192})
        assert r.status_code == 200, r.text

        before = datetime.now(timezone.utc)
        lease = client.post(
            "/api/v1/resources/gpu-host-ttl-a/gpu-0/leases",
            json={"memory_mb": 1024, "ttl_seconds": 3600},
        )
        assert lease.status_code == 200, lease.text
        body = lease.json()
        assert body["status"] == "active"
        assert body["expires_at"] is not None
        expires = datetime.fromisoformat(body["expires_at"])
        delta = expires - before
        assert timedelta(seconds=3500) < delta < timedelta(seconds=3700), (
            f"expected ~3600s ahead, got {delta}"
        )

    def test_duration_s_still_works(self, client):
        """Regression guard — existing duration_s callers still get expires_at."""
        from datetime import datetime, timezone, timedelta

        r = self._register_gpu(client, "gpu-host-ttl-b", "gpu-0", {"gpu_memory_mb": 8192})
        assert r.status_code == 200, r.text
        lease = client.post(
            "/api/v1/resources/gpu-host-ttl-b/gpu-0/leases",
            json={"memory_mb": 1024, "duration_s": 1800},
        )
        assert lease.status_code == 200, lease.text
        assert lease.json()["expires_at"] is not None

    def test_duration_s_wins_when_both_set(self, client):
        """If a client sends both, duration_s is authoritative."""
        from datetime import datetime, timezone, timedelta

        r = self._register_gpu(client, "gpu-host-ttl-c", "gpu-0", {"gpu_memory_mb": 8192})
        assert r.status_code == 200, r.text
        before = datetime.now(timezone.utc)
        lease = client.post(
            "/api/v1/resources/gpu-host-ttl-c/gpu-0/leases",
            json={"memory_mb": 1024, "duration_s": 60, "ttl_seconds": 7200},
        )
        assert lease.status_code == 200, lease.text
        expires = datetime.fromisoformat(lease.json()["expires_at"])
        delta = expires - before
        # duration_s=60 wins, not ttl_seconds=7200
        assert timedelta(seconds=30) < delta < timedelta(seconds=120), (
            f"expected ~60s ahead, got {delta}"
        )

    def test_no_duration_no_expires(self, client):
        """When neither field is set, lease has no expiry (existing semantic)."""
        r = self._register_gpu(client, "gpu-host-ttl-d", "gpu-0", {"gpu_memory_mb": 8192})
        assert r.status_code == 200, r.text
        lease = client.post(
            "/api/v1/resources/gpu-host-ttl-d/gpu-0/leases",
            json={"memory_mb": 1024},
        )
        assert lease.status_code == 200, lease.text
        assert lease.json()["expires_at"] is None

    def test_oversized_request_is_refused_not_queued(self, client):
        """A request bigger than the resource can never be granted. It used to
        queue (confirming total was honest); since the queue is promoted in order
        (0.12.98) it would sit at the head and block everyone behind it until its
        wait ran out, so it is a named 409 instead."""
        r = self._register_gpu(client, "gpu-host-d", "gpu-0", {"gpu_memory_mb": 8192})
        assert r.status_code == 200, r.text
        lease = client.post(
            "/api/v1/resources/gpu-host-d/gpu-0/leases",
            json={"memory_mb": 16384},
        )
        assert lease.status_code == 409, lease.text
        body = lease.json()["detail"]
        assert body["code"] == "exceeds_capacity"
        assert body["total_mb"] == 8192 and body["requested_mb"] == 16384


class TestResourceScope:
    """Resources/leases reads are scope-enforced and fail closed (staging audit).

    Read endpoints used to be public — an unauthenticated caller could
    enumerate every GPU and lease across the fleet. Now: admin sees all,
    scoped users see only their devices' resources, unauth sees nothing.
    Mutations were already auth-gated and are unchanged.
    """

    def _register_gpu(self, client, dev_id, name="gpu-0"):
        _register_device(client, dev_id, dev_id)
        return client.post("/api/v1/resources", json={
            "device_id": dev_id, "name": name, "type": "gpu", "provider": "test",
            "model": "test-gpu", "allocation_mode": "shared",
            "specs": {"gpu_memory_mb": 24576},
        })

    def test_unauthenticated_sees_no_resources(self, client):
        self._register_gpu(client, "gpu-vis")
        raw = client._client  # no bearer token
        assert raw.get("/api/v1/resources").json() == []
        assert raw.get("/api/v1/resources/dashboard").json() == []
        assert raw.get("/api/v1/leases").json() == []

    def test_unauthenticated_resource_detail_forbidden(self, client):
        self._register_gpu(client, "gpu-secret")
        r = client._client.get("/api/v1/resources/gpu-secret/gpu-0")
        assert r.status_code == 401

    def test_admin_sees_all_resources(self, client):
        self._register_gpu(client, "gpu-x")
        self._register_gpu(client, "gpu-y")
        assert len(client.get("/api/v1/resources/dashboard").json()) == 2

    def test_scoped_user_sees_only_group_resources(self, client, tmp_db):
        self._register_gpu(client, "gpu-a")
        self._register_gpu(client, "gpu-b")
        _create_group(client, "res-group", "Res Group")
        client.post("/api/v1/groups/res-group/devices", json={"device_id": "gpu-a"})
        client.post("/api/v1/resources/gpu-a/gpu-0/leases", json={"memory_mb": 1024})
        client.post("/api/v1/resources/gpu-b/gpu-0/leases", json={"memory_mb": 1024})

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "res-agent", ["group:res-group"])
            scoped = AuthedClient(raw, token)

            res_ids = [r["id"] for r in scoped.get("/api/v1/resources").json()]
            assert "gpu-a/gpu-0" in res_ids
            assert "gpu-b/gpu-0" not in res_ids

            dash_ids = [r["id"] for r in scoped.get("/api/v1/resources/dashboard").json()]
            assert "gpu-a/gpu-0" in dash_ids
            assert "gpu-b/gpu-0" not in dash_ids

            lease_res = [l["resource_id"] for l in scoped.get("/api/v1/leases").json()]
            assert "gpu-b/gpu-0" not in lease_res
            assert all(rid == "gpu-a/gpu-0" for rid in lease_res)

            assert scoped.get("/api/v1/resources/gpu-b/gpu-0").status_code == 404   # hidden (doc 261, decision 4)
            assert scoped.get("/api/v1/resources/gpu-a/gpu-0").status_code == 200


# ===========================================================================
# Ticket subscriptions + durable notifications (RFC #37)
# ===========================================================================

class TestNotifications:
    """Watchers, fan-out, inbox, ack, long-poll watch, event visibility."""

    def _ticket_with_alice(self, client, raw):
        """Admin files a ticket assigned to invited agent 'alice'.
        Returns (alice_client, ticket_id)."""
        token, _ = _invite_and_activate(client, raw, "alice", [])
        alice = AuthedClient(raw, token)
        r = client.post("/api/v1/tickets", json={"summary": "Notif test",
                                                 "assignee": "alice"})
        assert r.status_code == 200, r.json()
        return alice, r.json()["ticket_id"]

    def test_create_auto_subscribes_filer_and_assignee(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            watchers = client.get(f"/api/v1/tickets/{tid}/watchers").json()["watchers"]
            assert {w["identity"] for w in watchers} == {"admin", "alice"}

    def test_create_response_says_where_replies_go(self, client):
        # doc 177 v2: what happened + where replies land; no watch loop, no
        # claim release — the 3.8 hint taught flows the rail no longer runs.
        r = client.post("/api/v1/tickets", json={"summary": "hint test"})
        body = r.json()
        assert body["subscribed"] is True
        assert "conversation audience" in body["watch"]
        assert "Explicitly subscribe for record changes" in body["watch"]
        assert "GET /api/v1/notifications?unread=true" in body["watch"]
        for retired in ("notifications/watch", "claims/release", "KEEP THE WATCH", "X-Session-Id"):
            assert retired not in body["watch"]

    def test_assignee_notified_on_create_actor_is_not(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            inbox = alice.get("/api/v1/notifications").json()["notifications"]
            assert len(inbox) == 1
            assert inbox[0]["event_type"] == "ticket.created"
            assert inbox[0]["actor"] == "admin"
            assert inbox[0]["ticket_id"] == tid
            # The filer doesn't get woken by their own action
            mine = client.get("/api/v1/notifications").json()["notifications"]
            assert all(n["ticket_id"] != tid for n in mine)

    def test_watch_pages_through_backlog_via_seen_leases(self, client):
        """#539: >=50 unread rows must not pin the watch page forever.

        Pre-fix, watch paged oldest-50 unread ignoring seen_until, so a seat
        with a 50-row unacked backlog received the identical page every cycle
        and NEW rows never surfaced — the subscriber deduped the page, emitted
        nothing, and the seat went silently deaf (4h on rail-unity,
        2026-08-27). The subscriber's own per-page seen-lease must page the
        watcher through the backlog instead.
        """
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            for i in range(59):  # + ticket.created row = 60 unread
                r = client.post(f"/api/v1/tickets/{tid}/comments",
                                json={"body": f"backlog row {i}"})
                assert r.status_code == 200, r.json()
            # Cycle 1: the oldest-50 page.
            page1 = alice.get("/api/v1/notifications/watch?timeout=1") \
                .json()["notifications"]
            assert len(page1) == 50
            # Subscriber behavior: seen-lease what was surfaced.
            r = alice.post("/api/v1/notifications/seen",
                           json={"ids": [n["id"] for n in page1],
                                 "ttl_seconds": 1800})
            assert r.status_code == 200
            # A new row lands behind the backlog.
            client.post(f"/api/v1/tickets/{tid}/comments",
                        json={"body": "the row that used to be unreachable"})
            # Cycle 2: leased rows step aside — the remainder AND the new row
            # surface. Pre-fix this returned the identical 50 ids as page1.
            page2 = alice.get("/api/v1/notifications/watch?timeout=1") \
                .json()["notifications"]
            page1_ids = {n["id"] for n in page1}
            page2_ids = {n["id"] for n in page2}
            assert page2, "watch returned nothing with 11 unleased rows pending"
            assert not (page1_ids & page2_ids), "leased rows re-pinned the page"
            bodies = [(n.get("payload") or {}).get("body", "") for n in page2]
            assert any("unreachable" in b for b in bodies), \
                "new row still starved behind the leased backlog"

    def test_watch_settle_batches_burst_into_one_response(self, client):
        """Debounce (2026-09-01, doc 163 §6): a burst of comments seconds
        apart must be DELIVERED AS ONE BATCH, not raced row-by-row. Every
        crossing-race on #577 (c5235/c5236 13s apart, c5259/c5260) was an
        agent replying between two rows of the same burst. With ?settle=N the
        watch holds its response open N seconds after the first row and
        re-queries, so the second comment joins the same response."""
        import threading
        import time as _t
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            r = client.post(f"/api/v1/tickets/{tid}/comments",
                            json={"body": "burst first"})
            assert r.status_code == 200, r.json()
            res = {}

            def _watch():
                res["r"] = alice.get(
                    f"/api/v1/notifications/watch?timeout=8&settle=3"
                    f"&thread=ticket:{tid}").json()

            th = threading.Thread(target=_watch)
            th.start()
            _t.sleep(1.0)  # inside the 3s settle window
            r = client.post(f"/api/v1/tickets/{tid}/comments",
                            json={"body": "burst second"})
            assert r.status_code == 200, r.json()
            th.join(timeout=15)
            assert "r" in res, "watch call did not return"
            bodies = [(n.get("payload") or {}).get("body", "")
                      for n in res["r"]["notifications"]]
            assert any("burst first" in b for b in bodies), bodies
            assert any("burst second" in b for b in bodies), \
                f"settle window did not batch the burst: {bodies}"

    def test_mention_row_carries_body_and_thread_tail(self, client):
        """v0.8.6 (staging #86): 'being mentioned is the most conversational
        thing that can happen to you, and it was the one delivery that told
        you nothing.' A mention row must carry the triggering text and a short
        oldest-first thread tail, so a pull-in arrives with the conversation."""
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            for i in range(4):
                r = client.post(f"/api/v1/tickets/{tid}/comments",
                                json={"body": f"context {i}"})
                assert r.status_code == 200, r.json()
            r = client.post(f"/api/v1/tickets/{tid}/comments",
                            json={"body": "@alice what do you make of this?"})
            assert r.status_code == 200, r.json()
            rows = alice.get("/api/v1/notifications?unread=true&limit=50") \
                .json()["notifications"]
            mentions = [n for n in rows if n["event_type"] == "ticket.mentioned"]
            assert mentions, rows
            p = mentions[0]["payload"]
            assert "what do you make of this" in (p.get("body") or ""), p
            tail = p.get("thread_tail") or []
            assert len(tail) == 3, tail
            assert [t["body"] for t in tail] == \
                ["context 1", "context 2", "context 3"], tail  # oldest-first

    def test_watch_settle_extends_while_rows_arrive(self, client):
        """v0.8.6 quiet-period debounce: rows arriving INSIDE the window keep
        it open, so a multi-beat burst lands as one batch (the fixed window
        cut same-second groups across two deliveries on staging #86)."""
        import threading
        import time as _t
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "beat 0"})
            res = {}

            def _watch():
                res["r"] = alice.get(
                    f"/api/v1/notifications/watch?timeout=12&settle=2"
                    f"&thread=ticket:{tid}").json()

            th = threading.Thread(target=_watch)
            th.start()
            for i in (1, 2):
                _t.sleep(1.2)  # inside the 2s quiet window each time
                client.post(f"/api/v1/tickets/{tid}/comments",
                            json={"body": f"beat {i}"})
            th.join(timeout=25)
            bodies = [(n.get("payload") or {}).get("body", "")
                      for n in res["r"]["notifications"]]
            for i in range(3):
                assert any(f"beat {i}" in b for b in bodies), (i, bodies)

    def test_watch_settle_zero_disables_batching(self, client):
        """?settle=0 preserves the old return-on-first-row behavior for
        callers that want minimum latency over batch coherence."""
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            r = client.post(f"/api/v1/tickets/{tid}/comments",
                            json={"body": "solo row"})
            assert r.status_code == 200, r.json()
            got = alice.get(f"/api/v1/notifications/watch?timeout=2&settle=0"
                            f"&thread=ticket:{tid}").json()
            bodies = [(n.get("payload") or {}).get("body", "")
                      for n in got["notifications"]]
            assert any("solo row" in b for b in bodies), bodies

    def test_comment_notifies_other_watchers(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            r = alice.post(f"/api/v1/tickets/{tid}/comments",
                           json={"body": "on it"})
            assert r.status_code == 200, r.json()
            assert r.json()["subscribed"] is True
            inbox = client.get("/api/v1/notifications").json()["notifications"]
            commented = [n for n in inbox if n["event_type"] == "ticket.commented"]
            assert len(commented) == 1
            assert commented[0]["actor"] == "alice"
            assert commented[0]["payload"]["body"] == "on it"

    def test_ack_clears_unread(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            inbox = alice.get("/api/v1/notifications").json()["notifications"]
            ids = [n["id"] for n in inbox]
            r = alice.post("/api/v1/notifications/ack", json={"ids": ids})
            assert r.status_code == 200
            assert alice.get("/api/v1/notifications").json()["notifications"] == []
            # still there with unread=false
            all_rows = alice.get("/api/v1/notifications?unread=false").json()["notifications"]
            assert len(all_rows) == 1 and all_rows[0]["read"] is True

    def test_ticket_body_mention_subscribes_and_notifies(self, client):
        """#342: @username in a ticket body → the user is subscribed and gets a
        dedicated ticket.mentioned row (parity with doc/task mentions)."""
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "bob", [])
            bob = AuthedClient(raw, token)
            r = client.post("/api/v1/tickets",
                            json={"summary": "mention body",
                                  "body": "needs eyes @bob please"})
            tid = r.json()["ticket_id"]
            watchers = client.get(f"/api/v1/tickets/{tid}/watchers").json()["watchers"]
            assert "bob" in {w["identity"] for w in watchers}
            inbox = bob.get("/api/v1/notifications").json()["notifications"]
            rows = [n for n in inbox if n["ticket_id"] == tid]
            assert len(rows) == 1, rows
            assert rows[0]["event_type"] == "ticket.mentioned"
            assert rows[0]["actor"] == "admin"
            assert rows[0]["payload"]["by"] == "admin"

    def test_ticket_comment_mention_notifies_once(self, client):
        """#342: @mention in a comment → one ticket.mentioned row (with
        comment_id), and NOT an additional ticket.commented row for the same
        event (skip suppresses the double-delivery)."""
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            alice.post("/api/v1/notifications/ack", json={"all": True})
            r = client.post(f"/api/v1/tickets/{tid}/comments",
                            json={"body": "over to you @alice"})
            cid = r.json()["comment_id"]
            inbox = alice.get("/api/v1/notifications").json()["notifications"]
            rows = [n for n in inbox if n["ticket_id"] == tid]
            assert len(rows) == 1, rows
            assert rows[0]["event_type"] == "ticket.mentioned"
            assert rows[0]["payload"]["comment_id"] == cid

    def test_admin_can_clear_phantom_presence(self, client):
        """#241 family: a dead session's lingering presence blocks that
        identity's cold wakes; admin DELETE /presence/{username} clears it.
        Non-admins cannot clear others' presence."""
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "ghost", [])
            ghost = AuthedClient(raw, token)
            r = ghost.post("/api/v1/presence", json={"ttl_seconds": 3600})
            assert r.json()["present"] is True
            # non-admin cannot clear someone else's presence
            r = ghost.delete("/api/v1/presence/admin")
            assert r.status_code == 403
            # admin busts the phantom
            r = client.delete("/api/v1/presence/ghost")
            assert r.status_code == 200 and r.json()["status"] == "cleared"
            assert ghost.get("/api/v1/presence").json()["present"] is False
            # unknown user 404s
            assert client.delete("/api/v1/presence/no-such-user").status_code == 404

    def test_ticket_unknown_mention_ignored(self, client):
        """#342: a stray @handle that isn't a real user notifies no one and
        doesn't block creation."""
        r = client.post("/api/v1/tickets",
                        json={"summary": "ghost", "body": "ping @no-such-user-xyz"})
        assert r.status_code == 200
        tid = r.json()["ticket_id"]
        watchers = client.get(f"/api/v1/tickets/{tid}/watchers").json()["watchers"]
        assert {w["identity"] for w in watchers} == {"admin"}

    def test_ack_requires_ids_or_all(self, client):
        r = client.post("/api/v1/notifications/ack", json={})
        assert r.status_code == 400

    def test_unsubscribe_stops_notifications(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            alice.post("/api/v1/notifications/ack", json={"all": True})
            r = alice.delete(f"/api/v1/tickets/{tid}/subscribe")
            assert r.status_code == 200
            client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "ping"})
            assert alice.get("/api/v1/notifications").json()["notifications"] == []
            # opt back in works
            alice.post(f"/api/v1/tickets/{tid}/subscribe")
            client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "ping2"})
            inbox = alice.get("/api/v1/notifications").json()["notifications"]
            assert len(inbox) == 1
            assert inbox[0]["payload"]["body"] == "ping2"

    def test_subscribe_requires_ticket_visibility(self, client):
        # #339 made unaddressed tickets commons-readable, so the invisibility
        # this test needs now comes from group_scope containment.
        _create_group(client, "notif-priv", "Notif Priv")
        app = _get_app()
        with TestClient(app) as raw:
            r = client.post("/api/v1/tickets", json={"summary": "contained",
                                                     "group_scope": "notif-priv"})
            tid = r.json()["ticket_id"]
            token, _ = _invite_and_activate(client, raw, "bob", [])
            bob = AuthedClient(raw, token)
            assert bob.post(f"/api/v1/tickets/{tid}/subscribe").status_code == 404   # hidden (doc 261, decision 4)

    def test_watch_returns_unread_immediately(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            r = alice.get("/api/v1/notifications/watch?timeout=1")
            assert r.status_code == 200
            body = r.json()
            assert len(body["notifications"]) == 1
            assert "timed_out" not in body

    def test_watch_times_out_empty(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "bob", [])
            bob = AuthedClient(raw, token)
            r = bob.get("/api/v1/notifications/watch?timeout=1")
            assert r.status_code == 200
            assert r.json()["timed_out"] is True

    def test_assignee_sees_ticket_events_in_recent(self, client):
        """Regression: the old owns()-based event filter missed the assignee
        path, so an assigned agent never saw their ticket's events."""
        app = _get_app()
        with TestClient(app) as raw:
            alice, tid = self._ticket_with_alice(client, raw)
            # Unique body: the event-bus singleton keeps history across tests
            # in this process, so match our event by content, not ticket id.
            marker = "for alice — assignee visibility regression"
            client.post(f"/api/v1/tickets/{tid}/comments", json={"body": marker})
            events = alice.get("/api/v1/events/recent").json()
            commented = [e for e in events if e["type"] == "ticket.commented"
                         and e["data"].get("body") == marker]
            assert len(commented) == 1

    def test_nonparticipant_cannot_see_ticket_events(self, client):
        """Regression: ticket events were emitted unscoped, leaking comment
        snippets to any authenticated token via /events/recent. Post-#339 the
        unaddressed case is commons by design, so the leak check rides a
        group_scoped (contained) ticket."""
        _create_group(client, "notif-priv", "Notif Priv")
        app = _get_app()
        with TestClient(app) as raw:
            r = client.post("/api/v1/tickets", json={"summary": "contained",
                                                     "group_scope": "notif-priv"})
            tid = r.json()["ticket_id"]
            client.post(f"/api/v1/tickets/{tid}/comments",
                        json={"body": "secret detail"})
            token, _ = _invite_and_activate(client, raw, "bob", [])
            bob = AuthedClient(raw, token)
            events = bob.get("/api/v1/events/recent").json()
            assert not [e for e in events if e["type"].startswith("ticket.")]


# ===========================================================================
# Agent Wake (#303 item 1 — schedule-native wake, no ticket/doc/task backing)
# ===========================================================================

class TestAgentWake:
    def test_admin_wakes_agent(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "roundup-target", [])
            target = AuthedClient(raw, token)

            work = client.post("/api/v1/tickets",
                               json={"summary": "the work", "body": "b"}).json()
            r = client.post("/api/v1/agents/roundup-target/wake",
                            json={"summary": "digest ready", "body": "the full digest",
                                  "thread_id": work["thread_id"]})
            assert r.status_code == 200, r.json()
            assert r.json() == {"status": "sent", "recipient": "roundup-target",
                                "event_type": "schedule.fired"}

            inbox = target.get("/api/v1/notifications").json()["notifications"]
            assert len(inbox) == 1
            assert inbox[0]["event_type"] == "schedule.fired"
            assert inbox[0]["actor"] == "admin"
            # 0.12.19: the row carries the thread and its root, so whoever is
            # woken has somewhere to answer. It used to carry neither.
            assert inbox[0]["thread_id"] == work["thread_id"]
            assert inbox[0]["ticket_id"] == work["id"]
            assert inbox[0]["payload"] == {"summary": "digest ready", "body": "the full digest"}

    def test_wake_custom_event_type(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "custom-target", [])
            target = AuthedClient(raw, token)
            work = client.post("/api/v1/tickets", json={"summary": "w", "body": "b"}).json()
            # An event type the rail has never heard of is refused (0.12.19):
            # with a body it would classify as a MESSAGE on `_is_message`'s
            # body arm and birth a holder on a word nothing can judge.
            r = client.post("/api/v1/agents/custom-target/wake",
                            json={"event_type": "agent.nudge", "summary": "hi",
                                  "thread_id": work["thread_id"]})
            assert r.status_code == 400 and "event_type must be one of" in r.json()["detail"]
            r = client.post("/api/v1/agents/custom-target/wake",
                            json={"event_type": "ticket.mentioned", "summary": "hi",
                                  "thread_id": work["thread_id"]})
            assert r.status_code == 200, r.json()
            inbox = target.get("/api/v1/notifications").json()["notifications"]
            assert inbox[0]["event_type"] == "ticket.mentioned"

    def test_a_wake_must_name_the_work_it_is_about(self, client, tmp_db):
        """doc 239: work arrives wrapped in a thread. A wake with none is a
        message with no work behind it and nowhere to answer — 524 such rows
        were in the inbox when this was written, every one of them objectless."""
        r = client.post("/api/v1/agents/anyone/wake", json={"summary": "hi"})
        assert r.status_code == 422, r.text          # thread_id is required
        r = client.post("/api/v1/agents/anyone/wake",
                        json={"summary": "hi", "thread_id": 999999})
        assert r.status_code in (400, 404)

    def test_wake_unknown_agent_404(self, client):
        work = client.post("/api/v1/tickets", json={"summary": "w", "body": "b"}).json()
        r = client.post("/api/v1/agents/nobody-here/wake",
                        json={"summary": "hi", "thread_id": work["thread_id"]})
        assert r.status_code == 404


# ===========================================================================
# Task scheduler (doc 161 §5, task 596 — the timer half of #303)
# ===========================================================================

class TestTaskClose:
    """doc 174 / Phase C: a task thread lives while the task is open, so its
    `done` / `cancelled` must reach the ASSIGNEE's inbox (the rail delivers it
    as the wrap-up). Project watchers are opt-in and assignment does not
    watch, so before 0.8.18 a task holder never heard its own task close."""

    def _task_for(self, client, raw, assignee):
        slug = client.post("/api/v1/projects", json={"name": f"close {assignee}"}).json()["slug"]
        r = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "wrap me", "assignee": assignee})
        assert r.status_code == 200, r.json()
        return r.json()["id"]

    def _rows(self, who, tid, status):
        inbox = who.get("/api/v1/notifications").json()["notifications"]
        return [n for n in inbox if n["task_id"] == tid and n["event_type"] == "task.updated"
                and n["payload"].get("status") == status]

    def test_done_reaches_the_assignee_once(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "task-holder", [])
            holder = AuthedClient(raw, token)
            tid = self._task_for(client, raw, "task-holder")
            client.patch(f"/api/v1/tasks/{tid}", json={"status": "in_progress"})
            assert len(self._rows(holder, tid, "in_progress")) == 1  # responsible recipient
            client.patch(f"/api/v1/tasks/{tid}", json={"status": "done"})
            rows = self._rows(holder, tid, "done")
            assert len(rows) == 1 and rows[0]["actor"] == "admin"
            # Marking it done again is not a second close.
            client.patch(f"/api/v1/tasks/{tid}", json={"status": "done"})
            assert len(self._rows(holder, tid, "done")) == 1

    def test_own_close_never_rings_the_closer(self, client, tmp_db):
        # The assignee closes its own task from an interactive session — with or
        # without a rail holder of its own on the task, the actor gets no row
        # (0.9.1: your own writes never ring you; the held instance sees the
        # object's state on its next message, as any peer would).
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "self-closer", [])
            me = AuthedClient(raw, token)
            tid = self._task_for(client, raw, "self-closer")
            me.patch(f"/api/v1/tasks/{tid}", json={"status": "cancelled"},
                     headers={"X-Session-Id": "interactive-1"})
            assert self._rows(me, tid, "cancelled") == []
            tid2 = self._task_for(client, raw, "self-closer")
            r = me.post("/api/v1/wake/threads", json={"kind": "task", "object_id": tid2,
                                                      "conversation_id": "holder-sid", "holder": "stream", "pid": 77})
            assert r.status_code == 200, r.text
            me.patch(f"/api/v1/tasks/{tid2}", json={"status": "done"},
                     headers={"X-Session-Id": "interactive-1"})
            # 0.9.1: your own writes never ring you — not even a held instance
            # under another conversation (the self rule that birthed twins, doc 221).
            assert self._rows(me, tid2, "done") == []


class TestDocSelfRule:
    def test_doc_comment_reaches_existing_sibling_not_writer(self, client, tmp_db):
        # Unknown/self writes stay quiet; a stamped sibling write has one target.
        did = client.post("/api/v1/docs", json={"title": "mine", "body": "b"}).json()["id"]
        def rows():
            inbox = client.get("/api/v1/notifications").json()["notifications"]
            return [n for n in inbox if n.get("doc_id") == did and n["event_type"] == "doc.commented"]
        client.post(f"/api/v1/docs/{did}/comments", json={"body": "note to self"},
                    headers={"X-Session-Id": "interactive-1"})
        assert rows() == []                                   # no held instance: own write, no row
        r = client.post("/api/v1/wake/threads", json={"kind": "doc", "object_id": did,
                                                      "conversation_id": "holder-sid", "holder": "stream", "pid": 9})
        assert r.status_code == 200, r.text
        client.post(f"/api/v1/docs/{did}/comments", json={"body": "brief for my holder"},
                    headers={"X-Session-Id": "interactive-1"})
        assert len(rows()) == 1
        assert rows()[0]['payload']['recipient_session'] == _session_fp_expected('holder-sid')
        client.post(f"/api/v1/docs/{did}/comments", json={"body": "the holder's own reply"},
                    headers={"X-Session-Id": "holder-sid"})
        assert len(rows()) == 1                               # holder's own echo adds no row


class TestSelfRuleFilerBinding:
    def test_interactive_comment_targets_existing_filer_conversation(self, client, tmp_db):
        # The receiver already exists; this write cannot birth a twin.
        tid = client.post("/api/v1/tickets", json={"summary": "filed by my holder", "body": "b"},
                          headers={"X-Session-Id": "holder-sid"}).json()["id"]
        def mine():
            inbox = client.get("/api/v1/notifications").json()["notifications"]
            return [n for n in inbox if n.get("ticket_id") == tid and n["event_type"] == "ticket.commented"]
        client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "from the terminal"},
                    headers={"X-Session-Id": "interactive-1"})
        assert len(mine()) == 1
        assert mine()[0]['payload']['recipient_session'] == _session_fp_expected('holder-sid')
        client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "the holder's own reply"},
                    headers={"X-Session-Id": "holder-sid"})
        assert len(mine()) == 1                               # holder's own echo adds no row


class TestFingerprintOnDocAndTaskRows:
    def test_doc_and_task_rows_carry_the_writer_fingerprint(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "fp-peer", [])
            peer = AuthedClient(raw, token)
            did = client.post("/api/v1/docs", json={"title": "fp", "body": "b"}).json()["id"]
            peer.post(f"/api/v1/docs/{did}/comments", json={"body": "hi"}, headers={"X-Session-Id": "peer-conv"})
            inbox = client.get("/api/v1/notifications").json()["notifications"]
            row = next(n for n in inbox if n.get("doc_id") == did and n["event_type"] == "doc.commented")
            assert len(row["payload"].get("actor_session") or "") == 8
            slug = client.post("/api/v1/projects", json={"name": "fp proj"}).json()["slug"]
            tid = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "t"}).json()["id"]
            peer.post(f"/api/v1/tasks/{tid}/comments", json={"body": "hi"}, headers={"X-Session-Id": "peer-conv"})
            inbox = client.get("/api/v1/notifications").json()["notifications"]
            row = next(n for n in inbox if n.get("task_id") == tid and n["event_type"] == "task.commented")
            assert len(row["payload"].get("actor_session") or "") == 8


class TestDocCreateBinds:
    def test_doc_create_with_session_binds_the_author(self, client, tmp_db):
        did = client.post("/api/v1/docs", json={"title": "mine", "body": "b"},
                          headers={"X-Session-Id": "conv-author"}).json()["id"]
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        mine = next(x for x in rows if x["kind"] == "doc" and x["object_id"] == did)
        assert mine["conversation_id"] == "conv-author" and mine["holder"] == "conversation"
        did2 = client.post("/api/v1/docs", json={"title": "unbound", "body": "b"}).json()["id"]
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        assert not any(x["kind"] == "doc" and x["object_id"] == did2 for x in rows)


class TestTaskCreateBinds:
    def test_task_create_with_session_binds_the_creator(self, client, tmp_db):
        # "threads you start are yours" holds for tasks as for tickets (#105):
        # the creating conversation is bound so replies route back into it.
        slug = client.post("/api/v1/projects", json={"name": "bind proj"}).json()["slug"]
        tid = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "t"},
                          headers={"X-Session-Id": "conv-creator"}).json()["id"]
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        mine = next(x for x in rows if x["kind"] == "task" and x["object_id"] == tid)
        assert mine["conversation_id"] == "conv-creator" and mine["holder"] == "conversation"
        # Without the header nothing is bound (no nonce, no binding).
        tid2 = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "u"}).json()["id"]
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        assert not any(x["kind"] == "task" and x["object_id"] == tid2 for x in rows)


class TestTaskScheduler:
    def _make_scheduled(self, client, raw, assignee, scheduled_at, recurrence=None):
        slug = client.post("/api/v1/projects", json={"name": f"sched {assignee} {recurrence}"}).json()["slug"]
        body = {"title": "morning minutes", "assignee": assignee,
                "scheduled_at": scheduled_at}
        if recurrence:
            body["recurrence"] = recurrence
        r = client.post(f"/api/v1/projects/{slug}/tasks", json=body)
        assert r.status_code == 200, r.json()
        return r.json()["id"]

    def test_recurring_fires_into_inbox_and_rearms(self, client, tmp_db):
        import main
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "sched-target", [])
            target = AuthedClient(raw, token)
            past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            tid = self._make_scheduled(client, raw, "sched-target", past, "daily")

            assert main._run_task_scheduler_pass() == 1
            inbox = target.get("/api/v1/notifications").json()["notifications"]
            fired = [n for n in inbox if n["event_type"] == "schedule.fired"]
            assert len(fired) == 1
            assert fired[0]["task_id"] == tid
            assert fired[0]["actor"] == "registry"
            assert "morning minutes" in fired[0]["payload"]["summary"]

            task = client.get(f"/api/v1/tasks/{tid}").json()
            nxt = datetime.fromisoformat(task["scheduled_at"])
            assert nxt > datetime.now(timezone.utc)          # re-armed to the future
            assert task["last_fired_at"] is not None
            assert main._run_task_scheduler_pass() == 0      # nothing due now

    def test_one_shot_clears_after_firing(self, client, tmp_db):
        import main
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "oneshot-target", [])
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            tid = self._make_scheduled(client, raw, "oneshot-target", past)
            assert main._run_task_scheduler_pass() == 1
            task = client.get(f"/api/v1/tasks/{tid}").json()
            assert task["scheduled_at"] is None
            assert main._run_task_scheduler_pass() == 0

    def test_bad_recurrence_rejected(self, client, tmp_db):
        slug = client.post("/api/v1/projects", json={"name": "sched bad"}).json()["slug"]
        r = client.post(f"/api/v1/projects/{slug}/tasks",
                        json={"title": "x", "scheduled_at": "2027-01-01T09:00:00",
                              "recurrence": "fortnightly"})
        assert r.status_code == 422

    def test_wake_requires_admin(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "caller", [])
            caller = AuthedClient(raw, token)
            r = caller.post("/api/v1/agents/caller/wake", json={"summary": "hi"})
            assert r.status_code == 403

    def test_wake_actor_isolation(self, client, tmp_db):
        """Waking bob doesn't drop anything in alice's or the caller's own inbox."""
        app = _get_app()
        with TestClient(app) as raw:
            _invite_and_activate(client, raw, "alice", [])
            token, _ = _invite_and_activate(client, raw, "bob", [])
            bob = AuthedClient(raw, token)
            work = client.post("/api/v1/tickets", json={"summary": "w", "body": "b"}).json()
            client.post("/api/v1/agents/bob/wake",
                        json={"summary": "hi bob", "thread_id": work["thread_id"]})
            assert client.get("/api/v1/notifications").json()["notifications"] == []
            assert len(bob.get("/api/v1/notifications").json()["notifications"]) == 1


class TestOperatorTime:
    """Operator wall-clock (#384): agents reason in the operator's local time."""

    def test_verify_includes_operator_wall_clock(self, client):
        r = client.get("/api/v1/auth/verify")
        assert r.status_code == 200
        op = r.json().get("operator_time")
        assert op is not None
        assert op["tz"]                      # OPERATOR_TZ, else UTC
        assert op["human"] and op["iso"]     # zoneinfo resolved


class TestDocHierarchy:
    """Doc groups / parent-sub-docs (v0.7.71, structure directive)."""

    def test_child_create_filter_and_children_listing(self, client):
        p = client.post("/api/v1/docs", json={"title": "Parent overview", "body": "# top"}).json()
        c = client.post("/api/v1/docs", json={"title": "Fiddly gotchas", "body": "# sub",
                                              "parent_doc_id": p["doc_id"]}).json()
        got = client.get(f"/api/v1/docs/{p['doc_id']}").json()
        kids = [k["id"] for k in got["children"]]
        assert c["doc_id"] in kids
        listed = client.get(f"/api/v1/docs?parent_id={p['doc_id']}").json()["docs"]
        assert [d["id"] for d in listed] == [c["doc_id"]]

    def test_cycle_guard_and_clear(self, client):
        a = client.post("/api/v1/docs", json={"title": "A node", "body": "a"}).json()["doc_id"]
        b = client.post("/api/v1/docs", json={"title": "B node", "body": "b",
                                              "parent_doc_id": a}).json()["doc_id"]
        r = client.patch(f"/api/v1/docs/{a}", json={"parent_doc_id": b})
        assert r.status_code == 400  # would create a cycle
        r2 = client.patch(f"/api/v1/docs/{b}", json={"parent_doc_id": 0})
        assert r2.status_code == 200  # 0 clears
        assert client.get(f"/api/v1/docs/{a}").json()["children"] == []

    def test_unknown_parent_400(self, client):
        r = client.post("/api/v1/docs", json={"title": "Orphan wannabe", "body": "x",
                                              "parent_doc_id": 999999})
        assert r.status_code == 400


class TestReplayGuard:
    """Comment replay tripwire (v0.7.71, the #363 fossil: 4 posts, 3 identities)."""

    LONG = "Replay fossil body. " * 20  # > 200 chars

    def test_identical_long_body_409s_same_thread(self, client, tmp_db):
        t = client.post("/api/v1/tickets", json={"type": "bug", "summary": "replay host",
                                                 "body": "host"}).json()["ticket_id"]
        assert client.post(f"/api/v1/tickets/{t}/comments", json={"body": self.LONG}).status_code == 200
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "other-agent", [])
            other = AuthedClient(raw, token)
            r = other.post(f"/api/v1/tickets/{t}/comments", json={"body": self.LONG})
            assert r.status_code == 409
            assert "replay-guard" in r.json()["detail"]

    def test_short_conventional_phrases_stay_free(self, client):
        t = client.post("/api/v1/tickets", json={"type": "bug", "summary": "short host",
                                                 "body": "host"}).json()["ticket_id"]
        for _ in range(2):
            assert client.post(f"/api/v1/tickets/{t}/comments",
                               json={"body": "picked up, working"}).status_code == 200

    def test_same_body_different_thread_ok(self, client):
        t1 = client.post("/api/v1/tickets", json={"type": "bug", "summary": "h1", "body": "x"}).json()["ticket_id"]
        t2 = client.post("/api/v1/tickets", json={"type": "bug", "summary": "h2", "body": "x"}).json()["ticket_id"]
        assert client.post(f"/api/v1/tickets/{t1}/comments", json={"body": self.LONG}).status_code == 200
        assert client.post(f"/api/v1/tickets/{t2}/comments", json={"body": self.LONG}).status_code == 200


class TestClaimPlanes:
    """#392: claims widen to doc/task threads — same #199/#264 machinery,
    wider predicate. A live surface working a doc or task keeps its own
    cold rail off that thread, exactly as ticket writes always did."""

    def _peer(self, client, raw, name="claim-peer"):
        token, _ = _invite_and_activate(client, raw, name, [])
        return AuthedClient(raw, token)

    def test_doc_comment_claims_and_hides_from_cold_rail(self, client, tmp_db):
        doc_id = client.post("/api/v1/docs", json={"title": "claim host",
                                                   "body": "b"}).json()["doc_id"]
        # Admin engages the doc thread (auto-claim on write, #392).
        assert client.post(f"/api/v1/docs/{doc_id}/comments",
                           json={"body": "working this"}).status_code == 200
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw)
            peer.post(f"/api/v1/docs/{doc_id}/comments", json={"body": "reply"})
        # The doc.commented row for admin is claimed → invisible to the cold rail.
        rows = client.get("/api/v1/notifications?unread=true&unclaimed=true"
                          ).json()["notifications"]
        assert not any(r.get("doc_id") == doc_id for r in rows)
        rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
        mine = [r for r in rows if r.get("doc_id") == doc_id]
        assert mine and all(r["claimed"] for r in mine)
        # Targeted release → the cold rail takes over instantly.
        assert client.delete(f"/api/v1/docs/{doc_id}/claim").status_code == 200
        rows = client.get("/api/v1/notifications?unread=true&unclaimed=true"
                          ).json()["notifications"]
        assert any(r.get("doc_id") == doc_id for r in rows)

    def test_task_patch_claims_thread(self, client, tmp_db):
        slug = client.post("/api/v1/projects", json={"name": "claim proj"}).json()["slug"]
        task_id = client.post(f"/api/v1/projects/{slug}/tasks",
                              json={"title": "deliverable"}).json()["task_id"]
        # A status PATCH is engagement — the exact #392 race shape.
        r = client.patch(f"/api/v1/tasks/{task_id}", json={"status": "in_progress"})
        assert r.status_code == 200 and r.json()["claim_owned"] is True
        claims = client.get("/api/v1/wake/claims").json()["claims"]
        assert any(c.get("task_id") == task_id for c in claims)
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "claim-peer-2")
            peer.post(f"/api/v1/tasks/{task_id}/comments", json={"body": "q?"})
        rows = client.get("/api/v1/notifications?unread=true&unclaimed=true"
                          ).json()["notifications"]
        assert not any(r.get("task_id") == task_id for r in rows)

    def test_explicit_doc_claim_excludes_parallel_session(self, client, tmp_db):
        doc_id = client.post("/api/v1/docs", json={"title": "sess host",
                                                   "body": "b"}).json()["doc_id"]
        r = client.post(f"/api/v1/docs/{doc_id}/claim", json={"ttl_seconds": 60},
                        headers={"X-Session-Id": "sess-a"})
        assert r.status_code == 200 and r.json()["sticky"] is True
        # A parallel live session of the SAME identity is refused (#264).
        r = client.post(f"/api/v1/docs/{doc_id}/claim", json={"ttl_seconds": 60},
                        headers={"X-Session-Id": "sess-b"})
        assert r.status_code == 409
        # Takeover path: targeted release, then claim.
        client.delete(f"/api/v1/docs/{doc_id}/claim")
        r = client.post(f"/api/v1/docs/{doc_id}/claim", json={"ttl_seconds": 60},
                        headers={"X-Session-Id": "sess-b"})
        assert r.status_code == 200

    def test_doc_revision_claims_and_release_all_clears(self, client, tmp_db):
        doc_id = client.post("/api/v1/docs", json={"title": "rev host",
                                                   "body": "b"}).json()["doc_id"]
        r = client.post(f"/api/v1/docs/{doc_id}/revisions",
                        json={"body": "b2", "note": "edit"})
        assert r.status_code == 200 and r.json()["claim_owned"] is True
        slug = client.post("/api/v1/projects", json={"name": "rel proj"}).json()["slug"]
        task_id = client.post(f"/api/v1/projects/{slug}/tasks",
                              json={"title": "t"}).json()["task_id"]
        client.post(f"/api/v1/tasks/{task_id}/claim", json={"ttl_seconds": 60})
        claims = client.get("/api/v1/wake/claims").json()["claims"]
        assert any(c.get("doc_id") == doc_id for c in claims)
        assert any(c.get("task_id") == task_id for c in claims)
        # Identity-wide release with include_sticky covers every plane.
        client.post("/api/v1/wake/claims/release", json={"include_sticky": True})
        assert client.get("/api/v1/wake/claims").json()["claims"] == []


class TestHolderRead:
    """#529: no seat could see who else held a row. `?all=true` narrowed
    SILENTLY for non-admins — a self-view served under the fleet view's exact
    shape, so "is anyone else in here?" answered "no" from a query that could
    not have said otherwise. Three identities held ticket 526 concurrently
    while this was true."""

    def _peer(self, client, raw, name="holder-peer"):
        token, _ = _invite_and_activate(client, raw, name, [])
        return AuthedClient(raw, token)

    def test_non_admin_all_true_is_refused_not_narrowed(self, client, tmp_db):
        tid = client.post("/api/v1/tickets", json={
            "type": "bug", "priority": "low", "summary": "held row",
            "body": "x"}).json()["ticket_id"]
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "holder-peer")
            peer.post(f"/api/v1/tickets/{tid}/comments", json={"body": "mine too"})
            # The old behaviour: 200 + the caller's own rows, indistinguishable
            # from a fleet-wide answer. Now refused, and the refusal names the
            # read that DOES answer the question.
            r = peer.get("/api/v1/wake/claims?all=true")
            assert r.status_code == 403
            assert "?thread=" in r.json()["detail"]
            # Self-scope still works, and now says which scope it is.
            r = peer.get("/api/v1/wake/claims")
            assert r.status_code == 200 and r.json()["scope"] == "self"
            assert all(c["identity"] == "holder-peer" for c in r.json()["claims"])
        assert client.get("/api/v1/wake/claims?all=true").json()["scope"] == "fleet"

    def test_thread_scope_shows_other_identities_to_a_plain_seat(self, client, tmp_db):
        tid = client.post("/api/v1/tickets", json={
            "type": "bug", "priority": "low", "summary": "shared row",
            "body": "x"}, headers={"X-Session-Id": "admin-sess"}).json()["ticket_id"]
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "thread-peer")
            peer.post(f"/api/v1/tickets/{tid}/comments", json={"body": "also here"},
                      headers={"X-Session-Id": "peer-sess"})
            r = peer.get(f"/api/v1/wake/claims?thread=ticket:{tid}")
            assert r.status_code == 200
            body = r.json()
            assert body["scope"] == f"thread:ticket:{tid}"
            assert body["advisory"] is True and body["note"]
            holders = {h["identity"]: h for h in body["holders"]}
            # The whole point: a plain seat sees the OTHER identity.
            assert set(holders) == {"admin", "thread-peer"}
            assert holders["thread-peer"]["self"] is True
            assert holders["admin"]["self"] is False
            # Session fingerprints, never the nonce (#264 projection unchanged).
            assert holders["admin"]["session"] == _session_fp_expected("admin-sess")
            assert "admin-sess" not in r.text and "peer-sess" not in r.text

    def test_thread_scope_covers_docs_and_tasks(self, client, tmp_db):
        doc_id = client.post("/api/v1/docs", json={"title": "held doc",
                                                   "body": "b"}).json()["doc_id"]
        # NB: doc/task CREATE does not claim (only ticket create does) — a
        # revision is the doc-plane write that engages the thread.
        client.post(f"/api/v1/docs/{doc_id}/revisions", json={"body": "b2"})
        slug = client.post("/api/v1/projects", json={"name": "held proj"}).json()["slug"]
        task_id = client.post(f"/api/v1/projects/{slug}/tasks",
                              json={"title": "t"}).json()["task_id"]
        client.patch(f"/api/v1/tasks/{task_id}", json={"status": "in_progress"})
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "plane-peer")
            d = peer.get(f"/api/v1/wake/claims?thread=doc:{doc_id}").json()
            assert [h["identity"] for h in d["holders"]] == ["admin"]
            t = peer.get(f"/api/v1/wake/claims?thread=task:{task_id}").json()
            assert [h["identity"] for h in t["holders"]] == ["admin"]

    def test_malformed_thread_is_400_not_ignored(self, client, tmp_db):
        # The bug class this whole ticket is about: an unrecognised narrowing
        # answered confidently instead of refusing. FastAPI drops unknown query
        # params, so `?thread=` used to be accepted and silently ignored.
        for bad in ("ticket526", "ticket:", "ticket:abc", "issue:526", ""):
            r = client.get(f"/api/v1/wake/claims?thread={bad}")
            assert r.status_code == 400, bad
        assert client.get("/api/v1/wake/claims?thread=ticket:999999").status_code == 404

    def test_thread_scope_respects_visibility(self, client, tmp_db):
        client.post("/api/v1/groups", json={"id": "secret-grp", "name": "Secret"})
        tid = client.post("/api/v1/tickets", json={
            "type": "bug", "priority": "low", "summary": "scoped row",
            "body": "x", "group_scope": "secret-grp"}).json()["ticket_id"]
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "outsider")
            # No holder disclosure for a row you cannot read.
            assert peer.get(f"/api/v1/wake/claims?thread=ticket:{tid}").status_code == 404   # hidden (doc 261, decision 4)

    def test_a_thread_has_no_scope_its_objects_do(self, client, tmp_db):
        """0.12.76. #884 (personal) rooted thread 1770; #903 (services) inside it
        was the services seat's, and the seat could read neither the thread nor
        post its binding, so its first create started thread 1782 (doc 355).
        A thread has no scope; objects handle scope, and the thread only decides
        wake and routing. The read answers anyone; a member you cannot read is
        a bare ref; a hidden root lends nothing but its ref; the binding posts."""
        client.post("/api/v1/groups", json={"id": "secret-grp", "name": "Secret"})
        root = client.post("/api/v1/tickets", json={
            "type": "bug", "priority": "low", "summary": "private root",
            "body": "x", "group_scope": "secret-grp"}).json()
        t, rid = root["thread_id"], root["ticket_id"]
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "member-peer")
            r = peer.get(f"/api/v1/threads/{t}")                             # on nothing in it: still routing, not content
            assert r.status_code == 200
            b = r.json()
            assert b["root_hidden"] is True and b["title"] == f"thread {t}"
            assert b["members"]["tickets"] == [{"id": rid, "hidden": True}]
            assert b["hidden_members"] == 1 and "private root" not in r.text
            assert peer.get(f"/api/v1/tickets/{rid}").status_code == 404     # the object keeps its door
            member = client.post("/api/v1/tickets", json={
                "type": "bug", "priority": "low", "summary": "shared member", "body": "y"},
                headers={"X-Thread-Id": str(t)}).json()
            assert member["thread_id"] == t
            b = peer.get(f"/api/v1/threads/{t}").json()
            assert [m["id"] for m in b["members"]["tickets"]] == [rid, member["ticket_id"]]
            assert b["members"]["tickets"][1]["title"] == "shared member"
            assert b["members"]["tickets"][0] == {"id": rid, "hidden": True}
            assert b["hidden_members"] == 1 and b["open_members"] is True   # the thread's own fact
            # The binding is routing: it posts on a thread whose root the seat cannot read.
            rb = peer.post("/api/v1/wake/threads", json={"thread_id": t, "conversation_id": "peer-holder-1",
                                                         "holder": "stream", "state": "open"})
            assert rb.status_code == 200, rb.text
            assert any(x["identity"] == "member-peer" for x in peer.get(f"/api/v1/threads/{t}").json()["bindings"])
        whole = client.get(f"/api/v1/threads/{t}").json()                   # admin reads both
        assert whole["root_hidden"] is False and whole["hidden_members"] == 0
        assert len(whole["members"]["tickets"]) == 2 and whole["title"] == "private root"

    def test_holder_hint_on_the_three_reads(self, client, tmp_db):
        tid = client.post("/api/v1/tickets", json={
            "type": "bug", "priority": "low", "summary": "hinted",
            "body": "x"}).json()["ticket_id"]
        doc_id = client.post("/api/v1/docs", json={"title": "hinted doc",
                                                   "body": "b"}).json()["doc_id"]
        client.post(f"/api/v1/docs/{doc_id}/revisions", json={"body": "b2"})
        slug = client.post("/api/v1/projects", json={"name": "hint proj"}).json()["slug"]
        task_id = client.post(f"/api/v1/projects/{slug}/tasks",
                              json={"title": "t"}).json()["task_id"]
        client.post(f"/api/v1/tasks/{task_id}/comments", json={"body": "engaged"})
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "hint-peer")
            for path, holder in ((f"/api/v1/tickets/{tid}", "admin"),
                                 (f"/api/v1/docs/{doc_id}", "admin"),
                                 (f"/api/v1/tasks/{task_id}", "admin")):
                c = peer.get(path).json()["claims"]
                assert c["advisory"] is True and c["note"]
                assert [h["identity"] for h in c["holders"]] == [holder], path
                assert c["holders"][0]["self"] is False
        # Present-and-empty once the holder releases — absence must be legible,
        # and a missing key would be indistinguishable from an old server.
        client.post("/api/v1/wake/claims/release", json={"include_sticky": True})
        c = client.get(f"/api/v1/tickets/{tid}").json()["claims"]
        assert c["holders"] == [] and c["advisory"] is True

    def test_ticket_patch_claims_its_thread(self, client, tmp_db):
        # c3977: the only mutating route on any plane that claimed nothing —
        # so triage, the main verb here, left no trace for the hint to show.
        tid = client.post("/api/v1/tickets", json={
            "type": "bug", "priority": "low", "summary": "triaged",
            "body": "x"}).json()["ticket_id"]
        client.post("/api/v1/wake/claims/release", json={"include_sticky": True})
        assert client.get(f"/api/v1/wake/claims?thread=ticket:{tid}"
                          ).json()["holders"] == []
        r = client.patch(f"/api/v1/tickets/{tid}", json={"priority": "high"},
                         headers={"X-Session-Id": "triage-sess"})
        assert r.status_code == 200 and r.json()["claim_owned"] is True
        holders = client.get(f"/api/v1/wake/claims?thread=ticket:{tid}"
                             ).json()["holders"]
        assert [h["identity"] for h in holders] == ["admin"]
        assert holders[0]["session"] == _session_fp_expected("triage-sess")


class TestCommentFeed:
    """GET /comments — cross-plane watermark feed (#424: prose is write-once
    until something can re-read it cheaply)."""

    def test_author_since_filter_and_watermark(self, client, tmp_db):
        t = client.post("/api/v1/tickets", json={"type": "question", "summary": "feed host",
                                                 "body": "x"}).json()["ticket_id"]
        d = client.post("/api/v1/docs", json={"title": "feed doc", "body": "b"}).json()["doc_id"]
        client.post(f"/api/v1/tickets/{t}/comments", json={"body": "fact one: budget 50k"})
        client.post(f"/api/v1/docs/{d}/comments", json={"body": "fact two: quota 20k"})
        r = client.get("/api/v1/comments?author=admin").json()
        bodies = [c["body"] for c in r["comments"]]
        assert "fact one: budget 50k" in bodies and "fact two: quota 20k" in bodies
        planes = {c["plane"] for c in r["comments"]}
        assert {"ticket", "doc"} <= planes
        # watermark iteration: strict > from returned watermark yields nothing new
        r2 = client.get(f"/api/v1/comments?author=admin&since={r['watermark']}").json()
        assert r2["comments"] == [] and r2["more"] is False

    def test_plane_filter(self, client, tmp_db):
        t = client.post("/api/v1/tickets", json={"type": "question", "summary": "pf host",
                                                 "body": "x"}).json()["ticket_id"]
        client.post(f"/api/v1/tickets/{t}/comments", json={"body": "ticket-plane row"})
        r = client.get("/api/v1/comments?plane=doc").json()
        assert all(c["plane"] == "doc" for c in r["comments"])
        assert client.get("/api/v1/comments?plane=bogus").status_code == 400

    def test_visibility_scoped_caller(self, client, tmp_db):
        # group-scoped ticket: comment visible to admin, absent for an outsider,
        # but the outsider's watermark still advances past it.
        _create_group(client, "vis-group", "Vis Group")
        t = client.post("/api/v1/tickets", json={"type": "question", "summary": "vis host",
                                                 "body": "x", "group_scope": "vis-group"}
                        ).json()["ticket_id"]
        client.post(f"/api/v1/tickets/{t}/comments", json={"body": "scoped fact"})
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "feed-outsider", [])
            outsider = AuthedClient(raw, token)
            r = outsider.get("/api/v1/comments").json()
            assert not any(c["body"] == "scoped fact" for c in r["comments"])
            assert r["watermark"] is not None  # advanced over the hidden row
        r = client.get("/api/v1/comments").json()
        assert any(c["body"] == "scoped fact" for c in r["comments"])

    def test_unknown_author_is_loud_not_silent(self, client, tmp_db):
        # producer's #424 live-verify: ?author=alice returned silent zero (handle
        # is 'admin'). An author matching no user and no comment ever must warn.
        r = client.get("/api/v1/comments?author=alice").json()
        assert r["comments"] == [] and r.get("author_unknown") is True
        assert "admin" in r.get("warning", "")
        # a real-but-quiet author stays a plain zero (no false alarm)
        app = _get_app()
        with TestClient(app) as raw:
            _invite_and_activate(client, raw, "quiet-agent", [])
        r = client.get("/api/v1/comments?author=quiet-agent").json()
        assert r["comments"] == [] and "author_unknown" not in r


# ===========================================================================
# Wake capability leg (#521) — dark-watch measured connection, not capability
# ===========================================================================

class TestWakeSessionOutcomes:
    """claude-gpu-1 connected healthily for 23 days while every launch died on
    `Not logged in`, and its registration row read fully green throughout.
    These tests pin the axis that gap was on: the handler reports every
    ATTEMPT with a CAUSE CLASS, and a connected seat that cannot launch goes
    dark — while a connected seat that is merely rate-limited does not."""

    def _register(self, client, handler_version="2.4.6", host="testbox"):
        r = client.post("/api/v1/wake/register",
                        json={"wake_cmd": "/bin/samcloud-wake", "host": host,
                              "listener_version": "2.5.3",
                              "handler_version": handler_version,
                              "max_conn_age": 21600})
        assert r.status_code == 200
        return r

    def _row(self, client, username="admin"):
        rows = client.get("/api/v1/wake/registrations").json()["registrations"]
        return next(r for r in rows if r["username"] == username)

    def _report(self, client, outcome, rc=0, cause=None, error=None):
        body = {"outcome": outcome, "rc": rc}
        if cause:
            body["cause"] = cause
        if error:
            body["error"] = error
        return client.post("/api/v1/wake/session", json=body)

    def test_ok_and_failed_attempts_are_counted_separately(self, client, tmp_db):
        self._register(client)
        r = self._report(client, "ok")
        assert r.status_code == 200 and r.json()["attempts"] == 1
        self._report(client, "failed", rc=1, cause="auth")
        row = self._row(client)
        assert row["session_attempts"] == 2
        assert row["session_successes"] == 1
        assert row["consecutive_session_failures"] == 1
        assert row["last_session_ok_at"] and row["last_session_rc"] == 1
        assert row["last_session_cause"] == "auth"

    def test_n_consecutive_failures_darkens_a_connected_seat(self, client, tmp_db):
        self._register(client)
        for _ in range(3):
            self._report(client, "failed", rc=1, cause="auth")
        row = self._row(client)
        # The inversion that is the whole point: connected, fresh, and dark.
        assert row["dark"] is True and row["dark_reason"] == "failing-launches"
        assert row["last_connect_at"] is not None
        # One success clears it — recovery is a transition the channel carries.
        self._report(client, "ok")
        row = self._row(client)
        assert row["dark"] is False and row["consecutive_session_failures"] == 0

    def test_quota_failures_never_darken_a_healthy_seat(self, client, tmp_db):
        """The case that would have burned this leg's credibility on its first
        firing: one live seat has 463 of 464 failures in this class, with a
        longest run of 234 consecutive over ~70h, and the correct response is
        to wait for a 7am reset. Same rc=1, same tombstone line, as the 4,284
        dead-credential failures on another seat."""
        self._register(client)
        for _ in range(250):
            self._report(client, "failed", rc=1, cause="quota",
                         error="Claude AI usage limit reached|resets Aug 10 at 7am")
        row = self._row(client)
        assert row["session_attempts"] == 250
        assert row["session_successes"] == 0        # not one session completed
        assert row["consecutive_session_failures"] == 0
        assert row["dark"] is False                 # and it is NOT dark
        # Being rate-limited proves the launcher reached the API, so the
        # launch-ok field is stamped while the session-ok field is not.
        assert row["last_launch_ok_at"] is not None
        assert row["last_session_ok_at"] is None

    def test_quota_resets_rather_than_merely_not_counting(self, client, tmp_db):
        """auth, quota, auth must be 1 — not 2. A proof of capability in the
        middle supersedes the failure before it (c3749/c3752 vs c3751)."""
        self._register(client)
        self._report(client, "failed", rc=1, cause="auth")
        self._report(client, "failed", rc=1, cause="quota")
        self._report(client, "failed", rc=1, cause="auth")
        assert self._row(client)["consecutive_session_failures"] == 1

    def test_unclassified_failures_neither_count_nor_clear(self, client, tmp_db):
        """Under-firing is the safe direction: an unrecognised failure must not
        page an operator, and must not forgive a real fault either."""
        self._register(client)
        self._report(client, "failed", rc=1, cause="auth")
        for _ in range(10):
            self._report(client, "failed", rc=1, cause="api")
        row = self._row(client)
        assert row["consecutive_session_failures"] == 1     # held, not moved
        assert row["dark"] is False
        assert row["session_attempts"] == 11                # still all counted

    def test_launcher_class_latches(self, client, tmp_db):
        """rc=127 is exec-of-a-missing-binary: 995 occurrences across two
        seats, 100% this class. The cheapest positive in the scheme."""
        self._register(client)
        for _ in range(3):
            self._report(client, "failed", rc=127, cause="launcher")
        assert self._row(client)["dark_reason"] == "failing-launches"

    def test_watchdog_run_never_darkens_a_working_seat(self, client, tmp_db):
        """The measured false positive this leg would have fired first.

        One live rail's longest consecutive `rc=143` run is 5 — 112, 177, 121,
        156, 129 tool calls, 695 in the stretch, no `exit=0` between them. At
        the default threshold of 3, counting watchdog toward incapacity marks
        that seat `failing-launches` across the most productive stretch in its
        log. 29 kills across three rails, not one stalled session."""
        self._register(client)
        for _ in range(5):
            r = self._report(client, "failed", rc=143, cause="watchdog")
            assert r.json()["counter"] == "unchanged"
        row = self._row(client)
        assert row["dark"] is False
        assert row["consecutive_session_failures"] == 0
        # Counted, though — on the axis that means "the cap is too low".
        assert row["session_truncations"] == 5
        assert row["last_truncated_at"] is not None
        assert row["session_attempts"] == 5

    def test_watchdog_does_not_clear_a_real_fault_either(self, client, tmp_db):
        """The other half: it must not forgive. `auth, watchdog, auth, auth`
        is 3 — a kill proves the process lived 30 minutes, never that the
        credential works. A wrong latch is loud; a wrong clear is silent."""
        self._register(client)
        self._report(client, "failed", rc=1, cause="auth")
        self._report(client, "failed", rc=143, cause="watchdog")
        self._report(client, "failed", rc=1, cause="auth")
        self._report(client, "failed", rc=1, cause="auth")
        row = self._row(client)
        assert row["consecutive_session_failures"] == 3
        assert row["dark"] is True and row["dark_reason"] == "failing-launches"

    def test_watchdog_counter_survives_the_disposition_change(self, client, tmp_db):
        """A registry that already counted kills as capability failures may be
        holding a working seat at the threshold. The v16 migration clears the
        ones whose last attempt was a kill rather than leaving a false latch
        primed."""
        self._register(client)
        self._report(client, "failed", rc=143, cause="watchdog")
        with db.transaction() as conn:
            conn.execute("UPDATE wake_session_stats SET consecutive_failures = 4 "
                         "WHERE last_cause = 'watchdog'")
        db._migrate(db.get_connection())
        assert self._row(client)["consecutive_session_failures"] == 0
        assert self._row(client)["dark"] is False

    def test_unknown_cause_string_degrades_to_inert(self, client, tmp_db):
        """A future handler inventing a class must not silently latch or clear
        against an old registry — it lands in `api`, which does neither."""
        self._register(client)
        r = self._report(client, "failed", rc=1, cause="something-new")
        assert r.json()["cause"] == "api" and r.json()["counter"] == "unchanged"

    def test_zero_attempts_abstains_rather_than_reading_green(self, client, tmp_db):
        # A quiet seat has no exit=0 either. Keying on absence-of-success would
        # page every idle seat, so the leg abstains at attempts=0 (#521 c3742).
        self._register(client)
        row = self._row(client)
        assert row["session_attempts"] == 0
        assert row["dark"] is False and row["dark_reason"] is None

    def test_row_says_whether_the_seat_can_be_measured_at_all(self, client, tmp_db):
        """`session_attempts: 0` is ambiguous by nature — idle, or not
        reporting. The row states which rather than leaving a reader to
        deduce it from a zero, which is the deduction nobody made for 23 days."""
        self._register(client, handler_version="2.4.5")
        row = self._row(client)
        assert row["session_reporting"] is False
        assert row["stale"] is True          # and it is flagged for update
        self._register(client, handler_version="2.4.6")
        assert self._row(client)["session_reporting"] is True

    def test_missing_handler_version_is_stale_not_fresh(self, client, tmp_db):
        """An unreported component read as fresh: this ticket's own failure
        shape, one column over (#521 c3747)."""
        self._register(client)
        db.execute("UPDATE wake_registrations SET handler_version = NULL")
        assert self._row(client)["stale"] is True

    def test_ok_with_nonzero_rc_counts_as_failure(self, client, tmp_db):
        self._register(client)
        self._report(client, "ok", rc=1)
        row = self._row(client)
        assert row["session_successes"] == 0
        assert row["consecutive_session_failures"] == 0   # cause=api => inert
        assert row["session_attempts"] == 1

    def test_unknown_outcome_rejected(self, client, tmp_db):
        self._register(client)
        assert self._report(client, "maybe").status_code == 400

    def test_disconnected_seat_reports_age_not_capability(self, client, tmp_db):
        # Leg order: a box that is simply switched off must read
        # "go restart a box", not a stale "that seat's launcher is broken".
        self._register(client)
        for _ in range(3):
            self._report(client, "failed", rc=1, cause="auth")
        db.execute("UPDATE wake_registrations SET last_connect_at = "
                   "datetime('now', '-3 days')")
        assert self._row(client)["dark_reason"] == "silent-past-bound"

    def test_idle_seat_is_probed_and_probe_self_clears(self, client, tmp_db):
        import main
        self._register(client)
        db.execute("UPDATE wake_registrations SET registered_at = "
                   "datetime('now', '-30 days')")
        main._evaluate_dark_transitions()
        rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
        probes = [n for n in rows if n["event_type"] == "wake.probe"]
        assert len(probes) == 1
        # Cooldown: a second sweep does not stack a second probe.
        main._evaluate_dark_transitions()
        rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
        assert len([n for n in rows if n["event_type"] == "wake.probe"]) == 1
        # The attempt the probe existed to produce retires it.
        self._report(client, "ok")
        rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
        assert not [n for n in rows if n["event_type"] == "wake.probe"]

    def test_non_reporting_seat_is_never_probed(self, client, tmp_db):
        # A seat below the reporting handler can never convert a probe into an
        # attempt, so probing it spends a real session for nothing. The gate
        # compares the WAKE series (2.4.x), not the listener series (2.5.x) —
        # written the other way it matched zero seats on the whole mesh.
        import main
        self._register(client, handler_version="2.4.5")
        db.execute("UPDATE wake_registrations SET registered_at = "
                   "datetime('now', '-30 days')")
        main._evaluate_dark_transitions()
        rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
        assert not [n for n in rows if n["event_type"] == "wake.probe"]

    def test_current_handler_version_passes_the_probe_gate(self, client, tmp_db):
        """Regression guard for the version-series bug specifically: whatever
        the registry currently SERVES as the wake handler must satisfy the
        gate. If a bump ever breaks that, the probe leg goes silently inert."""
        import main
        assert main._ver_ge(main._rail_versions()["wake"],
                            main._SESSION_REPORTING_SINCE)

    def test_dark_transition_is_a_reading_not_an_inbox_message(self, client, tmp_db):
        """Rail 3.10: darkness is state + an audit trail, rendered by
        /wake/health. It used to be written into every admin's inbox — 45 of
        the last 200 rows on one, addressed to nobody. The DETAIL that made
        those rows worth reading (cause, the seat's own first error) has to
        survive the move, which is what this asserts."""
        import main
        self._register(client)
        main._evaluate_dark_transitions()          # record the healthy baseline
        for _ in range(3):
            self._report(client, "failed", rc=1, cause="auth",
                         error="Not logged in - Please run /login")
        main._evaluate_dark_transitions()

        # 1. nobody's inbox is spent on it
        rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
        assert [n for n in rows if n["event_type"].startswith("wake.dark")] == []

        # 2. the reading says which seat, why, and since when
        health = client.get("/api/v1/wake/health").json()
        seat = next(s for s in health["seats"] if s["identity"] == "admin")
        assert seat["dark"] is True and seat["reason"] == "failing-launches"
        assert seat["consecutive_session_failures"] == 3
        assert "admin" in health["dark"]

        # 3. the transition is in the history, with the operator detail intact
        t = [h for h in health["transitions"] if h["event"] == "wake.dark"]
        assert t and t[0]["identity"] == "admin"
        assert t[0]["detail"]["last_session_cause"] == "auth"
        assert "Not logged in" in t[0]["detail"]["last_session_error"]


class TestLaneScope:
    """#561 per-lane delivery (2026-09-01: the inbox hands out per
    thread id): thread= filters both list and watch to one lane;
    lane_scope=own hides rows on threads held by ANOTHER stamped live
    session of the same identity — held lanes go to their holders, panes
    only get turns for their own business (#568 c5200)."""

    def _two_threads(self, client, raw):
        token, _ = _invite_and_activate(client, raw, "lane", [])
        agent = AuthedClient(raw, token)
        ra = client.post("/api/v1/tickets", json={"summary": "lane A",
                                                  "assignee": "lane"})
        rb = client.post("/api/v1/tickets", json={"summary": "lane B",
                                                  "assignee": "lane"})
        assert ra.status_code == 200 and rb.status_code == 200
        return agent, ra.json()["ticket_id"], rb.json()["ticket_id"]

    def test_thread_filter_scopes_inbox_and_watch(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            agent, ta, tb = self._two_threads(client, raw)
            rows = agent.get(f"/api/v1/notifications?thread=ticket:{ta}") \
                .json()["notifications"]
            assert rows and all(n["ticket_id"] == ta for n in rows)
            w = agent.get(f"/api/v1/notifications/watch?timeout=1&thread=ticket:{tb}") \
                .json()["notifications"]
            assert w and all(n["ticket_id"] == tb for n in w)

    def test_thread_filter_validates(self, client):
        r = client.get("/api/v1/notifications?thread=bogus")
        assert r.status_code == 422

    def test_lane_scope_own_hides_only_foreign_held_lanes(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            agent, ta, tb = self._two_threads(client, raw)
            # A holder session of 'lane' claims thread A by writing with its nonce.
            r = agent.post(f"/api/v1/tickets/{ta}/comments",
                           json={"body": "holder resident on A"},
                           headers={"X-Session-Id": "holder-A-nonce"})
            assert r.status_code == 200, r.json()
            # Fresh traffic on both threads.
            client.post(f"/api/v1/tickets/{ta}/comments", json={"body": "to A"})
            client.post(f"/api/v1/tickets/{tb}/comments", json={"body": "to B"})
            # A DIFFERENT session (the pane): held lane A hidden, B visible.
            pane = agent.get("/api/v1/notifications?lane_scope=own",
                             headers={"X-Session-Id": "pane-nonce"}) \
                .json()["notifications"]
            assert pane and all(n["ticket_id"] != ta for n in pane)
            assert any(n["ticket_id"] == tb for n in pane)
            # The holder's own session still sees its lane.
            own = agent.get("/api/v1/notifications?lane_scope=own",
                            headers={"X-Session-Id": "holder-A-nonce"}) \
                .json()["notifications"]
            assert any(n["ticket_id"] == ta for n in own)
            # Watch honors the same exclusion for the pane.
            wp = agent.get("/api/v1/notifications/watch?timeout=1&lane_scope=own",
                           headers={"X-Session-Id": "pane-nonce"}) \
                .json()["notifications"]
            assert all(n["ticket_id"] != ta for n in wp)

    def test_lane_scope_never_hides_nonceless_claims(self, client):
        """Legacy unstamped writers keep today's visibility — a nonce-less
        claim must never hide a row (fail-open toward delivery)."""
        app = _get_app()
        with TestClient(app) as raw:
            agent, ta, tb = self._two_threads(client, raw)
            r = agent.post(f"/api/v1/tickets/{ta}/comments",
                           json={"body": "unstamped write claims A"})
            assert r.status_code == 200
            client.post(f"/api/v1/tickets/{ta}/comments", json={"body": "to A"})
            pane = agent.get("/api/v1/notifications?lane_scope=own",
                             headers={"X-Session-Id": "pane-nonce"}) \
                .json()["notifications"]
            assert any(n["ticket_id"] == ta for n in pane)


class TestInteractiveSurface:
    """Rail 3.10 — an interactive session is a holder that a human types into.

    It was getting every message and having to pull them, while every other
    consumer got targeted delivery. The
    surface that coordinates the fleet was the last consumer without the three
    properties every agent has: the plane knows it, delivery targets it, and
    its scope is the rail's one rule instead of "everything this identity
    watches"."""

    CONV = "interactive-session-1"
    OTHER = "interactive-session-2"

    def _seat(self, client, raw, name="surf"):
        token, _ = _invite_and_activate(client, raw, name, [])
        return AuthedClient(raw, token)

    def _register(self, agent, conv=None):
        r = agent.post("/api/v1/wake/surfaces",
                       json={"conversation_id": conv or self.CONV,
                             "surface": "interactive", "host": "testbox",
                             "client": "claude-code"})
        assert r.status_code == 200, r.json()
        return r.json()

    def test_registration_is_visible_and_refreshable(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            body = self._register(agent)
            assert body["surface"]["conversation_id"] == self.CONV   # raw id, it is ours
            assert body["surface"]["conversation"] != self.CONV       # fingerprint too
            assert body["surface"]["surface"] == "interactive"
            assert body["holding"] == []
            mine = agent.get("/api/v1/wake/surfaces").json()
            assert [s["conversation_id"] for s in mine["surfaces"]] == [self.CONV]
            # Re-registering refreshes rather than duplicating.
            self._register(agent)
            assert len(agent.get("/api/v1/wake/surfaces").json()["surfaces"]) == 1
            # Ending removes it from the default view but keeps the record.
            agent.post("/api/v1/wake/surfaces",
                       json={"conversation_id": self.CONV, "ended": True})
            assert agent.get("/api/v1/wake/surfaces").json()["surfaces"] == []
            kept = agent.get("/api/v1/wake/surfaces?include_ended=true").json()
            assert kept["surfaces"][0]["ended_at"]

    def test_scope_is_the_threads_this_conversation_is_on(self, client):
        """The one rule, server-side (0.12.69, #705): a thread this
        conversation is on arrives; a thread it merely watches does not; and a
        mention on a thread none of my conversations is on is NOT pushed here
        — it is a cold wake, the rail's to birth a holder on. Until 0.12.69 the
        mention arrived too, which made a live session the identity's
        catch-all and left "two live sessions, which one?" unanswerable."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            # A ticket this conversation is on: it wrote on it with its own id.
            mine = client.post("/api/v1/tickets", json={"summary": "mine"},
                               headers={"X-Session-Id": "work-mine"}).json()["ticket_id"]
            agent.post(f"/api/v1/tickets/{mine}/comments", json={"body": "mine"},
                       headers={"X-Session-Id": self.CONV})
            agent.post(f"/api/v1/tickets/{mine}/subscribe")
            # A ticket it merely watches (it commented under ANOTHER session).
            watched = client.post("/api/v1/tickets", json={"summary": "watched"},
                                  headers={"X-Session-Id": "work-watched"}).json()["ticket_id"]
            agent.post(f"/api/v1/tickets/{watched}/comments", json={"body": "drive-by"},
                       headers={"X-Session-Id": self.OTHER})
            agent.post(f"/api/v1/tickets/{watched}/subscribe")
            # A third it is @mentioned on, on which no conversation of surf is.
            elsewhere = client.post("/api/v1/tickets", json={"summary": "elsewhere"},
                                    headers={"X-Session-Id": "work-elsewhere"}).json()["ticket_id"]
            client.post(f"/api/v1/tickets/{mine}/comments", json={"body": "to the thread I am on"})
            client.post(f"/api/v1/tickets/{watched}/comments", json={"body": "not yours"})
            client.post(f"/api/v1/tickets/{elsewhere}/comments", json={"body": "hey @surf"})

            scoped = agent.get(f"/api/v1/notifications?for_conversation={self.CONV}") \
                .json()["notifications"]
            ids = {n["ticket_id"] for n in scoped}
            assert mine in ids, scoped
            assert watched not in ids, "a thread I merely watch is not pushed"
            assert elsewhere not in ids, \
                "a mention on a thread none of my conversations is on is a cold wake, not a push"
            # The cold wake is the rail's: the row is there, undelivered, for it.
            rail = agent.get("/api/v1/notifications?undelivered=true").json()["notifications"]
            assert any(n["ticket_id"] == elsewhere and n["event_type"] == "ticket.mentioned"
                       for n in rail), rail
            # …and everything is still readable on demand: scoping is not deletion.
            wide = {n["ticket_id"] for n in
                    agent.get("/api/v1/notifications").json()["notifications"]}
            assert watched in wide and elsewhere in wide
            # Writing on it from this conversation puts the conversation on
            # that thread; from then on its rows arrive here.
            agent.post(f"/api/v1/tickets/{elsewhere}/comments", json={"body": "on it"},
                       headers={"X-Session-Id": self.CONV})
            client.post(f"/api/v1/tickets/{elsewhere}/comments", json={"body": "thanks"})
            scoped = agent.get(f"/api/v1/notifications?for_conversation={self.CONV}") \
                .json()["notifications"]
            assert any(n["ticket_id"] == elsewhere and n["event_type"] == "ticket.commented"
                       for n in scoped), scoped

    def test_an_assignment_on_a_thread_i_am_not_on_is_a_cold_wake_not_a_push(self, client):
        """The commonest way work arrives, and the case ruled on #705:
        "an 'unheld' tag etc would be a cold wake". A ticket filed straight to
        the identity births a holder on its thread through the rail; a live
        session of that identity is never handed it — not even when it is the
        only live conversation. Until 0.12.69 this suite pinned the opposite
        (a filed ticket "never reached your session" was read as the bug)."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            url = f"/api/v1/notifications?for_conversation={self.CONV}"
            t = client.post("/api/v1/tickets", json={"summary": "filed to you",
                                                     "assignee": "surf"}).json()["ticket_id"]
            assert not any(n["ticket_id"] == t for n in agent.get(url).json()["notifications"])
            # The rail's predicate sees it: undelivered, addressed, a wake.
            assert any(n["ticket_id"] == t and n["event_type"] == "ticket.created"
                       for n in agent.get("/api/v1/notifications?undelivered=true")
                       .json()["notifications"])
            # Re-assigning it away and back changes nothing for the session:
            # still not on the thread, still not pushed.
            client.patch(f"/api/v1/tickets/{t}", json={"assignee": "admin"})
            client.patch(f"/api/v1/tickets/{t}", json={"assignee": "surf"})
            assert not any(n["ticket_id"] == t for n in agent.get(url).json()["notifications"])

    def test_a_thread_another_conversation_of_mine_is_on_is_not_pushed_here(self, client):
        """Two conversations, one identity: the thread's rows go to the
        conversation on it. This is what stops a human pane from being fed a
        headless holder's turns (#568 c5200, restated on the threads record)."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            t = client.post("/api/v1/tickets", json={"summary": "owned by surf",
                                                     "assignee": "surf"}).json()["ticket_id"]
            # The OTHER conversation takes the thread, then someone speaks.
            r = agent.post("/api/v1/wake/threads",
                           json={"kind": "ticket", "object_id": t,
                                 "conversation_id": self.OTHER, "holder": "stream",
                                 "state": "open"})
            assert r.status_code == 200, r.json()
            client.post(f"/api/v1/tickets/{t}/comments", json={"body": "for the holder"})
            scoped = agent.get(f"/api/v1/notifications?for_conversation={self.CONV}") \
                .json()["notifications"]
            assert all(n["ticket_id"] != t for n in scoped)

    def test_sibling_comments_are_pinned_and_other_self_events_stay_quiet(self, client):
        """#932 changes comment echoes, not the audience or status-event policy."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            t = agent.post("/api/v1/tickets", json={"summary": "surf's own", "body": "b",
                                                    "assignee": "surf"},
                           headers={"X-Session-Id": self.OTHER}).json()["id"]
            # another conversation of the same identity holds the ticket
            r = agent.post("/api/v1/wake/threads",
                           json={"kind": "ticket", "object_id": t,
                                 "conversation_id": self.OTHER, "holder": "stream",
                                 "state": "open"})
            assert r.status_code == 200, r.json()
            before = {n["id"] for n in agent.get("/api/v1/notifications?unread=false&limit=200")
                      .json()["notifications"]}
            # the identity writes from its interactive conversation
            r = agent.post(f"/api/v1/tickets/{t}/comments", json={"body": "from the surface"},
                           headers={"X-Session-Id": self.CONV})
            assert r.status_code == 200, r.json()
            r = agent.patch(f"/api/v1/tickets/{t}", json={"assignee": "surf"},
                            headers={"X-Session-Id": self.CONV})
            assert r.status_code == 200, r.json()
            after = [n for n in agent.get("/api/v1/notifications?unread=false&limit=200")
                     .json()["notifications"] if n["id"] not in before]
            sibling_rows = [n for n in after if n.get("actor") == "surf"]
            assert len(sibling_rows) == 1, after
            assert sibling_rows[0]['payload']['recipient_session'] == _session_fp_expected(self.OTHER)
            # docs: the author comments on its own doc, another conversation bound
            d = agent.post("/api/v1/docs", json={"title": "surf doc", "body": "x"},
                           headers={"X-Session-Id": self.OTHER}).json()["id"]
            before = {n["id"] for n in agent.get("/api/v1/notifications?unread=false&limit=200")
                      .json()["notifications"]}
            r = agent.post(f"/api/v1/docs/{d}/comments", json={"body": "own comment"},
                           headers={"X-Session-Id": self.CONV})
            assert r.status_code in (200, 201), r.json()
            after = [n for n in agent.get("/api/v1/notifications?unread=false&limit=200")
                     .json()["notifications"] if n["id"] not in before]
            sibling_rows = [n for n in after if n.get("actor") == "surf"]
            assert len(sibling_rows) == 1, after
            assert sibling_rows[0]['payload']['recipient_session'] == _session_fp_expected(self.OTHER)

    def test_a_surface_holds_without_proving_it_is_listening(self, client):
        """0.12.13: there is no liveness flag, and a surface is never asked to
        prove it is reading. It was asked: `connected` was true only while a
        `?for_conversation` long-poll was open (+30 s). The rail closed a
        person's bindings when it read false and then let a twin be born on the
        thread they were holding — twice on 2026-09-20, once because a deploy
        cleared the in-process dict and once because the subscriber was busy
        printing a backlog instead of polling.

        The old test asserted `connected is False` on a freshly registered
        surface. That is the defect stated as an expectation."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            row = agent.get("/api/v1/wake/surfaces").json()["surfaces"][0]
            assert "connected" not in row, row
            assert row["conversation_id"] == self.CONV

            # A watch opening and closing changes nothing about holding.
            r = agent.get(f"/api/v1/notifications/watch?timeout=1&for_conversation={self.CONV}")
            assert r.status_code == 200
            rows = agent.get("/api/v1/wake/surfaces").json()["surfaces"]
            assert [x["conversation_id"] for x in rows] == [self.CONV]

    def test_ended_is_the_only_thing_that_retires_a_surface(self, client):
        """`{"ended": true}` is the mechanism, not an option — the subscriber
        never sent it while it was optional, which is why a liveness guess had
        to be invented to stand in for it."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            assert agent.get("/api/v1/wake/surfaces").json()["surfaces"]

            agent.post("/api/v1/wake/surfaces",
                       json={"conversation_id": self.CONV, "surface": "interactive",
                             "ended": True})
            assert agent.get("/api/v1/wake/surfaces").json()["surfaces"] == []
            kept = agent.get("/api/v1/wake/surfaces?include_ended=true").json()["surfaces"]
            assert [x["conversation_id"] for x in kept] == [self.CONV]

    def test_delivered_receipt_stops_the_re_offer(self, client):
        """The cure for the hand-cranked cursor: a row this surface has shown
        is never offered to it again, exactly as for a holder's fifo."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            t = client.post("/api/v1/tickets", json={"summary": "receipts",
                                                     "assignee": "surf"}).json()["ticket_id"]
            # The conversation gets on the thread by writing on it (0.12.69:
            # an assignment alone is a cold wake, not a push).
            agent.post(f"/api/v1/tickets/{t}/comments", json={"body": "on it"},
                       headers={"X-Session-Id": self.CONV})
            client.post(f"/api/v1/tickets/{t}/comments", json={"body": "first"})
            url = f"/api/v1/notifications?for_conversation={self.CONV}&undelivered=true"
            rows = agent.get(url).json()["notifications"]
            assert rows
            agent.post("/api/v1/notifications/delivered",
                       json={"ids": [n["id"] for n in rows],
                             "conversation_id": self.CONV})
            assert agent.get(url).json()["notifications"] == []
            # Unread and unacked — delivery is not "handled".
            still = agent.get("/api/v1/notifications?unread=true").json()["notifications"]
            assert any(n["ticket_id"] == t for n in still)

    def test_watch_takes_the_same_scope(self, client):
        """The long-poll and the list cannot drift: one clause, both call
        sites."""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            noise = client.post("/api/v1/tickets", json={"summary": "noise"},
                                headers={"X-Session-Id": "work-noise"}).json()["ticket_id"]
            agent.post(f"/api/v1/tickets/{noise}/comments", json={"body": "watching only"},
                       headers={"X-Session-Id": self.OTHER})
            client.post(f"/api/v1/tickets/{noise}/comments", json={"body": "not for you"})
            got = agent.get("/api/v1/notifications/watch?timeout=1&settle=0"
                            f"&for_conversation={self.CONV}").json()
            assert got["notifications"] == []
            mine = client.post("/api/v1/tickets", json={"summary": "addressed", "assignee": "surf"},
                               headers={"X-Session-Id": "work-mine"}).json()["ticket_id"]
            agent.post(f"/api/v1/tickets/{mine}/comments", json={"body": "on it"},
                       headers={"X-Session-Id": self.CONV})      # on the thread now
            client.post(f"/api/v1/tickets/{mine}/comments", json={"body": "for you"})
            got = agent.get("/api/v1/notifications/watch?timeout=2&settle=0"
                            f"&for_conversation={self.CONV}").json()
            assert any(n["ticket_id"] == mine for n in got["notifications"])

    def test_machine_bookkeeping_never_reaches_a_surface(self, client):
        """`wake.*` is an operations reading now. (A scheduled follow-up still
        comes back to the conversation that asked for it: the task is in that
        conversation's thread, and the thread is what it hears — 0.12.69.)"""
        app = _get_app()
        with TestClient(app) as raw:
            agent = self._seat(client, raw)
            self._register(agent)
            import db as _db
            _db.execute("INSERT INTO notifications (recipient, event_type, actor, payload) "
                        "VALUES ('surf', 'wake.dark', 'registry', '{}')")
            scoped = agent.get(f"/api/v1/notifications?for_conversation={self.CONV}") \
                .json()["notifications"]
            assert all(n["event_type"] != "wake.dark" for n in scoped)


class TestStreamDark:
    """Task 609 stream leg: live-SSE absence is the minutes-resolution dark
    signal (the 2026-09-01 outage sat 70+ min inside every other leg's
    threshold), plus the >=2-seats fleet signature."""

    def _register(self, client, host="streambox"):
        r = client.post("/api/v1/wake/register",
                        json={"wake_cmd": "/bin/samcloud-wake", "host": host,
                              "listener_version": "2.6.3",
                              "handler_version": "2.4.6",
                              "max_conn_age": 21600})
        assert r.status_code == 200

    def _second_rail(self, client, raw, name):
        token, _ = _invite_and_activate(client, raw, name, [])
        agent = AuthedClient(raw, token)
        r = agent.post("/api/v1/wake/register",
                       json={"wake_cmd": "/bin/samcloud-wake", "host": name,
                             "listener_version": "2.6.3",
                             "handler_version": "2.4.6",
                             "max_conn_age": 21600})
        assert r.status_code == 200
        return agent

    def test_no_live_stream_past_threshold_is_dark(self, client, tmp_db):
        import main, time as _t
        self._register(client)
        main._REGISTRY_STARTED_TS = _t.time() - 3600      # past boot grace
        main._live_streams.clear()
        main._live_streams["admin"] = {
            "count": 0,
            "last_drop": (datetime.now(timezone.utc)
                          - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")}
        row = self._row_reason(client)
        assert row.startswith("no-live-stream-")

    def test_live_stream_clears_stream_leg(self, client, tmp_db):
        import main, time as _t
        self._register(client)
        main._REGISTRY_STARTED_TS = _t.time() - 3600
        main._live_streams.clear()
        main._live_streams["admin"] = {"count": 1, "last_drop": None}
        assert self._row_reason(client) is None

    def test_boot_grace_abstains(self, client, tmp_db):
        import main, time as _t
        self._register(client)
        main._REGISTRY_STARTED_TS = _t.time()             # just booted
        main._live_streams.clear()
        main._live_streams["admin"] = {
            "count": 0,
            "last_drop": (datetime.now(timezone.utc)
                          - timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")}
        assert self._row_reason(client) is None

    def test_two_stream_dark_seats_page_fleet_signature(self, client, tmp_db):
        import main, time as _t
        app = _get_app()
        with TestClient(app) as raw:
            self._register(client)
            self._second_rail(client, raw, "railtwo")
            main._REGISTRY_STARTED_TS = _t.time() - 3600
            main._live_streams.clear()
            drop = (datetime.now(timezone.utc)
                    - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
            main._live_streams["admin"] = {"count": 0, "last_drop": drop}
            main._live_streams["railtwo"] = {"count": 0, "last_drop": drop}
            main._evaluate_dark_transitions()
            # Rail 3.10: the signature is a reading and an audit row, not an
            # inbox page — but it must still SAY "infrastructure, not seats",
            # which is the whole reason it exists (the 2026-09-01 forensics).
            health = client.get("/api/v1/wake/health").json()
            assert health["fleet_signature"] is not None
            assert sorted(health["fleet_signature"]["seats"]) == ["admin", "railtwo"]
            assert "network/infrastructure" in health["fleet_signature"]["read"]
            fleet = [h for h in health["transitions"] if h["event"] == "wake.dark.fleet"]
            assert fleet and sorted(fleet[0]["detail"]["seats"]) == ["admin", "railtwo"]
            rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
            assert [n for n in rows if n["event_type"].startswith("wake.dark")] == []
            # Latch: a second pass must not re-record the fleet signature.
            main._evaluate_dark_transitions()
            health = client.get("/api/v1/wake/health").json()
            assert len([h for h in health["transitions"]
                        if h["event"] == "wake.dark.fleet"]) == 1

    def _row_reason(self, client, username="admin"):
        rows = client.get("/api/v1/wake/registrations").json()["registrations"]
        return next(r for r in rows if r["username"] == username).get("dark_reason")


class TestWakeRegistrationRetire:
    """#897: a registration could be created by anyone and retired by nobody.

    `GET /wake/registrations` and `POST /wake/register` were the entire surface —
    for admin as much as for a seat — so the one table feeding the fleet's health
    view could only ever grow. It was found paying a real cost: proving a listener
    change needed throwaway containers, two of them registered and ceased to
    exist, and their rows became permanent. Worse, a doc then cited those rows as
    evidence that seats go un-updated, so the measurement contaminated its own
    evidence and nobody could clean up after it.

    The semantics mirror a surface deliberately, and the alternative was argued
    and rejected: STICKY retirement builds this ticket inverted — a row nobody can
    un-retire — and a running seat ABSENT from the fleet view is worse than a
    stale one, because stale is visible and absent is not. So a real register
    clears it.
    """

    def _reg(self, c, host="boxA", **kw):
        body = {"wake_cmd": "/bin/samcloud-wake", "host": host,
                "listener_version": "2.5.3", "handler_version": "2.4.6"}
        body.update(kw)
        r = c.post("/api/v1/wake/register", json=body)
        assert r.status_code == 200, r.json()
        return r.json()

    def _hosts(self, c, include_retired=False):
        q = "?include_retired=true" if include_retired else ""
        rows = c.get("/api/v1/wake/registrations" + q).json()["registrations"]
        return sorted(r["host"] for r in rows)

    def test_rows_are_per_host_so_a_count_is_not_a_seat_count(self, client):
        """The premise of #897's wrong figure. One identity, three hosts, three
        rows — `UNIQUE(user_id, host)`. A throwaway container with no --hostname
        gets a fresh container id as its host, so each run mints a row."""
        for h in ("boxA", "7f3c91aa2b1d", "0c2e55fe9a77"):
            self._reg(client, host=h)
        assert self._hosts(client) == ["0c2e55fe9a77", "7f3c91aa2b1d", "boxA"]
        # Re-registering the SAME host upserts rather than adding (two years of
        # restarts on a stable hostname produced one row; two proof runs without
        # one produced two).
        self._reg(client, host="boxA")
        assert len(self._hosts(client)) == 3

    def test_retire_leaves_the_default_view_and_is_never_deleted(self, client):
        self._reg(client, host="boxA")
        self._reg(client, host="throwaway1")
        r = client.post("/api/v1/wake/register",
                        json={"ended": True, "host": "throwaway1"})
        assert r.status_code == 200, r.json()
        assert r.json()["status"] == "retired"
        assert self._hosts(client) == ["boxA"]
        # Mark, don't delete: these rows are the record of who ran a rail where.
        assert self._hosts(client, include_retired=True) == ["boxA", "throwaway1"]
        kept = [x for x in client.get(
            "/api/v1/wake/registrations?include_retired=true"
        ).json()["registrations"] if x["host"] == "throwaway1"]
        assert kept[0]["ended_at"], kept[0]

    def test_retired_stays_retired_while_nothing_reconnects(self, client):
        """The ticket's own case: a row is stale BECAUSE the seat is gone, and a
        gone seat calls nothing. Registering a DIFFERENT host must not revive it."""
        self._reg(client, host="throwaway1")
        self._reg(client, host="boxA")
        client.post("/api/v1/wake/register",
                    json={"ended": True, "host": "throwaway1"})
        for _ in range(3):
            self._reg(client, host="boxA")        # other rows churn; it stays gone
        assert self._hosts(client) == ["boxA"]

    def test_a_real_register_brings_its_own_row_back(self, client):
        """The other direction, and why sticky was rejected: a seat that retired
        on a clean shutdown and then starts again must be VISIBLE, or the view
        whose job is spotting absent seats is the thing hiding it."""
        self._reg(client, host="boxA")
        client.post("/api/v1/wake/register", json={"ended": True, "host": "boxA"})
        assert self._hosts(client) == []
        self._reg(client, host="boxA")            # the listener's next SSE connect
        assert self._hosts(client) == ["boxA"]
        live = client.get("/api/v1/wake/registrations").json()["registrations"]
        assert live[0]["ended_at"] is None, live[0]

    def test_retiring_does_not_make_a_seat_undarkenable(self, client):
        """The inverse hazard, and the one worth pinning. /wake/health documents
        itself as computing `dark` "the same way /wake/registrations computes it",
        so the two must agree about which rows exist at all — and a retire must
        not become a way to hide a seat from the dark-watch permanently."""
        self._reg(client, host="boxA")
        self._reg(client, host="throwaway1")
        # NOTE: /wake/health emits one entry per ROW but calls it a seat and keys
        # it by identity, so two rows of one identity appear twice with no field
        # distinguishing them (no `host` in the payload). That is the same
        # row-vs-identity confusion #897 is about, one endpoint over; not changed
        # here, because it is a response-shape change and this PR is the retire.
        # Asserted by count for that reason.
        assert [x["identity"] for x in
                client.get("/api/v1/wake/health").json()["seats"]] == \
            ["admin", "admin"]
        client.post("/api/v1/wake/register",
                    json={"ended": True, "host": "throwaway1"})
        # One row gone; the identity is STILL reportable through its live row, so
        # retiring cannot hide a seat that is still running.
        assert [x["identity"] for x in
                client.get("/api/v1/wake/health").json()["seats"]] == ["admin"]
        # And the retired row is darkenable again the moment it reconnects.
        self._reg(client, host="throwaway1")
        assert [x["identity"] for x in
                client.get("/api/v1/wake/health").json()["seats"]] == \
            ["admin", "admin"]

    def test_admin_retires_someone_elses_row_and_a_seat_cannot(self, client):
        """The leg the ticket actually needs. Self-retire only helps a seat
        shutting down cleanly — the case that was never the problem."""
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "ghost", [])
            seat = AuthedClient(raw, token)
            self._reg(seat, host="deadbox")
            assert self._hosts(seat) == ["deadbox"]

            # A seat may not retire another identity's row.
            token2, _ = _invite_and_activate(client, raw, "nosy", [])
            other = AuthedClient(raw, token2)
            r = other.post("/api/v1/wake/register",
                           json={"ended": True, "identity": "ghost",
                                 "host": "deadbox"})
            assert r.status_code == 403, r.json()
            assert self._hosts(seat) == ["deadbox"]

            # Admin may, which is the only route that closes a gone seat's row.
            r = client.post("/api/v1/wake/register",
                            json={"ended": True, "identity": "ghost",
                                  "host": "deadbox"})
            assert r.status_code == 200, r.json()
            assert r.json()["identity"] == "ghost"
            assert self._hosts(seat) == []
            assert self._hosts(seat, include_retired=True) == ["deadbox"]

    def test_retiring_a_row_that_is_not_there_is_a_404_not_a_silent_ok(self, client):
        self._reg(client, host="boxA")
        r = client.post("/api/v1/wake/register",
                        json={"ended": True, "host": "neverexisted"})
        assert r.status_code == 404, r.json()
        r = client.post("/api/v1/wake/register",
                        json={"ended": True, "identity": "nobody-at-all",
                              "host": "boxA"})
        assert r.status_code == 404, r.json()


class TestWakeBeacon:
    """Task 609 c1024: the deaf-seat witness statement. Passive (never darkens),
    idempotent within an episode, verbatim last_error into the dark page."""

    def _register(self, client):
        r = client.post("/api/v1/wake/register",
                        json={"wake_cmd": "/bin/samcloud-wake", "host": "beaconbox",
                              "listener_version": "2.6.3",
                              "handler_version": "2.4.6",
                              "max_conn_age": 21600})
        assert r.status_code == 200

    def test_beacon_records_and_reports_known_dark(self, client, tmp_db):
        import main, time as _t
        self._register(client)
        r = client.post("/api/v1/wake/beacon",
                        json={"dark_for_s": 612,
                              "last_error": "URLError: [Errno 8] nodename nor servname provided",
                              "last_event_id": 17250,
                              "handler_version": "2.6.3"})
        assert r.status_code == 200
        assert r.json() == {"status": "recorded", "known_dark": False}
        # Darken via the stream leg; the page carries the witness verbatim.
        main._REGISTRY_STARTED_TS = _t.time() - 3600
        main._live_streams.clear()
        main._live_streams["admin"] = {
            "count": 0,
            "last_drop": (datetime.now(timezone.utc)
                          - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")}
        main._evaluate_dark_transitions()
        # The witness statement rides the reading now (rail 3.10), verbatim:
        # Errno 8 and Errno 51 are different incidents with different owners.
        health = client.get("/api/v1/wake/health").json()
        seat = next(s for s in health["seats"] if s["identity"] == "admin")
        assert seat["dark"] is True
        assert seat["beacon"]["dark_for_s"] == 612
        assert "Errno 8" in seat["beacon"]["last_error"]
        dark = [h for h in health["transitions"] if h["event"] == "wake.dark"]
        assert dark and dark[0]["detail"]["beacon"]["dark_for_s"] == 612
        # Re-beacon while dark: refresh + known_dark true, and no re-record.
        r2 = client.post("/api/v1/wake/beacon", json={"dark_for_s": 900})
        assert r2.json()["known_dark"] is True
        main._evaluate_dark_transitions()
        health = client.get("/api/v1/wake/health").json()
        assert len([h for h in health["transitions"] if h["event"] == "wake.dark"]) == 1

    def test_beacon_never_darkens_by_itself(self, client, tmp_db):
        import main, time as _t
        self._register(client)
        main._REGISTRY_STARTED_TS = _t.time() - 3600
        main._live_streams.clear()
        main._live_streams["admin"] = {"count": 1, "last_drop": None}   # live stream
        client.post("/api/v1/wake/beacon", json={"dark_for_s": 5000})
        main._evaluate_dark_transitions()
        rows = client.get("/api/v1/notifications?unread=true").json()["notifications"]
        assert not [n for n in rows if n["event_type"] == "wake.dark"]


class TestThreadsRecord:
    """Thread bindings + delivery receipts (doc 174 / rail 3.9): the
    conversation <-> object <-> identity record that replaces claims and
    the 30-minute re-offer as the rail's routing basis."""

    def _peer(self, client, raw, name="thread-peer"):
        token, _ = _invite_and_activate(client, raw, name, [])
        return AuthedClient(raw, token)

    def test_a_scoped_stream_is_the_same_for_an_admin_as_for_anyone(self, client, tmp_db):
        """0.12.20. `_can_see_event` opens `if auth.is_admin(): return True`,
        which is right for permission and wrong for delivery: one identity's
        stream carried every event on the plane (10,430 of them on
        samclaude-admin) while every other seat's carried what was addressed
        to it. A role must not change the shape of a feed."""
        import main
        mine = client.post("/api/v1/tickets", json={"summary": "mine", "body": "b"},
                           headers={"X-Session-Id": "conv-a"}).json()
        theirs = client.post("/api/v1/tickets", json={"summary": "theirs", "body": "b"}).json()
        client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": mine["id"], "conversation_id": "conv-a",
            "holder": "conversation"})

        # The REAL Event, not a stub. A hand-rolled stub here carried a
        # `payload` attribute that Event has never had (the field is `data`),
        # so this test went green for three days over a clause that raised
        # AttributeError in production and 500'd the stream — 0.12.28, #814.
        # A stub that can drift from the class it stands in for is how a
        # passing suite hides a dead path.
        from events import Event

        def _Ev(target, payload=None, scope=""):
            return Event(type="test.event", target=target,
                         data=payload or {}, scope=scope)

        auth = main.AuthContext.__new__(main.AuthContext)   # identity/is_admin/owner_id
        auth.identity = lambda: "admin"
        auth.is_admin = lambda: True
        auth.owner_id = "user:1"

        # a thread this conversation is on: yes
        assert main._event_for_conversation(auth, _Ev(f"ticket:{mine['id']}"), "conv-a")
        # somebody else's work, which an admin may READ but is not addressed to:
        assert not main._event_for_conversation(auth, _Ev(f"ticket:{theirs['id']}"), "conv-a")
        # ...and the unscoped predicate would have handed it over, which is the bug
        assert main._can_see_event(auth, _Ev(f"ticket:{theirs['id']}"))
        # named in the payload on a thread I am not on: a cold wake for the
        # rail, not a doorbell for a live conversation (0.12.69, #705)
        assert not main._event_for_conversation(
            auth, _Ev(f"ticket:{theirs['id']}", {"assignee": "admin"}), "conv-a")
        # something addressed at me directly still rings. A wake emits with
        # target `user:<id>` and no object, so the thread check cannot see it —
        # found by ringing the doorbell and watching nothing arrive.
        assert main._event_for_conversation(
            auth, _Ev("user:1", {"summary": "wake"}, scope="user:1"), "conv-a")
        assert not main._event_for_conversation(
            auth, _Ev("user:2", {"summary": "not yours"}, scope="user:2"), "conv-a")

    def test_a_holder_cannot_take_a_thread_a_live_surface_holds(self, client, tmp_db):
        """No twin (0.12.18). The row is UNIQUE(kind, object_id, identity), so
        the upsert never failed on the conflict — it RESOLVED it, turning the
        person's binding into the holder's. That is the evidence being
        destroyed: `_bind_conversation` then declines to re-bind while a
        non-closed row exists, and the rail's gate has nothing left to read.
        Three headless holders sat under samclaude-admin for 21-23 hours on
        2026-09-21 because of it, one answering on a thread the person was
        typing in."""
        tid = client.post("/api/v1/tickets", json={"summary": "held", "body": "b"},
                          headers={"X-Session-Id": "person-conv"}).json()["id"]
        client.post("/api/v1/wake/surfaces",
                    json={"conversation_id": "person-conv", "surface": "interactive"})
        # writing on it bound the person: holder `conversation`
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        mine = next(x for x in rows if x["object_id"] == tid)
        assert mine["holder"] == "conversation" and mine["state"] == "open"

        r = client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "headless-conv",
            "holder": "stream", "pid": 4242})
        assert r.status_code == 409, r.text
        assert "already holds" in r.json()["detail"]
        # and the person's binding is exactly as it was
        again = next(x for x in client.get("/api/v1/wake/threads").json()["threads"]
                     if x["object_id"] == tid)
        assert again["holder"] == "conversation"
        assert again["conversation_id"] == "person-conv"

        # the same conversation may of course re-post its own binding
        assert client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "person-conv",
            "holder": "conversation"}).status_code == 200

    def test_goodbye_closes_the_conversations_bindings_and_frees_the_thread(self, client, tmp_db):
        """`{"ended": true}` is the one event that means the conversation is
        over, so the plane retires its bindings itself. Every seat used to
        sweep this by guessing — close any `conversation` row whose
        conversation I cannot find among the surfaces — and on 2026-09-20
        22:37:35 that closed a live person's bindings on three tickets from a
        list the rail had merely mis-read."""
        tid = client.post("/api/v1/tickets", json={"summary": "leaving", "body": "b"},
                          headers={"X-Session-Id": "going-conv"}).json()["id"]
        client.post("/api/v1/wake/surfaces",
                    json={"conversation_id": "going-conv", "surface": "interactive"})
        assert client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "other-conv",
            "holder": "stream", "pid": 1}).status_code == 409

        client.post("/api/v1/wake/surfaces",
                    json={"conversation_id": "going-conv", "surface": "interactive",
                          "ended": True})
        gone = next(x for x in client.get("/api/v1/wake/threads").json()["threads"]
                    if x["object_id"] == tid)
        assert gone["state"] == "closed", gone
        # unheld now, so a holder may be born
        assert client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "other-conv",
            "holder": "stream", "pid": 1}).status_code == 200

    def test_an_adapter_turn_binds_like_any_other_holder(self, client, tmp_db):
        """rail 3.12.0: a seat whose runtime is not a Claude harness posts
        `holder: adapter`. The enum rejected it with a 400 the rail logs as
        non-fatal — so a local-model seat's first live thread ran a turn, answered, and
        held nothing the plane could see. A rail holder is a rail holder."""
        tid = client.post("/api/v1/tickets", json={"summary": "turtle", "body": "b"}
                          ).json()["id"]
        r = client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "turn-sid",
            "host": "plane-host", "holder": "adapter", "pid": 40926})
        assert r.status_code == 200, r.text
        assert r.json()["thread"]["holder"] == "adapter"
        # and what it creates while it holds joins its thread, exactly as a
        # stream holder's does — the second of the three predicates.
        wt = r.json()["thread"]["thread_id"]
        made = client.post("/api/v1/tickets", json={"summary": "filed by the turn"},
                           headers={"X-Session-Id": "turn-sid"})
        assert made.status_code in (200, 201), made.text
        assert client.get(f"/api/v1/threads/of/ticket/{made.json()['id']}"
                          ).json()["id"] == wt

    def test_upsert_get_patch_roundtrip(self, client, tmp_db):
        tid = client.post("/api/v1/tickets", json={"summary": "held", "body": "b"}
                          ).json()["id"]
        r = client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "sid-one",
            "host": "plane-host", "holder": "stream", "pid": 4242, "rail_version": "3.9.0"})
        assert r.status_code == 200, r.text
        t = r.json()["thread"]
        assert t["state"] == "open" and t["pid"] == 4242 and t["closed_at"] is None
        # The owner reads its raw conversation id back; everyone gets the fingerprint.
        mine = client.get("/api/v1/wake/threads").json()
        assert mine["scope"] == "self"
        row = next(x for x in mine["threads"] if x["object_id"] == tid)
        assert row["conversation_id"] == "sid-one" and len(row["conversation"]) == 8
        by_thread = client.get(f"/api/v1/wake/threads?thread=ticket:{tid}").json()
        assert by_thread["scope"] == f"thread:ticket:{tid}"
        assert "conversation_id" not in by_thread["threads"][0]
        # A second upsert on the same (kind, object, identity) moves the row, never duplicates.
        client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "sid-one",
            "holder": "stream", "pid": 4343})
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        assert len([x for x in rows if x["object_id"] == tid]) == 1
        assert next(x for x in rows if x["object_id"] == tid)["pid"] == 4343
        # close stamps closed_at. A one-shot / resume on the SAME conversation
        # reopens the row without touching opened_at — the row is current
        # state, opened_at is the conversation's open time (staging #100:
        # two rewrites left the thread's real open time nowhere on the plane).
        r = client.patch(f"/api/v1/wake/threads/{t['id']}", json={"state": "closed"})
        assert r.status_code == 200 and r.json()["thread"]["closed_at"]
        r = client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "sid-one",
            "holder": "oneshot", "pid": None})
        t1 = r.json()["thread"]
        assert t1["state"] == "open" and t1["closed_at"] is None and t1["pid"] is None
        assert t1["opened_at"] == t["opened_at"]
        # A NEW conversation on the row (a fresh wake, not a resume) moves it.
        client.patch(f"/api/v1/wake/threads/{t['id']}", json={"state": "closed"})
        r = client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "sid-two",
            "holder": "stream", "pid": 5151})
        t2 = r.json()["thread"]
        assert t2["state"] == "open" and t2["closed_at"] is None
        assert t2["conversation_id"] == "sid-two" and t2["opened_at"] > t["opened_at"]
        # Bad inputs are 400s, not silent.
        assert client.post("/api/v1/wake/threads", json={
            "kind": "note", "object_id": tid, "conversation_id": "x"}).status_code == 400
        assert client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "x",
            "state": "paused"}).status_code == 400
        assert client.get("/api/v1/wake/threads?state=asleep").status_code == 400

    def test_write_with_session_binds_first_conversation_only(self, client, tmp_db):
        tid = client.post("/api/v1/tickets", json={"summary": "bind", "body": "b"},
                          headers={"X-Session-Id": "conv-filer"}).json()["id"]
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        mine = next(x for x in rows if x["object_id"] == tid)
        # Filing bound the filer's conversation (agent 1 is "across" its tickets).
        assert mine["conversation_id"] == "conv-filer" and mine["holder"] == "conversation"
        # A later write from ANOTHER conversation of the same identity does not
        # steal the binding while the first is open.
        client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "hi"},
                    headers={"X-Session-Id": "conv-other"})
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        assert next(x for x in rows if x["object_id"] == tid)["conversation_id"] == "conv-filer"
        # Once closed, the next write rebinds.
        client.patch(f"/api/v1/wake/threads/{mine['id']}", json={"state": "closed"})
        client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "again"},
                    headers={"X-Session-Id": "conv-other"})
        rows = client.get("/api/v1/wake/threads").json()["threads"]
        row = next(x for x in rows if x["object_id"] == tid)
        assert row["conversation_id"] == "conv-other" and row["state"] == "open"
        # No nonce, no binding.
        tid2 = client.post("/api/v1/tickets", json={"summary": "nonce-less", "body": "b"}
                           ).json()["id"]
        assert not [x for x in client.get("/api/v1/wake/threads").json()["threads"]
                    if x["object_id"] == tid2]

    def test_delivered_rows_are_never_offered_again(self, client, tmp_db):
        tid = client.post("/api/v1/tickets", json={"summary": "deliver", "body": "b"}
                          ).json()["id"]
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw)
            peer.post(f"/api/v1/tickets/{tid}/comments", json={"body": "one"})
            peer.post(f"/api/v1/tickets/{tid}/comments", json={"body": "two"})
        rows = [r for r in client.get("/api/v1/notifications?unread=true&undelivered=true"
                                      ).json()["notifications"] if r.get("ticket_id") == tid]
        assert len(rows) == 2
        # Bind an open conversation, deliver one row into it.
        client.post("/api/v1/wake/threads", json={
            "kind": "ticket", "object_id": tid, "conversation_id": "conv-A", "holder": "stream"})
        r = client.post("/api/v1/notifications/delivered",
                        json={"ids": [rows[0]["id"]], "conversation_id": "conv-A"})
        assert r.status_code == 200 and r.json()["delivered"] == 1
        left = [r for r in client.get("/api/v1/notifications?unread=true&undelivered=true"
                                      ).json()["notifications"] if r.get("ticket_id") == tid]
        assert [x["id"] for x in left] == [rows[1]["id"]]
        # Still unread and still in the plain inbox — delivery is not an ack.
        plain = [r for r in client.get("/api/v1/notifications?unread=true"
                                       ).json()["notifications"] if r.get("ticket_id") == tid]
        assert len(plain) == 2 and any(x["delivered_to"] == "conv-A" for x in plain)
        # The binding records the delivery time.
        me = next(x for x in client.get("/api/v1/wake/threads").json()["threads"]
                  if x["object_id"] == tid)
        assert me["last_delivery_at"]
        # Close the conversation: delivered is final — the row is NOT offered again.
        client.patch(f"/api/v1/wake/threads/{me['id']}", json={"state": "closed"})
        again = [r for r in client.get("/api/v1/notifications?unread=true&undelivered=true"
                                       ).json()["notifications"] if r.get("ticket_id") == tid]
        assert [x["id"] for x in again] == [rows[1]["id"]]
        assert client.post("/api/v1/notifications/delivered",
                           json={"ids": [], "conversation_id": "x"}).status_code == 400

    def test_thread_view_is_visibility_gated_and_fleet_is_admin_only(self, client, tmp_db):
        assert client.get("/api/v1/wake/threads?thread=ticket:999999").status_code == 404
        assert client.get("/api/v1/wake/threads?thread=nope").status_code == 400
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "thread-plain")
            assert peer.get("/api/v1/wake/threads?all=true").status_code == 403
            assert peer.get("/api/v1/wake/threads").json()["scope"] == "self"
        assert client.get("/api/v1/wake/threads?all=true").json()["scope"] == "fleet"

    def test_self_assign_never_rings_and_rows_carry_actor_session(self, client, tmp_db):
        # 0.9.1: an identity assigning a ticket to ITSELF gets no row — its own
        # write never rings it (until 0.9.0 this was "asking for an instance",
        # and it birthed twins, doc 221). Every row a PEER writes still carries
        # the writer's session fingerprint.
        tid = client.post("/api/v1/tickets", json={"summary": "brief myself", "body": "go",
                                                   "assignee": "admin"},
                          headers={"X-Session-Id": "interactive-1"}).json()["id"]
        rows = [r for r in client.get("/api/v1/notifications?unread=true").json()["notifications"]
                if r.get("ticket_id") == tid]
        assert rows == []
        # A plain comment on my own ticket does not notify me either.
        client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "note to self"},
                    headers={"X-Session-Id": "interactive-1"})
        rows = [r for r in client.get("/api/v1/notifications?unread=true").json()["notifications"]
                if r.get("ticket_id") == tid]
        assert rows == []
        # Another identity's comment carries ITS fingerprint.
        with TestClient(_get_app()) as raw:
            peer = self._peer(client, raw, "fp-peer")
            peer.post(f"/api/v1/tickets/{tid}/comments", json={"body": "hi"},
                      headers={"X-Session-Id": "peer-sess"})
        rows = [r for r in client.get("/api/v1/notifications?unread=true").json()["notifications"]
                if r.get("ticket_id") == tid and r["event_type"] == "ticket.commented"]
        assert rows and rows[0]["payload"]["actor_session"] == __import__("hashlib").sha256(b"peer-sess").hexdigest()[:8]


    def test_self_write_never_reaches_own_held_instance(self, client, tmp_db):
        # My interactive session resolves a ticket my rail-held instance holds.
        # 0.9.1: the instance is NOT told by a row — my own write never rings
        # me; the object's new state is what it sees next (doc 221: the
        # briefing path birthed twins of the writer on its own threads).
        tid = client.post("/api/v1/tickets", json={"summary": "held by me", "body": "b",
                                                   "assignee": "admin"},
                          headers={"X-Session-Id": "interactive-2"}).json()["id"]
        client.post("/api/v1/notifications/ack",
                    json={"ids": [r["id"] for r in client.get("/api/v1/notifications?unread=true").json()["notifications"] if r.get("ticket_id") == tid]})
        client.post("/api/v1/wake/threads", json={"kind": "ticket", "object_id": tid,
                                                  "conversation_id": "holder-sid", "holder": "stream", "pid": 1})
        client.patch(f"/api/v1/tickets/{tid}", json={"status": "resolved"},
                     headers={"X-Session-Id": "interactive-2"})
        rows = [r for r in client.get("/api/v1/notifications?unread=true").json()["notifications"]
                if r.get("ticket_id") == tid]
        assert rows == []
        # And the holder's OWN write does not notify the identity either.
        client.post(f"/api/v1/tickets/{tid}/comments", json={"body": "from the holder"},
                    headers={"X-Session-Id": "holder-sid"})
        rows2 = [r for r in client.get("/api/v1/notifications?unread=true").json()["notifications"]
                 if r.get("ticket_id") == tid]
        assert len(rows2) == len(rows)


class TestServedTexts:
    """Every text the registry hands a person or an agent with a token — the
    activation answer, the regeneration message, the user guide, the device
    guide — teaches one way in: a 0600 header file, the served skill, the rail.
    Step 3 of the lifecycle rollout (doc 235, 2026-09-08) found all four still
    teaching the retired shape: the bearer on a command line, a project-local
    `.samcloud/token`, and `hooks-install` (a 404 since 0.10.0)."""

    RETIRED = ("hooks-install", "./.samcloud/token", "<project>/.samcloud/token",
               "Bearer $TOKEN", "Bearer $TOK", "Bearer $AGENT_TOK", "Bearer <token>",
               "digest each prompt", "notif-hook", "stop-hook")
    REQUIRED = ("token.hdr", "wake-install", "skills/samcloud-agent/SKILL.md")

    def _check(self, text, *, token=None):
        for bad in self.RETIRED:
            assert bad not in text, f"retired teaching in served text: {bad!r}"
        if token:
            # the token itself may appear once, on its own line, never inside a command
            for line in text.splitlines():
                if token in line:
                    assert "curl" not in line and "echo" not in line and "printf" not in line, line
        for good in self.REQUIRED:
            assert good in text, f"served text lacks {good!r}"

    def test_activation_regeneration_and_guides(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            inv = client.post("/api/v1/users/invite",
                              json={"username": "newseat", "scopes": [], "role": "agent"})
            act = raw.post("/api/v1/users/activate",
                           json={"token": inv.json()["token"], "password": "test123"})
            assert act.status_code == 200, act.json()
            self._check("\n".join(act.json()["next_steps"]))
            uid, token = act.json()["user_id"], act.json()["token"]

            regen = client.post(f"/api/v1/users/{uid}/regenerate-token")
            assert regen.status_code == 200, regen.json()
            self._check(regen.json()["message"], token=regen.json()["token"])

            guide = AuthedClient(raw, regen.json()["token"]).get("/api/v1/users/me/guide")
            assert guide.status_code == 200
            self._check(guide.text)

            _register_device(client, "dev-guide", "host-guide")
            dg = client.get("/api/v1/devices/dev-guide/guide")
            assert dg.status_code == 200, dg.text
            for bad in ("Bearer $TOK", "Bearer $AGENT_TOK", "hooks-install"):
                assert bad not in dg.text, bad
            assert 'token.hdr' in dg.text and "wake-install" in dg.text


class TestUniversalThread:
    """Threads v4 (doc 239): the thread is the unit of work. Objects belong to
    one, instances are bound to one, every row is stamped with one, and what a
    bound conversation makes joins its thread. Built after the first wide
    stage (doc 238) birthed four instances of one identity for one brief."""

    def _agent(self, client, raw, name):
        token, _ = _invite_and_activate(client, raw, name, [])
        return AuthedClient(raw, token)

    def test_every_object_has_a_thread_and_rows_are_stamped(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-0")
            tid = client.post("/api/v1/tickets", json={"summary": "root", "body": "b", "assignee": "seat-0"}).json()
            assert tid["thread_id"]
            t = client.get(f"/api/v1/threads/{tid['thread_id']}").json()
            assert t["root"] == f"ticket:{tid['id']}" and t["state"] == "open" and t["open_members"] is True
            assert [m["id"] for m in t["members"]["tickets"]] == [tid["id"]]
            # the assignment row and a comment's row carry the thread
            client.post(f"/api/v1/tickets/{tid['id']}/comments", json={"body": "hello"},
                        headers={"X-Session-Id": "conv-x"})
            rows = [r for r in seat.get("/api/v1/notifications?unread=true&limit=50").json()["notifications"]
                    if r.get("ticket_id") == tid["id"]]
            assert len(rows) >= 2 and all(r.get("thread_id") == tid["thread_id"] for r in rows)

    def test_what_a_bound_conversation_makes_joins_its_thread(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-a")
            # the seat is woken into ticket T (its holder binds via the rail's upsert)
            t = client.post("/api/v1/tickets", json={"summary": "work", "body": "b", "assignee": "seat-a"}).json()
            seat.post("/api/v1/wake/threads", json={"thread_id": t["thread_id"], "conversation_id": "holder-1",
                                                    "holder": "stream", "pid": 4242})
            # everything it makes from that conversation is a member of T
            slug = client.post("/api/v1/projects", json={"name": "P"}).json()["slug"]
            task = seat.post(f"/api/v1/projects/{slug}/tasks", json={"title": "step"},
                             headers={"X-Session-Id": "holder-1"}).json()
            doc = seat.post("/api/v1/docs", json={"title": "notes", "body": "n"},
                            headers={"X-Session-Id": "holder-1"}).json()
            tk = seat.post("/api/v1/tickets", json={"summary": "follow-up", "body": "b", "assignee": "seat-a"},
                           headers={"X-Session-Id": "holder-1"}).json()
            th = client.get(f"/api/v1/threads/{t['thread_id']}").json()
            assert task["id"] in [m["id"] for m in th["members"]["tasks"]]
            assert doc["id"] in [m["id"] for m in th["members"]["docs"]]
            assert tk["id"] in [m["id"] for m in th["members"]["tickets"]] and tk["thread_id"] == t["thread_id"]
            # a comment on the task binds on the THREAD, not a second thread for the task
            client.post(f"/api/v1/tasks/{task['id']}/comments", json={"body": "on the task"},
                        headers={"X-Session-Id": "conv-admin"})
            rows = client.get(f"/api/v1/wake/threads?thread=task:{task['id']}").json()
            assert rows["thread_id"] == t["thread_id"]
            assert {r["identity"] for r in rows["threads"]} >= {"seat-a", "admin"}
            # and the task's rows are stamped with T, so the seat's holder hears them as one thread
            trows = [r for r in seat.get("/api/v1/notifications?unread=true").json()["notifications"]
                     if r.get("task_id") == task["id"]]
            assert trows and all(r["thread_id"] == t["thread_id"] for r in trows)
            # X-Thread-Id names a thread explicitly, from a surface bound to none
            other = client.post("/api/v1/tickets", json={"summary": "elsewhere", "body": "b", "assignee": "seat-a"},
                                headers={"X-Thread-Id": str(t["thread_id"])}).json()
            assert other["thread_id"] == t["thread_id"]
            r = client.post("/api/v1/tickets", json={"summary": "bad", "body": "b"}, headers={"X-Thread-Id": "999999"})
            assert r.status_code == 400

    def test_open_members_is_the_lifecycle_and_docs_never_count(self, client):
        t = client.post("/api/v1/tickets", json={"summary": "work", "body": "b", "assignee": "admin"}).json()
        client.post("/api/v1/docs", json={"title": "record", "body": "r"}, headers={"X-Thread-Id": str(t["thread_id"])})
        slug = client.post("/api/v1/projects", json={"name": "Q"}).json()["slug"]
        task = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "s"},
                           headers={"X-Thread-Id": str(t["thread_id"])}).json()
        assert client.get(f"/api/v1/threads/{t['thread_id']}").json()["open_members"] is True
        client.patch(f"/api/v1/tickets/{t['id']}", json={"status": "resolved"})
        assert client.get(f"/api/v1/threads/{t['thread_id']}").json()["open_members"] is True   # the task
        client.patch(f"/api/v1/tasks/{task['id']}", json={"status": "done"})
        th = client.get(f"/api/v1/threads/{t['thread_id']}").json()
        assert th["open_members"] is False and th["state"] == "open"   # the doc keeps nothing open; state is a person's

    def test_a_person_closing_a_thread_writes_thread_closed_to_every_bound_instance(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-b")
            t = client.post("/api/v1/tickets", json={"summary": "w", "body": "b", "assignee": "seat-b"}).json()
            seat.post("/api/v1/wake/threads", json={"thread_id": t["thread_id"], "conversation_id": "h-b",
                                                    "holder": "stream", "pid": 1})
            r = seat.patch(f"/api/v1/threads/{t['thread_id']}", json={"state": "closed"})
            assert r.status_code == 403                       # an agent does not close a thread
            r = client.patch(f"/api/v1/threads/{t['thread_id']}", json={"state": "closed"})
            assert r.status_code == 200 and r.json()["state"] == "closed"
            rows = [x for x in seat.get("/api/v1/notifications?unread=true").json()["notifications"]
                    if x["event_type"] == "thread.closed"]
            assert len(rows) == 1 and rows[0]["thread_id"] == t["thread_id"]
            # …and it is deliverable to the holder's conversation, as a row on the thread it holds
            scoped = seat.get(f"/api/v1/notifications?for_conversation=h-b&unread=true").json()["notifications"]
            assert any(x["event_type"] == "thread.closed" for x in scoped)

    def test_a_mention_inside_quoted_text_addresses_nobody(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "quoted-one")
            t = client.post("/api/v1/tickets", json={"summary": "design", "body": "b", "assignee": "admin"}).json()
            client.post(f"/api/v1/tickets/{t['id']}/comments",
                        json={"body": "the form is `@quoted-one/abcd1234`, and\n> someone wrote @quoted-one\n```\n@quoted-one\n```"})
            rows = seat.get("/api/v1/notifications?unread=true&limit=100").json()["notifications"]
            assert not any(r["event_type"] == "ticket.mentioned" for r in rows)
            client.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "@quoted-one can you look?"})
            rows = seat.get("/api/v1/notifications?unread=true&limit=100").json()["notifications"]
            assert any(r["event_type"] == "ticket.mentioned" and r["thread_id"] == t["thread_id"] for r in rows)

    def test_a_bare_create_is_never_adopted_by_whatever_is_running(self, client):
        """doc 239 D12: the lone-live-holder inference is dropped in favour of
        `X-Session-Id` on every write. It read a bare curl from ANYWHERE on the
        seat — a cron job, a shell, another tool — as the holder's own work,
        which is the inference doc 265 §2 forbids by name. `sc-new` has stamped
        the header since 0.12.11; what arrives unstamped joins the identity's
        own thread (#886) and says so, rather than being quietly adopted."""
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-c")
            t = client.post("/api/v1/tickets", json={"summary": "w", "body": "b", "assignee": "seat-c"}).json()
            seat.post("/api/v1/wake/threads", json={"thread_id": t["thread_id"], "conversation_id": "h-c",
                                                    "holder": "stream", "pid": 5})
            slug = client.post("/api/v1/projects", json={"name": "R"}).json()["slug"]
            task = seat.post(f"/api/v1/projects/{slug}/tasks", json={"title": "bare curl"}).json()
            doc = seat.post("/api/v1/docs", json={"title": "bare", "body": "b"}).json()
            th = client.get(f"/api/v1/threads/{t['thread_id']}").json()
            assert task["id"] not in [m["id"] for m in th["members"]["tasks"]]
            assert doc["id"] not in [m["id"] for m in th["members"]["docs"]]
            # ...and both now SAY which thread they started, which a doc and a
            # task create never did (0.12.19): sc-new could not confirm a join.
            assert task["thread_id"] and task["thread_id"] != t["thread_id"]
            assert doc["thread_id"] and doc["thread_id"] != t["thread_id"]
            # the same create, stamped, joins the holder's thread as always
            stamped = seat.post(f"/api/v1/projects/{slug}/tasks", json={"title": "stamped"},
                                headers={"X-Session-Id": "h-c"}).json()
            assert stamped["thread_id"] == t["thread_id"]

    def test_a_holder_writing_on_another_thread_binds_nothing_there(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-d")
            mine = client.post("/api/v1/tickets", json={"summary": "mine", "body": "b", "assignee": "seat-d"},
                               headers={"X-Session-Id": "work-mine"}).json()
            seat.post("/api/v1/wake/threads", json={"thread_id": mine["thread_id"], "conversation_id": "h-d",
                                                    "holder": "stream", "pid": 7})
            other = client.post("/api/v1/tickets", json={"summary": "theirs", "body": "b", "assignee": "admin"},
                                headers={"X-Session-Id": "work-theirs"}).json()
            seat.post(f"/api/v1/tickets/{other['id']}/comments", json={"body": "a note from a holder"},
                      headers={"X-Session-Id": "h-d"})
            rows = client.get(f"/api/v1/wake/threads?thread_id={other['thread_id']}").json()["threads"]
            assert not any(r["identity"] == "seat-d" for r in rows)          # the holder stays on its own thread
            # …while a person's session (no holder binding) does take a thread by writing on it
            client.post(f"/api/v1/tickets/{other['id']}/comments", json={"body": "a person"},
                        headers={"X-Session-Id": "person-1"})
            rows = client.get(f"/api/v1/wake/threads?thread_id={other['thread_id']}").json()["threads"]
            assert any(r["identity"] == "admin" and r["holder"] == "conversation" for r in rows)

    def test_a_bound_instance_hears_its_objects_not_every_member(self, client):
        """0.12.74 (#902): the thread is scope and wake; delivery is the
        object's. A holder on a thread hears the objects it is on — here the
        ticket it is assigned — and nothing about a task or a doc someone else
        made and changed in the same thread. Until 0.12.73 this test pinned the
        opposite ("a bound instance hears every member event"), a rule the
        admin had written into doc 239. x's comment on a ticket doesn't go to
        z just because z is on the thread; deliver-all does not scale."""
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-e")
            t = client.post("/api/v1/tickets", json={"summary": "w", "body": "b", "assignee": "seat-e"}).json()
            seat.post("/api/v1/wake/threads", json={"thread_id": t["thread_id"], "conversation_id": "h-e",
                                                    "holder": "stream", "pid": 8})
            slug = client.post("/api/v1/projects", json={"name": "S"}).json()["slug"]
            task = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "someone else's task"},
                               headers={"X-Thread-Id": str(t["thread_id"])}).json()
            doc = client.post("/api/v1/docs", json={"title": "someone else's doc", "body": "b"},
                              headers={"X-Thread-Id": str(t["thread_id"])}).json()
            assert task["thread_id"] == doc["thread_id"] == t["thread_id"]     # same thread, by name
            client.patch(f"/api/v1/tasks/{task['id']}", json={"status": "done"})
            client.post(f"/api/v1/docs/{doc['id']}/revisions", json={"body": "b2", "note": "edit"})
            client.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "on the ticket you hold"})
            rows = seat.get("/api/v1/notifications?unread=true&limit=100").json()["notifications"]
            kinds = {(r["event_type"], r.get("ticket_id") or r.get("task_id") or r.get("doc_id")) for r in rows}
            assert ("ticket.commented", t["id"]) in kinds, kinds            # on the object: heard
            assert ("task.updated", task["id"]) not in kinds, kinds          # not on it: nothing
            assert ("doc.revised", doc["id"]) not in kinds, kinds
            # A contribution alone does not enrol it; a directed reply still reaches it.
            seat.post(f"/api/v1/tasks/{task['id']}/comments", json={"body": "noted"},
                      headers={"X-Session-Id": "h-e"})
            client.post(f"/api/v1/tasks/{task['id']}/comments", json={"body": "reply"})
            rows = seat.get("/api/v1/notifications?unread=true&limit=100").json()["notifications"]
            assert not any(r["event_type"] == "task.commented" and r.get("task_id") == task["id"] for r in rows), rows
            client.post(f"/api/v1/tasks/{task['id']}/comments", json={"body": "@seat-e a directed reply"})
            rows = seat.get("/api/v1/notifications?unread=true&limit=100").json()["notifications"]
            assert any(r["event_type"] == "task.mentioned" and r.get("task_id") == task["id"] for r in rows), rows

    def test_a_newcomer_gets_the_last_messages_whole(self, client):
        """A mention into a live thread ships the last comments whole (0.11.5),
        not an 800-char preview."""
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "newcomer")
            t = client.post("/api/v1/tickets", json={"summary": "long", "body": "b", "assignee": "admin"}).json()
            long = "x" * 3000
            client.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": long})
            client.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "@newcomer see above"})
            rows = [r for r in seat.get("/api/v1/notifications?unread=true").json()["notifications"]
                    if r["event_type"] == "ticket.mentioned"]
            assert rows, "no mention row"
            tail = rows[-1]["payload"]["thread_tail"]
            assert any(tc.get("body") == long and not tc.get("truncated") for tc in tail), [len(tc.get("body", "")) for tc in tail]

    def test_one_comment_is_one_row_per_reader_on_every_plane(self, client):
        """A comment that mentions a reader who is ALSO bound to the thread (or
        watching the object) reaches that reader once — the mention row, with
        the tail — never a second `*.commented` row (doc 246 D1: rows 28874 +
        28875 for one comment, six messages spent on the phantom duplicate)."""
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-d1")
            t = client.post("/api/v1/tickets", json={"summary": "w", "body": "the brief", "assignee": "admin"}).json()
            seat.post("/api/v1/wake/threads", json={"thread_id": t["thread_id"], "conversation_id": "h-d1",
                                                    "holder": "stream", "pid": 9})
            slug = client.post("/api/v1/projects", json={"name": "D1"}).json()["slug"]
            task = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "member"},
                               headers={"X-Thread-Id": str(t["thread_id"])}).json()
            doc = client.post("/api/v1/docs", json={"title": "member doc", "body": "b"},
                              headers={"X-Thread-Id": str(t["thread_id"])}).json()
            seat.post(f"/api/v1/docs/{doc['id']}/subscribe")      # a watcher who is then mentioned
            c1 = client.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "@seat-d1 one question?"}).json()
            c2 = client.post(f"/api/v1/tasks/{task['id']}/comments", json={"body": "@seat-d1 on the task"}).json()
            c3 = client.post(f"/api/v1/docs/{doc['id']}/comments", json={"body": "@seat-d1 on the doc"}).json()
            rows = seat.get("/api/v1/notifications?unread=true&limit=100").json()["notifications"]
            for kind, obj, cid in (("ticket", t, c1), ("task", task, c2), ("doc", doc, c3)):
                mine = [r for r in rows if r.get(f"{kind}_id") == obj["id"]
                        and (r.get("payload") or {}).get("comment_id") == cid["comment_id"]]
                assert len(mine) == 1, (kind, [(r["event_type"], r["id"]) for r in mine])
                assert mine[0]["event_type"] == f"{kind}.mentioned"
                assert mine[0]["payload"]["body"]                   # the doc mention carries its text too
            # the comment rows OTHERS get say whom the comment addresses (0.12.2)
            w = self._agent(client, raw, "seat-w")
            w.post(f"/api/v1/tickets/{t['id']}/subscribe"); w.post(f"/api/v1/docs/{doc['id']}/subscribe")
            c4 = client.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "@seat-d1 again"}).json()
            c5 = client.post(f"/api/v1/docs/{doc['id']}/comments", json={"body": "@seat-d1 on the doc again"}).json()
            arows = [r for r in w.get("/api/v1/notifications?unread=true&limit=100").json()["notifications"]
                     if (r.get("payload") or {}).get("comment_id") in (c4["comment_id"], c5["comment_id"])]
            assert len(arows) == 2 and all(r["payload"].get("mentions") == ["seat-d1"] for r in arows), \
                [(r["event_type"], r["payload"].get("mentions")) for r in arows]

    def test_a_mention_row_carries_the_threads_root(self, client):
        """A mention introduces the authoritative root, without copying its body."""
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-d2")
            t = client.post("/api/v1/tickets", json={"summary": "the work", "body": "what this thread is for",
                                                     "assignee": "admin"}).json()
            slug = client.post("/api/v1/projects", json={"name": "D2"}).json()["slug"]
            task = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "a step"},
                               headers={"X-Thread-Id": str(t["thread_id"])}).json()
            client.post(f"/api/v1/tasks/{task['id']}/comments", json={"body": "@seat-d2 can you take this step?"})
            rows = [r for r in seat.get("/api/v1/notifications?unread=true").json()["notifications"]
                    if r["event_type"] == "task.mentioned"]
            assert rows, "no mention row"
            root = rows[-1]["payload"]["root"]
            assert root == {"kind": "ticket", "id": t["id"], "title": "the work",
                            "read": f"GET /api/v1/tickets/{t['id']}"}
            d = client.post("/api/v1/docs", json={"title": "a doc", "body": "doc body @seat-d2"},
                            headers={"X-Session-Id": "work-doc"}).json()
            drow = [r for r in seat.get("/api/v1/notifications?unread=true").json()["notifications"]
                    if r["event_type"] == "doc.mentioned" and r.get("doc_id") == d["id"]]
            assert drow and drow[-1]["payload"]["root"]["kind"] == "doc"
            assert drow[-1]["payload"]["read_required"] and "body" not in drow[-1]["payload"]

    def test_a_conversation_makes_everything_in_its_own_thread(self, client):
        """#886: a conversation's first create starts its thread; everything it
        makes after that joins it — writing on another thread does not move
        it there (binding by writing is not holding, 0.12.1), and there is no
        create that starts a second. Another conversation has its own. A
        holder's one binding still scopes its creations."""
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-s")
            first = client.post("/api/v1/tickets", json={"summary": "one", "body": "b", "assignee": "seat-s"},
                                headers={"X-Session-Id": "surface-1"}).json()
            elsewhere = client.post("/api/v1/tickets", json={"summary": "theirs", "body": "b"},
                                    headers={"X-Session-Id": "surface-2"}).json()
            assert elsewhere["thread_id"] != first["thread_id"]       # another conversation, its own thread
            client.post(f"/api/v1/tickets/{elsewhere['id']}/comments", json={"body": "noted"},
                        headers={"X-Session-Id": "surface-1"})          # surface-1 writes on surface-2's work
            second = client.post("/api/v1/tickets", json={"summary": "two", "body": "b", "assignee": "seat-s"},
                                 headers={"X-Session-Id": "surface-1"}).json()
            slug = client.post("/api/v1/projects", json={"name": "S"}).json()["slug"]
            task = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "three"},
                               headers={"X-Session-Id": "surface-1"}).json()
            doc = client.post("/api/v1/docs", json={"title": "four", "body": "b"},
                              headers={"X-Session-Id": "surface-1"}).json()
            assert second["thread_id"] == task["thread_id"] == doc["thread_id"] == first["thread_id"]
            # a holder's one binding still scopes its creations
            seat.post("/api/v1/wake/threads", json={"thread_id": elsewhere["thread_id"], "conversation_id": "h-s",
                                                    "holder": "stream", "pid": 5})
            made = seat.post("/api/v1/tickets", json={"summary": "from the holder", "body": "b", "assignee": "seat-s"},
                             headers={"X-Session-Id": "h-s"}).json()
            assert made["thread_id"] == elsewhere["thread_id"]

    def test_a_conversation_live_before_homes_keeps_the_thread_it_started(self, client):
        """0.12.69: homes are recorded from a conversation's first create after
        0.12.65; a conversation that was live before that had started threads
        under the old rule (each create its own) and has no home row. Its next
        create joins the open thread it started last — not a new one. The
        admin's own session got thread 1779 for its overview ticket on
        2026-10-07 while it had been on 1775 since 10-05 (#893)."""
        app = _get_app()
        with TestClient(app) as raw:
            import db as _db
            seat = self._agent(client, raw, "seat-h")
            # Two threads started by one conversation under the old rule: made
            # here as two conversations, then re-keyed to one, with no record.
            older = client.post("/api/v1/tickets", json={"summary": "older", "body": "b"},
                                headers={"X-Session-Id": "pre-0.12.65"}).json()
            newer = client.post("/api/v1/tickets", json={"summary": "newer", "body": "b"},
                                headers={"X-Session-Id": "pre-0.12.65-b"}).json()
            assert newer["thread_id"] != older["thread_id"]
            _db.execute("UPDATE threads SET conversation_id = 'pre-0.12.65' "
                        "WHERE conversation_id = 'pre-0.12.65-b'")
            _db.execute("DELETE FROM conversation_threads WHERE conversation_id LIKE 'pre-0.12.65%'")
            third = client.post("/api/v1/tickets", json={"summary": "third", "body": "b"},
                                headers={"X-Session-Id": "pre-0.12.65"}).json()
            assert third["thread_id"] == newer["thread_id"], \
                "no record: the open thread this conversation started last is its thread"
            fourth = client.post("/api/v1/docs", json={"title": "fourth", "body": "b"},
                                 headers={"X-Session-Id": "pre-0.12.65"}).json()
            assert fourth["thread_id"] == newer["thread_id"]       # and it is recorded now
            # A thread it only WROTE on is not one it started; a closed one is skipped.
            theirs = seat.post("/api/v1/tickets", json={"summary": "theirs", "body": "b"},
                               headers={"X-Session-Id": "someone-else"}).json()
            _db.execute("DELETE FROM conversation_threads WHERE conversation_id = 'pre-0.12.65'")
            _db.execute("UPDATE work_threads SET state = 'closed' WHERE id = ?", (newer["thread_id"],))
            client.post(f"/api/v1/tickets/{theirs['id']}/comments", json={"body": "hi"},
                        headers={"X-Session-Id": "pre-0.12.65"})
            fifth = client.post("/api/v1/tickets", json={"summary": "fifth", "body": "b"},
                                headers={"X-Session-Id": "pre-0.12.65"}).json()
            assert fifth["thread_id"] == older["thread_id"], \
                "the closed one is skipped, the written-on one is not mine: the older started thread"

    def test_an_unstamped_create_joins_the_identitys_own_thread(self, client):
        """#886: a write that names no conversation is the identity's — it
        joins the identity's own thread, never a new one, and is never adopted
        by a holder that happens to be running (doc 239 D12)."""
        app = _get_app()
        with TestClient(app) as raw:
            seat = self._agent(client, raw, "seat-u")
            a = seat.post("/api/v1/tickets", json={"summary": "cron one", "body": "b"}).json()
            b = seat.post("/api/v1/tickets", json={"summary": "cron two", "body": "b"}).json()
            assert a["thread_id"] == b["thread_id"]
            other = client.post("/api/v1/tickets", json={"summary": "admin's", "body": "b"}).json()
            assert other["thread_id"] != a["thread_id"]                 # each identity has its own
            # a person closing the identity's thread does not strand the next write
            client.patch(f"/api/v1/threads/{a['thread_id']}", json={"state": "closed"})
            c = seat.post("/api/v1/tickets", json={"summary": "cron three", "body": "b"}).json()
            assert c["thread_id"] == a["thread_id"]
            assert client.get(f"/api/v1/threads/{a['thread_id']}").json()["state"] == "open"

    def test_a_subtask_lives_in_its_parents_thread(self, client):
        """#886: a parent is the work a task belongs to. Created under one, or
        given one later, the task is in the parent's thread — unless it roots a
        thread other work has joined, which it does not abandon."""
        slug = client.post("/api/v1/projects", json={"name": "Sub"}).json()["slug"]
        parent = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "parent"},
                             headers={"X-Session-Id": "conv-p"}).json()
        child = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "child", "parent_task_id": parent["id"]},
                            headers={"X-Session-Id": "conv-q"}).json()
        assert child["thread_id"] == parent["thread_id"]
        loose = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "loose"},
                            headers={"X-Session-Id": "conv-r"}).json()
        assert loose["thread_id"] != parent["thread_id"]
        r = client.patch(f"/api/v1/tasks/{loose['id']}", json={"parent_task_id": parent["id"]}).json()
        assert r["thread_id"] == parent["thread_id"] and r["notes"]
        rooted = client.post(f"/api/v1/projects/{slug}/tasks", json={"title": "rooted"},
                             headers={"X-Session-Id": "conv-s"}).json()
        client.post("/api/v1/docs", json={"title": "joined it", "body": "b"}, headers={"X-Session-Id": "conv-s"})
        r = client.patch(f"/api/v1/tasks/{rooted['id']}", json={"parent_task_id": parent["id"]}).json()
        assert r["thread_id"] == rooted["thread_id"] and "stays" in r["notes"][0]

    def test_move_an_object_into_a_thread(self, client):
        a = client.post("/api/v1/tickets", json={"summary": "a", "body": "b", "assignee": "admin"},
                        headers={"X-Session-Id": "conv-a"}).json()
        b = client.post("/api/v1/tickets", json={"summary": "b", "body": "b", "assignee": "admin"},
                        headers={"X-Session-Id": "conv-b"}).json()
        assert a["thread_id"] != b["thread_id"]
        r = client.post(f"/api/v1/threads/{a['thread_id']}/members", json={"kind": "ticket", "id": b["id"]})
        assert r.status_code == 200
        assert [m["id"] for m in r.json()["members"]["tickets"]] == [a["id"], b["id"]]
        assert client.get(f"/api/v1/threads/of/ticket/{b['id']}").json()["id"] == a["thread_id"]


# ===========================================================================
# Scope v1, train 0 — the door is shut (doc 261 / doc 262)
# ===========================================================================

class TestAccessDoor:
    """Every route answers a bearer; a read-one is gated like its list and an
    object outside scope is hidden (404, doc 261 decision 4); the gateway lists
    and admits by the one predicate that the API uses (membership, not the
    token's scope string). Doc 262 is the inventory these rows close."""

    def _world(self, client):
        """Two devices, one in group `g`; a service on each; a resource on each."""
        _register_device(client, "dev-in", "host-in")
        _register_device(client, "dev-out", "host-out")
        _create_group(client, "g", "G")
        client.post("/api/v1/groups/g/devices", json={"device_id": "dev-in"})
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "svc-in", "dev-in", 8080)
            _register_service(client, "svc-out", "dev-out", 8081)
        for dev in ("dev-in", "dev-out"):
            r = client.post("/api/v1/resources", json={
                "device_id": dev, "name": "gpu-0", "type": "gpu", "provider": "test",
                "model": "test-gpu", "allocation_mode": "shared",
                "specs": {"gpu_memory_mb": 24576}})
            assert r.status_code == 200, r.text

    def test_users_take_a_bearer(self, client):
        app = _get_app()
        with TestClient(app) as raw:
            assert raw.get("/api/v1/users").status_code == 401
            assert raw.get("/api/v1/users/1").status_code == 401

    def test_a_user_is_read_by_self_admin_or_a_fellow_member(self, client, tmp_db):
        _create_group(client, "g", "G")
        _create_group(client, "h", "H")
        app = _get_app()
        with TestClient(app) as raw:
            t1, u1 = _invite_and_activate(client, raw, "one", ["group:g"])
            t2, u2 = _invite_and_activate(client, raw, "two", ["group:g"])
            t3, u3 = _invite_and_activate(client, raw, "three", ["group:h"])
            one = AuthedClient(raw, t1)
            me = one.get(f"/api/v1/users/{u1}")
            assert me.status_code == 200 and me.json()["username"] == "one" and "scopes" in me.json()
            fellow = one.get(f"/api/v1/users/{u2}")
            assert fellow.status_code == 200
            assert fellow.json()["username"] == "two"
            assert "scopes" not in fellow.json()
            assert [g["id"] for g in fellow.json()["groups"]] == ["g"]
            assert one.get(f"/api/v1/users/{u3}").status_code == 404
            assert client.get(f"/api/v1/users/{u3}").status_code == 200

    def test_device_read_one_is_gated_like_the_list(self, client, tmp_db):
        self._world(client)
        app = _get_app()
        with TestClient(app) as raw:
            assert raw.get("/api/v1/devices").status_code == 401
            assert raw.get("/api/v1/devices/dev-in").status_code == 401
            token, _ = _invite_and_activate(client, raw, "seat", ["group:g"])
            seat = AuthedClient(raw, token)
            assert seat.get("/api/v1/devices/dev-in").status_code == 200
            assert seat.get("/api/v1/devices/dev-out").status_code == 404
            assert seat.get("/api/v1/devices/dev-in/guide").status_code == 200
            assert seat.get("/api/v1/devices/dev-out/guide").status_code == 404
            assert seat.get("/api/v1/devices/dev-out/helpers").status_code == 404

    def test_service_and_route_reads_are_gated_like_their_lists(self, client, tmp_db):
        self._world(client)
        app = _get_app()
        with TestClient(app) as raw:
            assert raw.get("/api/v1/services").status_code == 401
            assert raw.get("/api/v1/services/dev-in/svc-in").status_code == 401
            assert raw.get("/api/v1/routes").status_code == 401
            token, _ = _invite_and_activate(client, raw, "seat", ["group:g"])
            seat = AuthedClient(raw, token)
            assert seat.get("/api/v1/services/dev-in/svc-in").status_code == 200
            assert seat.get("/api/v1/services/dev-out/svc-out").status_code == 404
            assert seat.get("/api/v1/services/dev-in/svc-in/guide").status_code == 200
            assert seat.get("/api/v1/services/dev-out/svc-out/guide").status_code == 404

    def test_the_open_writes_take_a_bearer(self, client, tmp_db):
        self._world(client)
        app = _get_app()
        with TestClient(app) as raw:
            assert raw.post("/api/v1/routes", json={"subdomain": "x", "target": "localhost:1"}).status_code == 401
            assert raw.post("/api/v1/ports/allocate", json={"port_type": "tunnel", "owner_id": "dev-in"}).status_code == 401
            assert raw.post("/api/v1/enroll/dev-in/retoken").status_code == 401
            assert raw.post("/api/v1/gateway/sync").status_code == 401
            assert raw.post("/api/v1/probe").status_code == 401
            assert raw.get("/api/v1/enroll/pending").status_code == 401
            assert raw.get("/api/v1/enroll/services/pending").status_code == 401
            assert raw.get("/api/v1/ports").status_code == 401
            assert raw.get("/api/v1/ports/check/10001").status_code == 401
            assert raw.get("/api/v1/gateway/status").status_code == 401
            assert raw.get("/api/v1/auth/can-access?username=admin&device_id=dev-in").status_code == 401

    def test_device_writes_follow_the_device_manager(self, client, tmp_db):
        self._world(client)
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "seat", ["group:g"])
            seat = AuthedClient(raw, token)
            # Readers use services; managing credentials/devices needs editor.
            assert seat.post("/api/v1/enroll/dev-in/retoken").status_code == 403
            assert client.patch(f"/api/v1/groups/g/members/{uid}", json={"role": "editor"}).status_code == 200
            # a fresh enrollment token: the device's manager, and a hidden device does not exist
            assert seat.post("/api/v1/enroll/dev-in/retoken").status_code == 200
            assert seat.post("/api/v1/enroll/dev-out/retoken").status_code == 404
            # a port for a device you manage; a hidden device does not exist
            with patch.object(port_alloc, "_port_in_use", return_value=False):
                assert seat.post("/api/v1/ports/allocate", json={"port_type": "tunnel", "owner_id": "dev-in"}).status_code == 200
                assert seat.post("/api/v1/ports/allocate", json={"port_type": "tunnel", "owner_id": "dev-out"}).status_code == 404
            # a route: for a service you manage; a hidden service does not exist; no service = admin's
            assert seat.post("/api/v1/routes", json={"subdomain": "in", "target": "localhost:8080", "service_id": "dev-in/svc-in"}).status_code == 200
            assert seat.post("/api/v1/routes", json={"subdomain": "out", "target": "localhost:8081", "service_id": "dev-out/svc-out"}).status_code == 404
            assert seat.post("/api/v1/routes", json={"subdomain": "bare", "target": "localhost:1"}).status_code == 403
            # the fleet verbs stay the admin's
            assert seat.post("/api/v1/gateway/sync").status_code == 403
            assert seat.post("/api/v1/probe").status_code == 403
            assert seat.get("/api/v1/enroll/pending").status_code == 403
            assert seat.get("/api/v1/enroll/services/pending").status_code == 403

    def test_the_index_does_not_call_a_scoped_resource_write_open(self, client, tmp_db):
        """The index is what an agent reads first. It said `"auth": "any"` for
        leases.request, resources.stats and resources.update, while all three call
        _require_visible_resource and answer 404 outside scope (the test below).
        So gpu-1's gateway ran on a token that sees no device, every lease came back
        404, `_request_lease` swallowed it, and it looked like a working gateway
        (#861). The label has to say what the code does."""
        idx = client.get("/api/v1/").json()["endpoints"]
        for group, verb in (("leases", "request"), ("resources", "stats"), ("resources", "update")):
            entry = idx[group][verb]
            assert entry.get("auth") == "scoped", (group, verb, entry.get("auth"))
            assert "404" in entry.get("description", ""), (group, verb)
        assert "service token" in idx["leases"]["request"]["description"]
        renew = idx["leases"]["renew"]["description"]
        assert "already_later" not in renew and "ttl_below_granted" in renew and "max_total_s" in renew

    def test_resource_writes_follow_the_device(self, client, tmp_db):
        self._world(client)
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "seat", ["group:g"])
            other, _ = _invite_and_activate(client, raw, "other", ["group:g"])
            seat, peer = AuthedClient(raw, token), AuthedClient(raw, other)
            assert seat.patch("/api/v1/resources/dev-in/gpu-0", json={"status": "online"}).status_code == 403
            assert client.patch(f"/api/v1/groups/g/members/{uid}", json={"role": "editor"}).status_code == 200
            assert seat.patch("/api/v1/resources/dev-out/gpu-0", json={"status": "offline"}).status_code == 404
            assert seat.post("/api/v1/resources/dev-out/gpu-0/stats", json={"compute_pct": 1}).status_code == 404
            assert seat.post("/api/v1/resources/dev-out/gpu-0/leases", json={"memory_mb": 1}).status_code == 404
            assert seat.patch("/api/v1/resources/dev-in/gpu-0", json={"status": "online"}).status_code == 200
            lease = seat.post("/api/v1/resources/dev-in/gpu-0/leases", json={"memory_mb": 1024})
            assert lease.status_code == 200, lease.text
            lid = lease.json()["lease_id"]
            # a lease is released by its holder or the admin
            assert peer.delete(f"/api/v1/leases/{lid}").status_code == 403
            assert seat.delete(f"/api/v1/leases/{lid}").status_code == 200

    def test_the_gateway_lists_and_admits_by_membership(self, client, tmp_db):
        """`can-access` and `accessible-devices` are the gateway's (admin bearer) and
        read the same store the API reads: a member added through the group, with no
        `group:` in the token's scope string, is admitted and listed."""
        self._world(client)
        _create_group(client, "empty", "Empty")
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "seat", ["group:empty"])
            seat = AuthedClient(raw, token)
            assert seat.get("/api/v1/auth/can-access?username=seat&device_id=dev-in").status_code == 403
            assert seat.get("/api/v1/auth/accessible-devices?username=seat").status_code == 403
            before = client.get("/api/v1/auth/can-access?username=seat&device_id=dev-in").json()
            assert before["access"] is False
            assert client.get("/api/v1/auth/accessible-devices?username=seat").json()["devices"] == []
            assert client.post("/api/v1/groups/g/members", json={"user_id": uid, "role": "viewer"}).status_code == 200
            after = client.get("/api/v1/auth/can-access?username=seat&device_id=dev-in").json()
            assert after["access"] is True and "g" in after["reason"]
            listed = client.get("/api/v1/auth/accessible-devices?username=seat").json()["devices"]
            assert [d["id"] for d in listed] == ["dev-in"]
            assert client.get("/api/v1/auth/can-access?username=seat&device_id=dev-out").json()["access"] is False
            assert client.get("/api/v1/auth/can-access?username=nobody&device_id=dev-in").json()["access"] is False

    def test_wake_health_shows_a_seat_its_own_row(self, client, tmp_db):
        _create_group(client, "g", "G")
        app = _get_app()
        with TestClient(app) as raw:
            t1, _ = _invite_and_activate(client, raw, "one", ["group:g"])
            t2, _ = _invite_and_activate(client, raw, "two", ["group:g"])
            one, two = AuthedClient(raw, t1), AuthedClient(raw, t2)
            for c in (one, two):
                assert c.post("/api/v1/wake/register", json={"wake_cmd": "x", "host": "h"}).status_code == 200
            mine = one.get("/api/v1/wake/health")
            assert mine.status_code == 200
            assert {s["identity"] for s in mine.json()["seats"]} <= {"one"}
            fleet = client.get("/api/v1/wake/health").json()["seats"]
            assert {"one", "two"} <= {s["identity"] for s in fleet}


# ===========================================================================
# Scope v1, train 1 — one predicate on the work plane (doc 261)
# ===========================================================================

class TestScopeOnePredicate:
    """The group is the scope. One module (`scope.py`) answers every read,
    write and delivery question for tickets, docs, projects, tasks and files:
    a member reads; an editor creates and changes; an owner manages; the
    author always sees their own; an object outside your groups is hidden
    (404); an assignment to an outsider is refused with the note; a mention
    of one is inert with the note; every fan-out row follows the object; the
    thread is not scoped."""

    def _world(self, client, raw):
        """Groups E (seat edits), V (seat views), N (seat is not in); a peer
        editor in E; a stranger in N only."""
        for g in ("E", "V", "N"):
            _create_group(client, g, g)
        t_seat, uid = _invite_and_activate(client, raw, "seat", ["group:E"])
        client.patch(f"/api/v1/groups/E/members/{uid}", json={"role": "editor"})
        client.post("/api/v1/groups/V/members", json={"user_id": uid, "role": "viewer"})
        t_peer, pid = _invite_and_activate(client, raw, "peer", ["group:E"])
        client.patch(f"/api/v1/groups/E/members/{pid}", json={"role": "editor"})
        t_str, sid = _invite_and_activate(client, raw, "stranger", ["group:N"])
        client.patch(f"/api/v1/groups/N/members/{sid}", json={"role": "editor"})
        return AuthedClient(raw, t_seat), AuthedClient(raw, t_peer), AuthedClient(raw, t_str)

    def test_a_create_names_its_group_or_takes_the_writers_first_editable(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            seat, peer, stranger = self._world(client, raw)
            t = seat.post("/api/v1/tickets", json={"summary": "unnamed"}).json()
            assert t["group_scope"] == "E"
            d = seat.post("/api/v1/docs", json={"title": "unnamed", "body": "b"}).json()
            assert d["group_scope"] == "E"
            p = seat.post("/api/v1/projects", json={"name": "unnamed"}).json()
            assert p["group_scope"] == "E"
            # a viewer's group: named → refused as a viewer; a group you are not in → refused
            r = seat.post("/api/v1/tickets", json={"summary": "in V", "group_scope": "V"})
            assert r.status_code == 403 and "viewer" in r.json()["detail"]
            r = seat.post("/api/v1/tickets", json={"summary": "in N", "group_scope": "N"})
            assert r.status_code == 403 and "not in group" in r.json()["detail"]
            # the sentinel that once cleared a doc to the commons is refused
            assert seat.patch(f"/api/v1/docs/{d['id']}", json={"group_scope": ""}).status_code == 422
            # the admin's unnamed create goes to the contributors group; with an assignee, to the assignee's group
            assert client.post("/api/v1/tickets", json={"summary": "admin"}).json()["group_scope"] == "samcloud-contributors"
            assert client.post("/api/v1/tickets", json={"summary": "for seat", "assignee": "seat"}).json()["group_scope"] == "E"

    def test_an_object_outside_your_groups_is_hidden_on_every_kind(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            seat, peer, stranger = self._world(client, raw)
            t = stranger.post("/api/v1/tickets", json={"summary": "N ticket"}).json()
            d = stranger.post("/api/v1/docs", json={"title": "N doc", "body": "b"}).json()
            p = stranger.post("/api/v1/projects", json={"name": "N proj"}).json()
            k = stranger.post(f"/api/v1/projects/{p['slug']}/tasks", json={"title": "N task"}).json()
            assert seat.get(f"/api/v1/tickets/{t['id']}").status_code == 404
            assert seat.get(f"/api/v1/docs/{d['id']}").status_code == 404
            assert seat.get(f"/api/v1/projects/{p['slug']}").status_code == 404
            assert seat.get(f"/api/v1/tasks/{k['id']}").status_code == 404
            assert seat.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "hi"}).status_code == 404
            assert t["id"] not in {x["id"] for x in seat.get("/api/v1/tickets").json()}
            _docs = seat.get("/api/v1/docs").json()
            assert d["id"] not in {x["id"] for x in (_docs["docs"] if isinstance(_docs, dict) else _docs)}
            assert k["id"] not in {x["id"] for x in seat.get("/api/v1/tasks").json()["tasks"]}
            # the thread view hides with the object
            assert seat.get(f"/api/v1/threads/of/ticket/{t['id']}").status_code == 404
            # the author always sees their own
            assert stranger.get(f"/api/v1/tickets/{t['id']}").status_code == 200

    def test_an_outsider_assignee_is_refused_with_the_note(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            seat, peer, stranger = self._world(client, raw)
            r = seat.post("/api/v1/tickets", json={"summary": "x", "assignee": "stranger"})
            assert r.status_code == 422
            assert "stranger is not in group 'E'" in r.json()["detail"]
            assert "POST /api/v1/groups/E/members" in r.json()["detail"]
            t = seat.post("/api/v1/tickets", json={"summary": "x", "assignee": "peer"}).json()
            assert seat.patch(f"/api/v1/tickets/{t['id']}", json={"assignee": "stranger"}).status_code == 422
            p = seat.post("/api/v1/projects", json={"name": "P"}).json()
            assert seat.post(f"/api/v1/projects/{p['slug']}/tasks", json={"title": "t", "assignee": "stranger"}).status_code == 422
            k = seat.post(f"/api/v1/projects/{p['slug']}/tasks", json={"title": "t"}).json()
            assert seat.patch(f"/api/v1/tasks/{k['id']}", json={"assignee": "stranger"}).status_code == 422
            assert seat.post(f"/api/v1/tasks/{k['id']}/delegate", json={"assignee": "stranger"}).status_code == 422
            # a delegation inside the group makes a ticket in the project's group
            dl = seat.post(f"/api/v1/tasks/{k['id']}/delegate", json={"assignee": "peer"}).json()
            assert client.get(f"/api/v1/tickets/{dl['ticket_id']}").json()["group_scope"] == "E"

    def test_an_outsider_mention_is_inert_and_noted(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            seat, peer, stranger = self._world(client, raw)
            def rows(who, **key):
                inbox = who.get("/api/v1/notifications").json()["notifications"]
                return [n for n in inbox if all(n.get(k) == v for k, v in key.items())]
            t = seat.post("/api/v1/tickets", json={"summary": "x", "body": "hello @stranger and @peer"}).json()
            assert any("stranger is not in group 'E'" in n for n in t["notes"])
            assert rows(stranger, ticket_id=t["id"]) == []
            assert any(n["event_type"] == "ticket.mentioned" for n in rows(peer, ticket_id=t["id"]))
            c = seat.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "again @stranger"}).json()
            assert c["notes"] and rows(stranger, ticket_id=t["id"]) == []
            d = seat.post("/api/v1/docs", json={"title": "d", "body": "b"}).json()
            dc = seat.post(f"/api/v1/docs/{d['id']}/comments", json={"body": "@stranger @peer"}).json()
            assert dc["notes"] and rows(stranger, doc_id=d["id"]) == []
            assert any(n["event_type"] == "doc.mentioned" for n in rows(peer, doc_id=d["id"]))
            p = seat.post("/api/v1/projects", json={"name": "P"}).json()
            k = seat.post(f"/api/v1/projects/{p['slug']}/tasks", json={"title": "t"}).json()
            tc = seat.post(f"/api/v1/tasks/{k['id']}/comments", json={"body": "@stranger @peer"}).json()
            assert tc["notes"] and rows(stranger, task_id=k["id"]) == []
            assert any(n["event_type"] == "task.mentioned" for n in rows(peer, task_id=k["id"]))

    def test_a_viewer_reads_and_answers_but_does_not_change(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            seat, peer, stranger = self._world(client, raw)
            # an editor of V makes the objects (the admin, here)
            t = client.post("/api/v1/tickets", json={"summary": "V ticket", "group_scope": "V"}).json()
            d = client.post("/api/v1/docs", json={"title": "V doc", "body": "b", "group_scope": "V"}).json()
            p = client.post("/api/v1/projects", json={"name": "V proj", "group_scope": "V"}).json()
            k = client.post(f"/api/v1/projects/{p['slug']}/tasks", json={"title": "V task"}).json()
            assert seat.get(f"/api/v1/tickets/{t['id']}").status_code == 200
            assert seat.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "an answer"}).status_code == 200
            assert seat.post(f"/api/v1/docs/{d['id']}/comments", json={"body": "an answer"}).status_code == 200
            assert seat.post(f"/api/v1/tasks/{k['id']}/comments", json={"body": "an answer"}).status_code == 200
            r = seat.patch(f"/api/v1/tickets/{t['id']}", json={"status": "resolved"})
            assert r.status_code == 403 and "viewer" in r.json()["detail"]
            assert seat.post(f"/api/v1/docs/{d['id']}/revisions", json={"body": "rewrite"}).status_code == 403
            assert seat.post(f"/api/v1/docs/{d['id']}/append", json={"block": "more"}).status_code == 403
            assert seat.post(f"/api/v1/projects/{p['slug']}/tasks", json={"title": "new"}).status_code == 403
            assert seat.patch(f"/api/v1/tasks/{k['id']}", json={"title": "renamed"}).status_code == 403
            # the assignee drives status whatever their role
            client.patch(f"/api/v1/tickets/{t['id']}", json={"assignee": "seat"})
            assert seat.patch(f"/api/v1/tickets/{t['id']}", json={"status": "resolved"}).status_code == 200
            client.patch(f"/api/v1/tasks/{k['id']}", json={"assignee": "seat"})
            assert seat.patch(f"/api/v1/tasks/{k['id']}", json={"status": "done"}).status_code == 200

    def test_an_editor_changes_a_peers_work_but_not_its_metadata(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            seat, peer, stranger = self._world(client, raw)
            t = peer.post("/api/v1/tickets", json={"summary": "peer's"}).json()
            assert seat.patch(f"/api/v1/tickets/{t['id']}", json={"priority": "high"}).status_code == 200
            d = peer.post("/api/v1/docs", json={"title": "peer's doc", "body": "b"}).json()
            assert seat.post(f"/api/v1/docs/{d['id']}/revisions", json={"body": "edited by an editor"}).status_code == 200
            assert seat.patch(f"/api/v1/docs/{d['id']}", json={"status": "archived"}).status_code == 403
            assert client.patch(f"/api/v1/docs/{d['id']}", json={"status": "archived"}).status_code == 200

    def test_thread_fan_out_follows_the_object_not_the_thread(self, client, tmp_db):
        """The thread is not scoped (doc 261): the admin's thread holds a
        ticket in E and a doc in N; a holder bound to it who reads only E hears
        the ticket and never the doc."""
        app = _get_app()
        with TestClient(app) as raw:
            seat, peer, stranger = self._world(client, raw)
            t = client.post("/api/v1/tickets", json={"summary": "root", "group_scope": "E", "assignee": "seat"}).json()
            th = t["thread_id"]
            r = seat.post("/api/v1/wake/threads", json={"kind": "ticket", "object_id": t["id"],
                                                        "conversation_id": "seat-holder", "holder": "stream", "pid": 4242})
            assert r.status_code == 200, r.text
            d = client.post("/api/v1/docs", json={"title": "N doc in the same thread", "body": "b", "group_scope": "N"},
                            headers={"X-Thread-Id": str(th)}).json()
            assert client.get(f"/api/v1/threads/{th}").json()["members"]["docs"][0]["id"] == d["id"]
            client.post(f"/api/v1/docs/{d['id']}/comments", json={"body": "a comment on the N doc"})
            client.post(f"/api/v1/tickets/{t['id']}/comments", json={"body": "a comment on the E ticket"})
            inbox = seat.get("/api/v1/notifications").json()["notifications"]
            assert not any(n.get("doc_id") == d["id"] for n in inbox)
            assert any(n.get("ticket_id") == t["id"] and n["event_type"] == "ticket.commented" for n in inbox)
            assert seat.get(f"/api/v1/docs/{d['id']}").status_code == 404

    def test_doc_fan_out_reads_membership_not_the_token(self, client, tmp_db):
        """A member added through the group (no `group:` in the token's scope
        string) hears the group's docs — the pre-v1 mirror read the scope
        string alone (doc 262 §3)."""
        _create_group(client, "late", "Late")
        _create_group(client, "elsewhere", "Elsewhere")
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "latecomer", ["group:elsewhere"])
            late = AuthedClient(raw, token)
            client.post("/api/v1/groups/late/members", json={"user_id": uid, "role": "viewer"})
            d = client.post("/api/v1/docs", json={"title": "for late", "body": "hello @latecomer", "group_scope": "late"}).json()
            inbox = late.get("/api/v1/notifications").json()["notifications"]
            kinds = {n["event_type"] for n in inbox if n.get("doc_id") == d["id"]}
            assert "doc.mentioned" in kinds or "doc.published" in kinds
            assert late.get(f"/api/v1/docs/{d['id']}").status_code == 200

    def test_an_editor_annexes_only_what_it_manages(self, client, tmp_db):
        _register_device(client, "theirs", "h1")
        _register_device(client, "mine", "h2")
        _create_group(client, "mine-g", "Mine")
        _create_group(client, "target", "Target")
        client.post("/api/v1/groups/mine-g/devices", json={"device_id": "mine"})
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "annexer", ["group:mine-g"])
            client.patch(f"/api/v1/groups/mine-g/members/{uid}", json={"role": "editor"})
            client.post("/api/v1/groups/target/members", json={"user_id": uid, "role": "editor"})
            me = AuthedClient(raw, token)
            assert me.post("/api/v1/groups/target/devices", json={"device_id": "theirs"}).status_code == 404
            assert me.post("/api/v1/groups/target/devices", json={"device_id": "mine"}).status_code == 200

    def test_roles_are_viewer_editor_owner_and_admin_reads_as_owner(self, client, tmp_db):
        _create_group(client, "g", "G")
        app = _get_app()
        with TestClient(app) as raw:
            _, uid = _invite_and_activate(client, raw, "member", ["group:g"])
            _, uid2 = _invite_and_activate(client, raw, "member2", [])
            assert client.post("/api/v1/groups/g/members", json={"user_id": uid2, "role": "boss"}).status_code == 422
            assert client.post("/api/v1/groups/g/members", json={"user_id": uid2, "role": "admin"}).status_code == 200
            r = client.patch(f"/api/v1/groups/g/members/{uid}", json={"role": "admin"})
            assert r.status_code == 200
            roles = {m["username"]: m["group_role"] for m in client.get("/api/v1/groups/g").json()["members"]}
            assert roles["member"] == "owner" and roles["member2"] == "owner"

    def test_the_backfill_gives_every_unscoped_row_its_creators_group(self, client, tmp_db):
        """v20: rows written before scope v1 get the creator's first group, a
        machine's the group holding its device, and the rest `core`."""
        _create_group(client, "theirs", "Theirs")
        app = _get_app()
        with TestClient(app) as raw:
            _invite_and_activate(client, raw, "old-hand", ["group:theirs"])
        _register_device(client, "box", "box")
        _create_group(client, "boxes", "Boxes")
        client.post("/api/v1/groups/boxes/devices", json={"device_id": "box"})
        import db as _db
        t1 = _db.execute("INSERT INTO tickets (source, summary, status) VALUES ('old-hand', 'legacy', 'open')")
        t2 = _db.execute("INSERT INTO tickets (source, summary, status) VALUES ('box/svc', 'machine', 'open')")
        t3 = _db.execute("INSERT INTO tickets (source, summary, status) VALUES ('nobody', 'orphan', 'open')")
        t4 = _db.execute("INSERT INTO tickets (source, summary, status) VALUES ('admin', 'the admin''s own', 'open')")
        t5 = _db.execute("INSERT INTO tickets (source, summary, status, assignee) VALUES ('admin', 'filed to a seat', 'open', 'old-hand')")
        d1 = _db.execute("INSERT INTO docs (slug, title, source, current_rev) VALUES ('legacy-doc', 'legacy', 'old-hand', 1)")
        assert client.get("/api/v1/admin/scope/unscoped").json()["count"] == 6
        conn = _db.get_connection()
        _db._migrate(conn)
        conn.close()
        assert client.get("/api/v1/admin/scope/unscoped").json()["count"] == 0
        assert client.get(f"/api/v1/tickets/{t1}").json()["group_scope"] == "theirs"
        assert client.get(f"/api/v1/tickets/{t2}").json()["group_scope"] == "boxes"
        assert client.get(f"/api/v1/tickets/{t3}").json()["group_scope"] == "core"                    # an orphan: the inner city, seen by few
        assert client.get(f"/api/v1/tickets/{t4}").json()["group_scope"] == "samcloud-contributors"   # the admin's own: the plane's work
        assert client.get(f"/api/v1/tickets/{t5}").json()["group_scope"] == "theirs"                  # admin-filed to a seat: the seat's group
        assert client.get(f"/api/v1/docs/{d1}").json()["group_scope"] == "theirs"


# ===========================================================================
# Scope v1, train 2 — groups anyone can make, the guide, the release (doc 261)
# ===========================================================================

class TestScopeGroupsAndRecord:

    def test_any_user_makes_a_group_and_owns_it(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "maker", [])
            me = AuthedClient(raw, token)
            r = me.post("/api/v1/groups", json={"id": "my-lab", "name": "My lab", "description": "where I keep my things"})
            assert r.status_code == 200, r.text
            assert r.json()["owner"] == "maker"
            g = me.get("/api/v1/groups/my-lab").json()
            assert g["your_role"] == "owner" and g["created_by"] == "maker"
            assert {m["username"]: m["group_role"] for m in g["members"]} == {"maker": "owner"}
            # an owner adds people and sets the working doc
            t2, uid2 = _invite_and_activate(client, raw, "friend", [])
            assert me.post("/api/v1/groups/my-lab/members", json={"user_id": uid2, "role": "editor"}).status_code == 200
            d = me.post("/api/v1/docs", json={"title": "lab notes", "body": "b", "group_scope": "my-lab"}).json()
            assert me.patch("/api/v1/groups/my-lab", json={"doc_id": d["id"]}).status_code == 200
            g = AuthedClient(raw, t2).get("/api/v1/groups/my-lab").json()
            assert g["working_doc"]["id"] == d["id"] and g["your_role"] == "editor"
            # the plane's two groups exist; a bad id is refused; a stranger is told the group does not exist
            assert {x["id"] for x in client.get("/api/v1/groups").json()} >= {"core", "samcloud-contributors", "my-lab"}
            assert me.post("/api/v1/groups", json={"id": "Bad Id!", "name": "x"}).status_code == 422
            t3, _ = _invite_and_activate(client, raw, "stranger", [])
            assert AuthedClient(raw, t3).get("/api/v1/groups/my-lab").status_code == 404

    def test_the_guide_tells_a_seat_its_groups_roles_and_default(self, client, tmp_db):
        _create_group(client, "g", "G")
        client.patch("/api/v1/groups/g", json={"description": "the G work"})
        _register_device(client, "dev-g", "host-g")
        client.post("/api/v1/groups/g/devices", json={"device_id": "dev-g"})
        _register_device(client, "dev-x", "host-x")
        with patch.object(port_alloc, "_port_in_use", return_value=False):
            _register_service(client, "svc-g", "dev-g", 8080)
            _register_service(client, "svc-x", "dev-x", 8081)
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "seat", ["group:g"])
            client.patch(f"/api/v1/groups/g/members/{uid}", json={"role": "editor"})
            _invite_and_activate(client, raw, "mate", ["group:g"])
            guide = AuthedClient(raw, token).get("/api/v1/users/me/guide").text
            assert "**g** — you are a **editor**" in guide and "the G work" in guide
            assert "mate (viewer)" in guide
            assert "goes to **g**" in guide
            assert "dev-g/svc-g" in guide and "dev-x/svc-x" not in guide

    def test_the_index_names_the_release_and_tickets_filter_by_source(self, client, tmp_db):
        idx = client.get("/api/v1/").json()
        assert idx["version"] not in ("0.1.0", "unknown") and idx["schema"] == "v20"
        assert "rail" in idx["helpers"] and "scope-check" in idx["helpers"] and idx["skill"] != "0"
        assert client.get("/").json()["version"] == idx["version"]
        app = _get_app()
        with TestClient(app) as raw:
            token, _ = _invite_and_activate(client, raw, "filer", [])
            AuthedClient(raw, token).post("/api/v1/tickets", json={"summary": "mine"})
            client.post("/api/v1/tickets", json={"summary": "the admin's"})
            mine = client.get("/api/v1/tickets?source=filer").json()
            assert [t["summary"] for t in mine] == ["mine"]

    def test_scope_check_is_served_with_a_stamp(self, client):
        r = client.get("/api/v1/helpers/scope-check")
        assert r.status_code == 200 and "SCOPE_CHECK_VERSION" in r.text and "__API__" not in r.text

    def test_verify_names_your_groups_and_default(self, client, tmp_db):
        """F1 of the per-seat stage (four seats): "first group" was undefined."""
        _create_group(client, "a", "A")
        _create_group(client, "b", "B")
        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(client, raw, "seat", ["group:b"])
            client.patch(f"/api/v1/groups/b/members/{uid}", json={"role": "editor"})
            client.post("/api/v1/groups/a/members", json={"user_id": uid, "role": "editor"})
            v = AuthedClient(raw, token).get("/api/v1/auth/verify").json()
            assert v["groups"] == {"b": "editor", "a": "editor"}
            assert v["default_group"] == "b"           # the earliest membership you can edit
            assert AuthedClient(raw, token).post("/api/v1/tickets", json={"summary": "x"}).json()["group_scope"] == "b"
            assert client.get("/api/v1/auth/verify").json()["groups"] == "*"

    def test_the_ticket_group_filter_is_exact(self, client, tmp_db):
        """Doc 264 (codex): `?group_scope=work` answered with tickets of other
        groups through the old device/service anchor union."""
        _register_device(client, "dev-g", "host-g")
        _create_group(client, "g", "G")
        _create_group(client, "h", "H")
        client.post("/api/v1/groups/g/devices", json={"device_id": "dev-g"})
        client.post("/api/v1/tickets", json={"summary": "in g", "group_scope": "g"})
        client.post("/api/v1/tickets", json={"summary": "in h, on g's device", "group_scope": "h", "device_id": "dev-g"})
        got = client.get("/api/v1/tickets?group_scope=g").json()
        assert [t["summary"] for t in got] == ["in g"]


# ===========================================================================
# Device host keys — the pin the gateway checks a hop against (#818)
# ===========================================================================

def _ed25519_hostkey(seed: bytes = b"\x01" * 32) -> str:
    """A syntactically real `ssh-ed25519 <base64>` host key.

    Built in SSH wire format (uint32 length + bytes, twice) so the validator is
    exercised for real rather than against a hand-written blob.
    """
    import base64 as _b64

    def _string(b: bytes) -> bytes:
        return len(b).to_bytes(4, "big") + b

    blob = _string(b"ssh-ed25519") + _string(seed)
    return "ssh-ed25519 " + _b64.b64encode(blob).decode()


def _agent_token(agent_id: str, scopes):
    """A non-admin agent bearer, created the way the plane creates one."""
    import enrollment
    return enrollment.generate_agent_token(agent_id, scopes=scopes)


def _device_token(device_id: str):
    """That device's own bearer — what `/devices/activate` hands back."""
    import enrollment
    return enrollment.generate_device_token(device_id)


class TestDeviceHostKey:
    KEY_A = _ed25519_hostkey(b"\xaa" * 32)
    KEY_B = _ed25519_hostkey(b"\xbb" * 32)

    def test_pin_then_read_back(self, client):
        _register_device(client, "hk-dev", "hk-host", ip="192.0.2.99")
        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_A})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["recorded"] is True
        assert body["host_pubkey"] == self.KEY_A
        assert body["fingerprint"].startswith("SHA256:")

        dev = client.get("/api/v1/devices/hk-dev").json()
        assert dev["host_pubkey"] == self.KEY_A
        assert dev["host_pubkey_recorded_at"]

    def test_pinning_the_same_key_again_is_idempotent(self, client):
        _register_device(client, "hk-dev", "hk-host")
        client.post("/api/v1/devices/hk-dev/host-key", json={"host_pubkey": self.KEY_A})
        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_A})
        assert r.status_code == 200, r.text
        assert r.json()["recorded"] is False

    def test_a_different_key_is_refused_with_409_and_the_pin_stands(self, client):
        """Set-if-absent: the gateway's learn path cannot overwrite a pin."""
        _register_device(client, "hk-dev", "hk-host")
        client.post("/api/v1/devices/hk-dev/host-key", json={"host_pubkey": self.KEY_A})

        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_B})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "pinned_fingerprint" in detail and "offered_fingerprint" in detail

        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] == self.KEY_A

    def test_delete_then_pin_accepts_a_rekey(self, client):
        _register_device(client, "hk-dev", "hk-host")
        client.post("/api/v1/devices/hk-dev/host-key", json={"host_pubkey": self.KEY_A})

        d = client.delete("/api/v1/devices/hk-dev/host-key")
        assert d.status_code == 200, d.text
        assert d.json()["cleared"] is True
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] is None

        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_B})
        assert r.status_code == 200, r.text
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] == self.KEY_B

    def test_clearing_an_unpinned_device_is_a_no_op(self, client):
        _register_device(client, "hk-dev", "hk-host")
        r = client.delete("/api/v1/devices/hk-dev/host-key")
        assert r.status_code == 200, r.text
        assert r.json()["cleared"] is False

    # --- who may write a pin -------------------------------------------------

    def test_an_agent_that_can_reach_the_device_still_cannot_pin_it(self, client):
        """The pre-pinning attack: a `device:` scope is enough to USE a device,
        and must not be enough to pin a key on one nobody has pinned yet —
        otherwise the gateway trusts the first caller and fail-closes against
        the real host forever."""
        _register_device(client, "hk-dev", "hk-host")
        token = _agent_token("agent:opportunist", ["device:hk-dev"])
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_B}, headers=headers)
        assert r.status_code == 403, r.text
        # And nothing was written.
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] is None

    def test_an_agent_cannot_clear_a_pin_either(self, client):
        _register_device(client, "hk-dev", "hk-host")
        client.post("/api/v1/devices/hk-dev/host-key", json={"host_pubkey": self.KEY_A})
        token = _agent_token("agent:opportunist", ["device:hk-dev"])

        r = client.delete("/api/v1/devices/hk-dev/host-key",
                          headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, r.text
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] == self.KEY_A

    def test_a_device_outside_scope_stays_hidden(self, client):
        """404, not 403 — an out-of-scope device does not exist from outside."""
        _register_device(client, "hk-dev", "hk-host")
        token = _agent_token("agent:stranger", ["device:something-else"])

        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_B},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404, r.text
        # Assert on the message, not just the code: a missing route is also a 404,
        # so the bare status would pass with no endpoint at all.
        assert "hk-dev" in str(r.json()["detail"])

    def test_the_device_pins_its_own_key(self, client):
        """The no-person-in-the-loop re-key path."""
        _register_device(client, "hk-dev", "hk-host")
        headers = {"Authorization": f"Bearer {_device_token('hk-dev')}"}

        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_A}, headers=headers)
        assert r.status_code == 200, r.text

        d = client.delete("/api/v1/devices/hk-dev/host-key", headers=headers)
        assert d.status_code == 200, d.text
        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_B}, headers=headers)
        assert r.status_code == 200, r.text
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] == self.KEY_B

    def test_a_device_token_cannot_pin_another_device(self, client):
        _register_device(client, "hk-dev", "hk-host")
        _register_device(client, "hk-other", "other-host")
        headers = {"Authorization": f"Bearer {_device_token('hk-other')}"}

        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_B}, headers=headers)
        assert r.status_code in (403, 404), r.text
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] is None

    # --- validation: this value is written into a known_hosts file -----------

    def test_a_second_line_is_refused(self, client):
        """A newline would pin an extra host pattern in the gateway's
        known_hosts file."""
        _register_device(client, "hk-dev", "hk-host")
        evil = self.KEY_A + "\n* " + self.KEY_B.split()[0] + " " + self.KEY_B.split()[1]
        r = client.post("/api/v1/devices/hk-dev/host-key", json={"host_pubkey": evil})
        assert r.status_code == 400, r.text
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] is None

    def test_a_mismatched_type_is_refused(self, client):
        _register_device(client, "hk-dev", "hk-host")
        body = self.KEY_A.split()[1]
        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": f"ssh-rsa {body}"})
        assert r.status_code == 400, r.text

    def test_an_unknown_type_is_refused(self, client):
        _register_device(client, "hk-dev", "hk-host")
        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": "ssh-magic AAAA"})
        assert r.status_code == 400, r.text

    def test_a_non_base64_body_is_refused(self, client):
        _register_device(client, "hk-dev", "hk-host")
        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": "ssh-ed25519 not!base64!"})
        assert r.status_code == 400, r.text

    def test_a_trailing_comment_is_accepted_and_dropped(self, client):
        """ssh-keygen writes a comment; store the two fields that matter."""
        _register_device(client, "hk-dev", "hk-host")
        r = client.post("/api/v1/devices/hk-dev/host-key",
                        json={"host_pubkey": self.KEY_A + " root@hk-host"})
        assert r.status_code == 200, r.text
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] == self.KEY_A

    def test_pinning_an_unknown_device_is_404(self, client):
        r = client.post("/api/v1/devices/nope/host-key",
                        json={"host_pubkey": self.KEY_A})
        assert r.status_code == 404, r.text
        # Names the device, so this cannot pass against a missing route.
        assert "nope" in str(r.json()["detail"])

    # --- the pin is decided by the write, not by a prior read ----------------

    def test_a_stale_read_of_an_empty_pin_still_cannot_overwrite(self, client):
        """The check is the UPDATE's WHERE clause, not the read before it.

        Two first hops to one device can both read an empty pin before either
        writes (the handler is a sync def, so it runs in the threadpool). Here the
        pre-read is stubbed empty while key A is really pinned — the write must
        still refuse, or the second hop silently wins and the 409 is decoration.
        """
        import main

        _register_device(client, "hk-dev", "hk-host")
        client.post("/api/v1/devices/hk-dev/host-key", json={"host_pubkey": self.KEY_A})

        real = main._require_host_key_writer

        def stale_read(auth, device_id):
            device = dict(real(auth, device_id))
            device["host_pubkey"] = None          # what a racing caller saw
            device["host_pubkey_recorded_at"] = None
            return device

        with patch.object(main, "_require_host_key_writer", stale_read):
            r = client.post("/api/v1/devices/hk-dev/host-key",
                            json={"host_pubkey": self.KEY_B})

        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["pinned_fingerprint"] != detail["offered_fingerprint"]
        # The pin that got there first still stands.
        assert client.get("/api/v1/devices/hk-dev").json()["host_pubkey"] == self.KEY_A

    def test_concurrent_first_hops_produce_one_pin_and_one_409(self, client):
        """Two hops racing to pin different keys: exactly one wins.

        An invariant check, not the regression test — sqlite serialises writes, so
        this does not reliably reproduce the interleaving (it passes against the
        racy read-then-write too). The test above is the one that fails without
        the conditional UPDATE.
        """
        import threading

        _register_device(client, "hk-dev", "hk-host")
        results = {}
        start = threading.Barrier(2)

        def attempt(name, key):
            start.wait()
            r = client.post("/api/v1/devices/hk-dev/host-key",
                            json={"host_pubkey": key})
            results[name] = r.status_code

        threads = [
            threading.Thread(target=attempt, args=("a", self.KEY_A)),
            threading.Thread(target=attempt, args=("b", self.KEY_B)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        codes = sorted(results.values())
        assert codes == [200, 409], f"expected one win and one conflict, got {results}"
        # And the winner's key is what is pinned — not a blend or the loser's.
        pinned = client.get("/api/v1/devices/hk-dev").json()["host_pubkey"]
        assert pinned in (self.KEY_A, self.KEY_B)
        winner = "a" if results["a"] == 200 else "b"
        assert pinned == (self.KEY_A if winner == "a" else self.KEY_B)


# ===========================================================================
# Host key pinned at enrollment, not on the first hop (#821)
# ===========================================================================

class TestHostKeyAtEnrollment:
    KEY_A = _ed25519_hostkey(b"\x11" * 32)
    KEY_B = _ed25519_hostkey(b"\x22" * 32)

    def _enroll_slot(self, client, device_id):
        """A device created to be enrolled — `POST /enroll` leaves it 'pending'.

        Not `POST /devices`, which creates devices already 'online' and so never
        pins at activation.
        """
        r = client.post("/api/v1/enroll", json={
            "device_id": device_id,
            "access_target": "direct-ssh",
            "target_ip": "198.51.100.9",
            "target_ssh_user": "you",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        return body.get("token") or body["bootstrap_url"].rsplit("/", 1)[1]

    def _retoken(self, client, device_id):
        r = client.post(f"/api/v1/enroll/{device_id}/retoken")
        assert r.status_code == 200, r.text
        body = r.json()
        return body.get("token") or body["bootstrap_url"].rsplit("/", 1)[1]

    def _activate(self, client, token, **extra):
        payload = {"token": token,
                   "ssh_pubkey": "ssh-ed25519 AAAAclientkey", **extra}
        return client.post("/api/v1/devices/activate", json=payload)

    def test_a_first_enrollment_pins_the_reported_key(self, client):
        token = self._enroll_slot(client, "enr-dev")
        r = self._activate(client, token, host_pubkey=self.KEY_A)
        assert r.status_code == 200, r.text
        assert r.json()["host_key_pinned"] is True

        dev = client.get("/api/v1/devices/enr-dev").json()
        assert dev["host_pubkey"] == self.KEY_A
        assert dev["host_pubkey_recorded_at"]

    def test_reporting_no_key_leaves_the_device_unpinned(self, client):
        """An older bootstrap, or one whose ssh-keyscan found nothing: the gateway
        still learns the key on first hop."""
        token = self._enroll_slot(client, "enr-dev")
        r = self._activate(client, token)
        assert r.status_code == 200, r.text
        assert r.json()["host_key_pinned"] is None
        assert client.get("/api/v1/devices/enr-dev").json()["host_pubkey"] is None

    def test_a_re_bootstrap_cannot_move_an_existing_pin(self, client):
        """`/enroll/{id}/retoken` mints a token for any MANAGER of the device, not
        just the admin. If activation could re-pin, a manager could point the
        gateway at a key of their choosing and capture other seats' sessions to a
        device others already use — the escalation POST /host-key refuses. A real
        re-key goes through DELETE."""
        token = self._enroll_slot(client, "enr-dev")
        self._activate(client, token, host_pubkey=self.KEY_A)

        r = self._activate(client, self._retoken(client, "enr-dev"),
                           host_pubkey=self.KEY_B)
        assert r.status_code == 200, r.text
        assert r.json()["host_key_pinned"] is False
        assert client.get("/api/v1/devices/enr-dev").json()["host_pubkey"] == self.KEY_A

    def test_an_activated_but_unpinned_device_is_not_pinned_by_a_re_bootstrap(self, client):
        """Devices activated before pinning existed keep learning on first hop.

        Pinning them here would hand a manager an arbitrary-key write on a device
        other seats already hop to.
        """
        token = self._enroll_slot(client, "enr-dev")
        self._activate(client, token)                     # activated, unpinned
        assert client.get("/api/v1/devices/enr-dev").json()["status"] == "online"

        r = self._activate(client, self._retoken(client, "enr-dev"),
                           host_pubkey=self.KEY_B)
        assert r.status_code == 200, r.text
        assert r.json()["host_key_pinned"] is False
        assert client.get("/api/v1/devices/enr-dev").json()["host_pubkey"] is None

    def test_a_device_registered_directly_is_never_pinned_at_activation(self, client):
        """`POST /devices` creates devices already 'online', so they are outside the
        pending window by construction and fall back to learning on first hop."""
        _register_device(client, "direct-dev", "direct-host")
        assert client.get("/api/v1/devices/direct-dev").json()["status"] == "online"

        r = self._activate(client, self._retoken(client, "direct-dev"),
                           host_pubkey=self.KEY_A)
        assert r.status_code == 200, r.text
        assert r.json()["host_key_pinned"] is False
        assert client.get("/api/v1/devices/direct-dev").json()["host_pubkey"] is None

    def test_a_malformed_reported_key_is_refused(self, client):
        """Validated on the way in — the gateway writes this into known_hosts."""
        token = self._enroll_slot(client, "enr-dev")
        r = self._activate(client, token,
                           host_pubkey=self.KEY_A + "\n* " + self.KEY_B)
        assert r.status_code == 400, r.text
        assert client.get("/api/v1/devices/enr-dev").json()["host_pubkey"] is None

    def test_an_enrollment_pin_is_cleared_and_relearned_like_any_other(self, client):
        """However it got there, a pin is cleared the one way."""
        token = self._enroll_slot(client, "enr-dev")
        self._activate(client, token, host_pubkey=self.KEY_A)

        assert client.delete("/api/v1/devices/enr-dev/host-key").json()["cleared"] is True
        r = client.post("/api/v1/devices/enr-dev/host-key",
                        json={"host_pubkey": self.KEY_B})
        assert r.status_code == 200, r.text
        assert client.get("/api/v1/devices/enr-dev").json()["host_pubkey"] == self.KEY_B

# The control plane verifies the hops it makes (#822)
# ===========================================================================

class TestControlPlaneHostKeys:
    """`forward.py` makes two hops: registry → sshd container (anchored on the
    shared sshd-keys volume) and, inside sshd, → a device through its reverse
    tunnel (anchored on devices.host_pubkey, the pin the gateway establishes)."""

    KEY_SSHD = _ed25519_hostkey(b"\x33" * 32)
    KEY_DEV = _ed25519_hostkey(b"\x44" * 32)

    # --- outer hop: the sshd container -------------------------------------

    def test_the_sshd_host_key_is_read_from_the_volume(self, tmp_path, monkeypatch):
        import forward
        (tmp_path / "ssh_host_ed25519_key.pub").write_text(
            self.KEY_SSHD + " root@sshd\n")
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path))
        assert forward.sshd_host_pubkey() == self.KEY_SSHD

    def test_an_rsa_host_key_is_used_when_there_is_no_ed25519(self, tmp_path, monkeypatch):
        import forward
        (tmp_path / "ssh_host_rsa_key.pub").write_text("ssh-rsa AAAAfake comment\n")
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path))
        assert forward.sshd_host_pubkey() == "ssh-rsa AAAAfake"

    def test_a_missing_volume_reads_as_no_key(self, tmp_path, monkeypatch):
        import forward
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path / "absent"))
        assert forward.sshd_host_pubkey() is None

    def test_the_outer_hop_verifies_when_the_volume_is_mounted(self, tmp_path, monkeypatch):
        import forward
        keys = tmp_path / "keys"
        keys.mkdir()
        (keys / "ssh_host_ed25519_key.pub").write_text(self.KEY_SSHD + "\n")
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(keys))

        work = tmp_path / "work"
        work.mkdir()
        args = forward._outer_hostkey_args(str(work))
        joined = " ".join(args)
        assert "StrictHostKeyChecking=yes" in joined
        assert "HostKeyAlias=samcloud-sshd" in joined
        assert "/dev/null" not in joined
        # The known_hosts it points at really holds the volume's key, aliased.
        kh = work / "known_hosts"
        assert kh.read_text() == f"samcloud-sshd {self.KEY_SSHD}\n"

    def test_the_outer_hop_degrades_loudly_without_the_mount(self, tmp_path, monkeypatch, capsys):
        """Deliberately not fail-closed: this call establishes every service
        forward, so refusing on a missing mount would take working forwards down
        fleet-wide. It says what is missing instead."""
        import forward
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path / "absent"))
        monkeypatch.setattr(forward, "_warned", set())

        args = forward._outer_hostkey_args(str(tmp_path))
        assert "StrictHostKeyChecking=no" in " ".join(args)
        out = capsys.readouterr().out
        assert "cannot verify" in out and "sshd-keys" in out

    def test_the_missing_mount_warning_is_said_once(self, tmp_path, monkeypatch, capsys):
        import forward
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path / "absent"))
        monkeypatch.setattr(forward, "_warned", set())
        for _ in range(3):
            forward._outer_hostkey_args(str(tmp_path))
        assert capsys.readouterr().out.count("cannot verify") == 1

    def test_a_transient_missing_anchor_does_not_warn_forever(self, tmp_path, monkeypatch, capsys):
        """sshd starts after the registry, so the first probe can precede the key.

        That one line must not become the last word for the life of the process — and
        a later disappearance must warn again rather than be swallowed.
        """
        import forward
        keys = tmp_path / "keys"
        keys.mkdir()
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(keys))
        monkeypatch.setattr(forward, "_warned", set())

        # Before sshd publishes: warns, unverified.
        args = forward._outer_hostkey_args(str(tmp_path))
        assert "StrictHostKeyChecking=no" in " ".join(args)
        assert capsys.readouterr().out.count("cannot verify") == 1

        # sshd publishes: verifies, and the warning is re-armed.
        (keys / "ssh_host_ed25519_key.pub").write_text(self.KEY_SSHD + "\n")
        args = forward._outer_hostkey_args(str(tmp_path))
        assert "StrictHostKeyChecking=yes" in " ".join(args)

        # It goes away again: said once more, not swallowed.
        (keys / "ssh_host_ed25519_key.pub").unlink()
        forward._outer_hostkey_args(str(tmp_path))
        assert capsys.readouterr().out.count("cannot verify") == 1

    def test_the_probe_uses_the_same_anchor(self, tmp_path, monkeypatch):
        import forward
        (tmp_path / "ssh_host_ed25519_key.pub").write_text(self.KEY_SSHD + "\n")
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path))

        joined = " ".join(forward.probe_hostkey_args())
        assert "HostKeyAlias=samcloud-sshd" in joined
        assert "StrictHostKeyChecking=yes" in joined
        assert "/dev/null" not in joined

    # --- inner hop: the device, on the gateway's anchor ---------------------

    def test_the_device_pin_is_read_from_the_registry(self, client):
        import forward
        _register_device(client, "fwd-dev", "fwd-host")
        assert forward.device_host_pubkey("fwd-dev") is None

        client.post("/api/v1/devices/fwd-dev/host-key",
                    json={"host_pubkey": self.KEY_DEV})
        assert forward.device_host_pubkey("fwd-dev") == self.KEY_DEV

    def test_an_unknown_device_has_no_pin(self, client):
        import forward
        assert forward.device_host_pubkey("no-such-device") is None
        assert forward.device_host_pubkey("") is None

    def test_the_inner_hop_pins_the_device_when_it_has_a_pin(self, client, monkeypatch):
        """The command sent into sshd stages the pin and checks strictly, keyed on
        the device id — the same alias the gateway uses, so the two agree."""
        import forward
        _register_device(client, "fwd-dev", "fwd-host")
        client.post("/api/v1/devices/fwd-dev/host-key",
                    json={"host_pubkey": self.KEY_DEV})

        sent = {}

        def fake_outer(cmd, timeout=15):
            sent["cmd"] = cmd
            class R:
                returncode = 0
                stdout = stderr = ""
            return R()

        monkeypatch.setattr(forward, "_outer_ssh", fake_outer)
        monkeypatch.setattr(forward, "_is_listening_on_sshd", lambda p: "cmd" in sent)

        ok, msg = forward.start_forward(18001, 8080, 10001, "fwd-dev/svc", "alice")
        assert ok, msg
        cmd = sent["cmd"]
        assert "HostKeyAlias=samcloud-fwd-dev" in cmd
        assert "StrictHostKeyChecking=yes" in cmd
        assert "/dev/null" not in cmd
        # The pin is staged inside the container and removed after.
        assert f"samcloud-fwd-dev {self.KEY_DEV}" in cmd
        assert "rm -f /tmp/samcloud-kh-18001" in cmd

    def test_the_inner_hop_still_works_for_an_unpinned_device(self, client, monkeypatch, capsys):
        """A device the gateway has not reached yet must not lose its forwards."""
        import forward
        _register_device(client, "fwd-dev", "fwd-host")

        sent = {}

        def fake_outer(cmd, timeout=15):
            sent["cmd"] = cmd
            class R:
                returncode = 0
                stdout = stderr = ""
            return R()

        monkeypatch.setattr(forward, "_outer_ssh", fake_outer)
        monkeypatch.setattr(forward, "_is_listening_on_sshd", lambda p: "cmd" in sent)
        monkeypatch.setattr(forward, "_warned", set())

        ok, msg = forward.start_forward(18002, 8080, 10001, "fwd-dev/svc", "alice")
        assert ok, msg
        assert "StrictHostKeyChecking=no" in sent["cmd"]
        assert "HostKeyAlias" not in sent["cmd"]
        assert "no pinned host key" in capsys.readouterr().out

    def test_the_inner_hop_keeps_sshs_exit_status(self, client, monkeypatch):
        """Staging a known_hosts file must not swallow a failure.

        `ssh ...; rm ...` would report rm's status, so a refused host key or a dead
        tunnel would read as a started forward. Run the real command shape through a
        shell with ssh stubbed both ways.
        """
        import subprocess as sp
        import forward
        _register_device(client, "fwd-dev", "fwd-host")
        client.post("/api/v1/devices/fwd-dev/host-key",
                    json={"host_pubkey": self.KEY_DEV})

        sent = {}

        def fake_outer(cmd, timeout=15):
            sent["cmd"] = cmd
            class R:
                returncode = 0
                stdout = stderr = ""
            return R()

        monkeypatch.setattr(forward, "_outer_ssh", fake_outer)
        monkeypatch.setattr(forward, "_is_listening_on_sshd", lambda p: "cmd" in sent)
        forward.start_forward(18009, 8080, 10001, "fwd-dev/svc", "alice")

        cmd = sent["cmd"].replace("/etc/ssh/keys/gateway", "/dev/null")
        # Only meaningful if a file really is staged and removed — otherwise there is
        # no cleanup step to swallow the status and this would pass vacuously.
        assert "rm -f /tmp/samcloud-kh-18009" in cmd

        failing = sp.run(["bash", "-c", cmd.replace("ssh -fN", "false ")],
                         capture_output=True, text=True)
        assert failing.returncode != 0, "a failed inner ssh was reported as success"
        passing = sp.run(["bash", "-c", cmd.replace("ssh -fN", "true ")],
                         capture_output=True, text=True)
        assert passing.returncode == 0, passing.stderr
        # And the staged file does not outlive the command.
        assert not Path("/tmp/samcloud-kh-18009").exists()

    # --- the anchor must stay public-only ----------------------------------

    def _compose_mounts(self, service: str) -> list[str]:
        """The volume mounts of one service in deploy/docker-compose.yml.

        Parsed by indentation rather than with pyyaml, which is not in
        registry/requirements.txt — a guard nobody can run is not a guard. Only the
        `volumes:` entries, not the whole block: prose about a volume is not a mount
        of it, and matching comment text would make this fire on its own rationale.
        """
        compose = (Path(__file__).resolve().parent.parent
                   / "deploy" / "docker-compose.yml").read_text().splitlines()
        mounts, in_service, in_volumes = [], False, False
        for line in compose:
            if line.startswith(f"  {service}:"):
                in_service = True
                continue
            if not in_service:
                continue
            # Next service (a two-space key) ends the block.
            if line and not line.startswith("   ") and not line.startswith("#"):
                break
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if line.startswith("    volumes:"):
                in_volumes = True
                continue
            if in_volumes:
                # Indentation decides, not content. `volumes:` sits at 4,
                # its list items at 6, long-form keys at 8+. Anything at 4 or
                # less is the next service-level key and ends the block.
                #
                # The first cut of this matched on "has a colon" instead, which
                # swallowed `environment:`, `REGISTRY_URL: …` and `ports:` as
                # mount keys and then counted `"${SSH_PORT:-2200}:22"` as a
                # mount. Every presence assertion still passed; only the count
                # caught it (#901).
                indent = len(line) - len(line.lstrip())
                if indent <= 4:
                    in_volumes = False
                elif stripped.startswith("- type:"):
                    # Long form: one mount spread over the following keys.
                    # Collected as a block rather than ending the scan -- the
                    # old parser stopped at the first non-"- " line, so one
                    # long-form entry made every mount after it invisible and
                    # these guards passed on a truncated list. Normalised to
                    # source:target[:ro][:subpath=…] so short-form assertions
                    # are unaffected.
                    mounts.append({"type": stripped[len("- type:"):].strip()})
                elif stripped.startswith("- "):
                    mounts.append(stripped[2:].strip())
                elif indent >= 8 and mounts and isinstance(mounts[-1], dict):
                    if ":" in stripped:
                        k, _, v = stripped.partition(":")
                        mounts[-1][k.strip()] = v.strip()
                else:
                    in_volumes = False
        flat = []
        for m in mounts:
            if not isinstance(m, dict):
                flat.append(m)
                continue
            spec = f"{m.get('source', '?')}:{m.get('target', '?')}"
            if str(m.get("read_only", "")).lower() == "true":
                spec += ":ro"
            if m.get("subpath"):
                spec += f":subpath={m['subpath']}"
            flat.append(spec)
        assert flat, f"no volumes parsed for {service}"
        return flat

    def test_the_registry_never_mounts_the_private_host_keys(self):
        """The registry must not be able to impersonate the gateway.

        `sshd-keys` holds ssh_host_ed25519_key and ssh_host_rsa_key — the PRIVATE
        halves. Mounting that volume into the registry, even :ro, would let a
        compromised registry present itself AS the gateway to every device that
        dials in, which is the impersonation a published gateway host key exists to
        prevent. It gets `sshd-hostpub`, public halves only.
        """
        mounts = self._compose_mounts("registry")
        assert "sshd-hostpub:/sshd-hostpub:ro" in mounts, mounts
        assert not any("sshd-keys" in m for m in mounts), (
            f"the registry mounts the sshd PRIVATE host keys: {mounts}")

    def test_sshd_publishes_only_the_public_halves(self):
        """By name, not by glob, so nothing else in the key dir leaks with them."""
        entrypoint = (Path(__file__).resolve().parent.parent
                      / "deploy" / "sshd-entrypoint.sh").read_text()
        assert 'HOSTPUB_DIR="/etc/ssh/hostpub"' in entrypoint
        assert "for k in ssh_host_ed25519_key ssh_host_rsa_key; do" in entrypoint
        # A glob would sweep up anything else that lands in KEYS_DIR.
        assert '"$KEYS_DIR"/*.pub' not in entrypoint
        assert 'cp "$KEYS_DIR/$k.pub" "$HOSTPUB_DIR/$k.pub"' in entrypoint

    def test_sshd_still_holds_the_private_keys_and_shares_the_public_ones(self):
        mounts = self._compose_mounts("sshd")
        assert "sshd-keys:/etc/ssh/keys" in mounts, mounts        # private, unshared
        assert "sshd-hostpub:/etc/ssh/hostpub" in mounts, mounts  # public, written here

    # --- the gateway gets two files, not the data volume (#901, #757) -------

    def test_sshd_does_not_mount_the_registry_data_volume_root(self):
        """The gateway container is reachable by every device tunnel key. Mounting
        the volume root gave it read access to registry.db, two pre-migration
        backups, the Caddyfile and the uploaded files — so a one-shot exec through
        any of those keys read every ticket, doc, task and file on the plane."""
        mounts = self._compose_mounts("sshd")
        roots = [m for m in mounts if m.split(":")[1:2] == ["/registry-data"]]
        assert roots == [], f"sshd mounts the data volume root: {roots}"
        assert not any("registry-data:/registry-data:" in m for m in mounts), mounts

    def test_sshd_mounts_exactly_the_two_files_the_entrypoint_reads(self):
        """A count, not a presence: a third mount added later should fail here
        rather than ride along, and a parser that silently truncated the list
        would read as a clean pass."""
        mounts = self._compose_mounts("sshd")
        assert len(mounts) == 4, f"expected 4 sshd mounts, got {len(mounts)}: {mounts}"
        assert ("registry-data:/registry-data/authorized_keys:ro"
                ":subpath=authorized_keys") in mounts, mounts
        assert ("registry-data:/registry-data/keys/gateway:ro"
                ":subpath=keys/gateway") in mounts, mounts

    def test_the_mount_targets_are_the_paths_the_entrypoint_actually_reads(self):
        """Compose and the entrypoint have to agree, and neither can see the
        other. If a future edit moves a mount target, the container boots with
        no authorized_keys and refuses every key — so the two are asserted
        against each other rather than each against a remembered value."""
        entrypoint = (Path(__file__).resolve().parent.parent
                      / "deploy" / "sshd-entrypoint.sh").read_text()
        assert 'REGISTRY_DATA="/registry-data"' in entrypoint
        assert '"$REGISTRY_DATA/authorized_keys"' in entrypoint
        assert '"$REGISTRY_DATA/keys/gateway"' in entrypoint
        mounts = self._compose_mounts("sshd")
        for path in ("/registry-data/authorized_keys", "/registry-data/keys/gateway"):
            assert any(f":{path}:" in m for m in mounts), (
                f"the entrypoint reads {path} and nothing mounts it: {mounts}")


# ===========================================================================
# The gateway publishes its host key, so a device can verify the tunnel (#825)
# ===========================================================================

class TestTheSuiteDoesNotRunFirstBootSetup:
    """#853 — running the tests used to perform a machine's first-boot setup.

    `main.lifespan` generates the gateway keypair and mints a real device
    token, and the `client` fixture starts a lifespan for every test that
    uses it. Until conftest pointed them at a temp directory, both paths
    resolved outside the repo. The failure was loud on a machine that cannot
    write `/var/lib` and silent on one that can, so it is asserted from both
    ends: where the paths point, and what a run left behind.
    """

    def test_the_gateway_paths_resolve_inside_the_test_tempdir(self):
        """The one that fails without the conftest change, on any machine.

        `main.GATEWAY_KEY_PATH` is read at import, so this also pins that the
        isolation happens early enough — a fixture-based fix would leave the
        module constant pointing at /var/lib and still pass the env checks.
        """
        import main
        tmp = Path(conftest.GATEWAY_TMPDIR)
        assert Path(main.GATEWAY_KEY_PATH).parent == tmp, (
            f"main.GATEWAY_KEY_PATH is {main.GATEWAY_KEY_PATH}, outside {tmp} — "
            "the module constant is read at import and was resolved too late")
        for var in ("GATEWAY_KEY", "GATEWAY_TOKEN_PATH"):
            assert Path(os.environ[var]).parent == tmp, (
                f"${var} is {os.environ.get(var)}, outside {tmp}")

    def test_a_run_leaves_the_real_first_boot_paths_untouched(self, client):
        """`client` has started a lifespan, so first boot ran during this test.

        Catches a creation on a machine where the real paths are absent and
        writable, and an overwrite on one where they exist — neither of which
        the check above would see, since it only reads where we intend to
        write. Unchanged-None is the assertion that matters on a fresh seat.
        """
        assert conftest.first_boot_path_state() == conftest.REAL_PATHS_BEFORE_TESTS, (
            "a test run changed /var/lib/samcloud/gateway or the gateway token "
            "in $HOME — lifespan reached a real first-boot path")


class TestGatewayHostKey:
    KEY_ED = _ed25519_hostkey(b"\x55" * 32)

    def _publish(self, tmp_path, monkeypatch, ed=True, rsa=False):
        import forward
        if ed:
            (tmp_path / "ssh_host_ed25519_key.pub").write_text(self.KEY_ED + " root@sshd\n")
        if rsa:
            (tmp_path / "ssh_host_rsa_key.pub").write_text("ssh-rsa AAAAfakersa comment\n")
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path))

    def test_it_serves_the_published_keys_and_the_alias(self, client, tmp_path, monkeypatch):
        self._publish(tmp_path, monkeypatch, ed=True, rsa=True)
        r = client.get("/api/v1/gateway/host-key")
        assert r.status_code == 200, r.text
        body = r.json()
        # Both types, so a device can pin whichever the gateway negotiates.
        assert body["host_keys"] == [self.KEY_ED, "ssh-rsa AAAAfakersa"]
        assert body["alias"] == "samcloud-gateway"

    def test_a_trailing_comment_is_stripped(self, client, tmp_path, monkeypatch):
        """The value goes straight into a known_hosts line, so it is the two fields."""
        self._publish(tmp_path, monkeypatch)
        keys = client.get("/api/v1/gateway/host-key").json()["host_keys"]
        assert keys == [self.KEY_ED]
        assert all(len(k.split()) == 2 for k in keys)

    def test_it_needs_no_bearer(self, client, tmp_path, monkeypatch):
        """Deliberately unauthenticated: the gateway offers these keys to anyone who
        opens a TCP connection to its SSH port, and a bootstrapping device may hold
        only a single-use enrollment token."""
        self._publish(tmp_path, monkeypatch)
        r = client._client.get("/api/v1/gateway/host-key")   # no Authorization header
        assert r.status_code == 200, r.text
        assert r.json()["host_keys"] == [self.KEY_ED]

    def test_unpublished_is_503_not_an_empty_list(self, client, tmp_path, monkeypatch):
        """A caller must be able to tell 'not published' from 'the gateway has none'."""
        import forward
        monkeypatch.setattr(forward, "SSHD_HOST_KEYS_DIR", str(tmp_path / "absent"))
        r = client.get("/api/v1/gateway/host-key")
        assert r.status_code == 503, r.text
        assert "not published" in str(r.json()["detail"])

    def test_activation_hands_the_device_the_host_keys(self, client, tmp_path, monkeypatch):
        """So the first tunnel connect is verified, with no second call."""
        self._publish(tmp_path, monkeypatch)
        r = client.post("/api/v1/enroll", json={
            "device_id": "gw-dev", "access_target": "reverse-tunnel",
            "target_ssh_user": "you",
        })
        assert r.status_code == 200, r.text
        token = r.json().get("token") or r.json()["bootstrap_url"].rsplit("/", 1)[1]

        act = client.post("/api/v1/devices/activate",
                          json={"token": token, "ssh_pubkey": "ssh-ed25519 AAAAclient"})
        assert act.status_code == 200, act.text
        body = act.json()
        assert body["gateway_host_keys"] == [self.KEY_ED]
        assert body["gateway_host_key_alias"] == "samcloud-gateway"
        # Not to be confused with the gateway's CLIENT key, which is a different field.
        assert "gateway_pubkey" in body

    def test_the_index_advertises_it(self, client):
        idx = client.get("/api/v1/").json()
        gw = idx["endpoints"]["gateway"]["host_key"]
        assert gw["path"] == "/api/v1/gateway/host-key"
        assert gw["auth"] == "none"

    # --- the tunnel bootstrap writes, and why it is a loop ------------------

    def _bootstrap(self) -> str:
        return (Path(__file__).resolve().parent / "bootstrap.sh").read_text()

    def test_the_tunnel_verifies_the_gateway(self):
        b = self._bootstrap()
        runner = b.split('cat > "$RUNNER" <<RUNNER_EOF')[1].split("RUNNER_EOF")[0]
        assert "StrictHostKeyChecking=yes" in runner
        assert "HostKeyAlias=" in runner
        # Specifically the known_hosts sink, not any /dev/null — the runner also uses
        # `2>/dev/null` to quieten curl, and matching that would fail on its own noise.
        assert "UserKnownHostsFile=/dev/null" not in runner
        assert "StrictHostKeyChecking=no" not in runner

    def test_the_refresh_runs_before_every_connect(self):
        """The reason this is a loop and not a unit pre-start hook.

        autossh restarts ssh itself and with AUTOSSH_GATETIME=0 never exits, so a
        pre-start hook fires once at boot and never again — a gateway re-key would
        then refuse forever, which is the outage in CHANGELOG.md with more steps.
        """
        b = self._bootstrap()
        runner = b.split('cat > "$RUNNER" <<RUNNER_EOF')[1].split("RUNNER_EOF")[0]
        assert "while :; do" in runner
        fetch = runner.index("/api/v1/gateway/host-key")
        connect = runner.index("ssh -N")
        assert fetch < connect, "the connect happens before the refresh"

    def test_the_refresh_never_wipes_a_working_pin(self):
        """Written atomically, and only when the fetch succeeded and parsed."""
        b = self._bootstrap()
        runner = b.split('cat > "$RUNNER" <<RUNNER_EOF')[1].split("RUNNER_EOF")[0]
        assert '.new" && mv' in runner, "the pin is not written atomically"
        # The write is guarded by the parsed result being non-empty.
        assert 'if [ -n "\\$LINES" ]; then' in runner

    def test_autossh_is_not_used_for_the_tunnel(self):
        """It would swallow the exits this design depends on."""
        b = self._bootstrap()
        unit = b.split("[Service]")[1].split("[Install]")[0]
        assert "ExecStart=${RUNNER}" in unit
        assert "autossh" not in unit
        assert "AUTOSSH_GATETIME" not in unit

    def test_the_ssh_config_alias_is_verified_too(self):
        """`ssh samcloud-gw` from the device was unverified as well."""
        b = self._bootstrap()
        cfg = b.split("${CFG_MARK}")[1].split("EOF")[0]
        assert "StrictHostKeyChecking yes" in cfg
        assert "HostKeyAlias" in cfg
        assert "StrictHostKeyChecking no" not in cfg

    # --- a strict tunnel makes a silent miss fatal, so the bootstrap says so ---
    #
    # host-1 (2026-11-03) was the first Mac enrolled after the tunnel went strict:
    # Remote Login was off and the bootstrap still activated and reported a tunnel,
    # which then never connected. These run the device-side pieces, not just grep them.

    def _helpers(self) -> str:
        return (
            'ok()   { echo "OK $*"; }\n'
            'info() { echo "INFO $*"; }\n'
            'warn() { echo "WARN $*"; }\n'
            'fail() { echo "FAIL $*"; }\n'
        )

    def _run(self, script: str, tmp_path, env_extra=None):
        import subprocess
        f = tmp_path / "piece.sh"
        f.write_text("set -euo pipefail\n" + self._helpers() + script)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
        env.update(env_extra or {})
        return subprocess.run(["bash", str(f)], capture_output=True, text=True, env=env)

    def test_the_preflights_run_before_the_token_is_consumed(self):
        b = self._bootstrap()
        activate = b.index('"${API}/api/v1/devices/activate"')
        assert b.index("python3 -c 'import json'") < activate
        assert b.index('/dev/tcp/127.0.0.1/${LOCAL_SSH_PORT}') < activate

    def test_a_python3_that_does_not_run_stops_the_bootstrap(self, tmp_path):
        b = self._bootstrap()
        piece = b[b.index("if ! python3 -c 'import json'"):b.index("# Detect local sshd port")]
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "python3").write_text("#!/bin/sh\nexit 1\n")
        (stub / "python3").chmod(0o755)
        r = self._run('OS=macos\n' + piece + '\necho CONTINUED\n', tmp_path,
                      {"PATH": f"{stub}:/usr/bin:/bin"})
        assert r.returncode == 1
        assert "FAIL python3 does not run" in r.stdout
        assert "xcode-select --install" in r.stdout
        assert "CONTINUED" not in r.stdout

    def test_nothing_answering_on_the_sshd_port_stops_the_bootstrap(self, tmp_path):
        import socket
        b = self._bootstrap()
        piece = b[b.index('if ! (exec 3<>'):b.index("# The host key this machine's sshd")]
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        r = self._run(f'OS=macos\nLOCAL_SSH_PORT={closed}\n' + piece + '\necho CONTINUED\n', tmp_path)
        assert r.returncode == 1
        assert "Remote Login" in r.stdout
        assert "CONTINUED" not in r.stdout

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            r = self._run(f'OS=macos\nLOCAL_SSH_PORT={listener.getsockname()[1]}\n' + piece + '\necho CONTINUED\n', tmp_path)
        finally:
            listener.close()
        assert r.returncode == 0, r.stdout + r.stderr
        assert "CONTINUED" in r.stdout

    def _activation_piece(self) -> str:
        b = self._bootstrap()
        start = b.index("json_get() {")
        end = b.index('case "$HOST_KEY_PINNED" in')
        return b[start:end]

    def test_the_activation_answer_writes_the_gateway_pin(self, tmp_path):
        resp = json.dumps({"device_token": "t", "gateway_host_key_alias": "samcloud-gateway",
                           "gateway_host_keys": [self.KEY_ED]})
        cfg = tmp_path / "cfg"
        r = self._run(f"RESP='{resp}'\nAPI=http://127.0.0.1:9\nCFG_DIR={cfg}\n" + self._activation_piece(), tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        kt, kb = self.KEY_ED.split()[:2]
        assert (cfg / "known_hosts").read_text().strip() == f"samcloud-gateway {kt} {kb}"
        assert "FAIL" not in r.stdout

    def test_a_missing_gateway_pin_is_a_failure_not_a_warning(self, tmp_path):
        resp = json.dumps({"device_token": "", "gateway_host_keys": []})
        cfg = tmp_path / "cfg"
        r = self._run(f"RESP='{resp}'\nAPI=http://127.0.0.1:9\nCFG_DIR={cfg}\n" + self._activation_piece(), tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "FAIL Could not get the gateway's host key" in r.stdout
        assert "FAIL The activation answer carried no device token" in r.stdout
        assert not (cfg / "known_hosts").exists()

    def test_verify_checks_the_tunnel_connected_not_just_registered(self):
        b = self._bootstrap()
        verify = b.split('step "Verify"')[1]
        assert 'chk "Gateway host key"' in verify
        assert 'chk "Tunnel connected"' in verify
        assert "ExitOnForwardFailure=yes" in b

    def test_no_comment_says_the_tunnel_skips_the_host_key_check(self):
        assert "StrictHostKeyChecking=no and" not in self._bootstrap()

    def test_the_powershell_bootstrap_carries_the_same_checks(self):
        ps = (Path(__file__).resolve().parent / "bootstrap.ps1").read_text()
        activate = ps.index("/api/v1/devices/activate")
        assert ps.index('$probe.Connect("127.0.0.1", 22)') < activate
        assert "the tunnel refuses to connect until it has one" in ps
        assert 'Check "Gateway host key"' in ps
        assert 'Check "Tunnel connected"' in ps


# ===========================================================================
# Files — the upload path (#889)
#
# The files plane shipped with no tests at all, which is how a .ply capture
# ended up in the strictest size tier and how the handler kept buffering whole
# uploads in RAM. These cover the three things that make a cap above a few
# hundred MB safe to offer: one cap for every type, constant memory in both
# directions, and a reserve on the volume the control plane also writes to.
# ===========================================================================

class _Uploads:
    """Mixin: a blob root per test, so "nothing was left behind" can be checked."""

    @pytest.fixture(autouse=True)
    def _blob_root(self, tmp_path):
        import main
        root = tmp_path / "blobs"
        root.mkdir()
        with patch.object(main.blobs, "root", root):
            self.root = root
            yield root

    def _upload(self, client, body, name="capture.ply",
                ctype="application/octet-stream"):
        return client.post("/api/v1/files",
                           files={"file": (name, body, ctype)})

    def _stored(self):
        """Committed blobs — inside the two-char shards, never a temp."""
        return [f for f in self.root.rglob("*")
                if f.is_file() and not f.name.startswith(".incoming-")]

    def _temps(self):
        return list(self.root.glob(".incoming-*"))


class TestFileUploadCap(_Uploads):
    def test_the_generic_binary_type_is_not_the_strictest_tier(self):
        """A Gaussian splat (.ply) is application/octet-stream to every browser,
        and octet-stream used to mean 100 MB while video meant 800 MB — so
        splat captures bounced at 100 MB while a 101 MB screencap of them went
        through (files 613 vs 614). One cap now, sized for a real capture."""
        import main
        assert isinstance(main.FILE_MAX_BYTES, int), \
            "a per-type table is what put .ply in the strictest tier"
        assert main.FILE_MAX_BYTES >= 1024 ** 3

    def test_an_upload_over_the_cap_is_refused_and_leaves_nothing(self, client):
        import main
        with patch.object(main, "FILE_MAX_BYTES", 2 * 1024 * 1024):
            r = self._upload(client, b"x" * (3 * 1024 * 1024))
        assert r.status_code == 413
        assert "2 MB" in r.json()["detail"], "the limit is reported in MB, from the constant"
        assert self._stored() == [] and self._temps() == []
        assert client.get("/api/v1/files").json()["files"] == []

    def test_an_upload_at_the_cap_is_accepted(self, client):
        import main
        body = b"y" * (2 * 1024 * 1024)
        with patch.object(main, "FILE_MAX_BYTES", len(body)):
            r = self._upload(client, body)
        assert r.status_code == 200, r.text
        assert r.json()["bytes"] == len(body)

    def test_an_empty_upload_is_refused_and_leaves_nothing(self, client):
        r = self._upload(client, b"")
        assert r.status_code == 400
        assert self._stored() == [] and self._temps() == []


class TestFileUploadStreams(_Uploads):
    """Neither direction may hold a whole file in memory: the registry runs in a
    7.7 GB VM alongside the console, so a 1 GB capture has to cost a buffer."""

    def test_the_store_only_offers_a_streaming_seam(self):
        """`_LocalBlobStore` has no get()/put() any more. A reader who
        reintroduced either — the obvious way to "simplify" this — would be
        reintroducing the allocation, so the absence is asserted."""
        import main
        assert not hasattr(main.blobs, "get")
        assert not hasattr(main.blobs, "put")

    def test_what_lands_on_disk_is_hashed_from_the_stream(self, client):
        """The address is the hash of what was written, not of a buffer the
        handler kept, and the commit leaves no temp behind."""
        body = b"ply" * 5000
        r = self._upload(client, body)
        assert r.status_code == 200, r.text
        sha = hashlib.sha256(body).hexdigest()
        stored = self._stored()
        assert len(stored) == 1
        assert stored[0].name == sha
        assert stored[0].read_bytes() == body
        assert r.json()["sha256"] == sha
        assert self._temps() == []

    def test_download_streams_off_the_volume(self, client):
        """Served with FileResponse from the content-addressed path. Asserted by
        what the handler constructs rather than by memory: a Response(content=…)
        has to read the blob whole before it can exist."""
        import main
        body = b"splat" * 4000
        fid = self._upload(client, body).json()["id"]

        seen = {}
        real = main.FileResponse

        def _spy(path, **kw):
            seen["path"] = path
            return real(path, **kw)

        with patch.object(main, "FileResponse", _spy):
            r = client.get(f"/api/v1/files/{fid}/raw")
        assert r.status_code == 200
        assert r.content == body
        assert seen["path"] == main.blobs.path(hashlib.sha256(body).hexdigest())

    def test_identical_content_is_stored_once(self, client):
        body = b"same" * 3000
        a = self._upload(client, body, name="a.ply").json()
        b = self._upload(client, body, name="b.ply").json()
        assert a["id"] != b["id"] and a["sha256"] == b["sha256"]
        assert len(self._stored()) == 1, "de-duplication survived the rewrite"
        assert self._temps() == [], "the second upload's temp was cleaned up"


class TestFileUploadHeadroom(_Uploads):
    """The size cap was standing in for a disk check; now the disk check is the
    disk check. On 2026-09-24 a full volume made sqlite fail every write and,
    because `verify_token` writes last_used on every authenticated request, the
    whole plane 500'd. Uploads must not be able to do that."""

    def test_an_upload_that_would_eat_the_reserve_is_refused(self, client):
        import main
        with patch.object(main.blobs, "free_bytes",
                          return_value=main.FILE_FREE_RESERVE_BYTES + 4 * 1024):
            r = self._upload(client, b"z" * (64 * 1024))
        assert r.status_code == 507
        assert "reserved for the control plane" in r.json()["detail"]
        assert self._stored() == [] and self._temps() == []

    def test_a_small_upload_still_fits_in_the_margin(self, client):
        """The refusal is measured against the bytes that actually arrive, not
        against the cap — otherwise a 10 KB avatar would bounce whenever free
        space sat under reserve + 1 GB, which on a 30 GB volume is most days."""
        import main
        with patch.object(main.blobs, "free_bytes",
                          return_value=main.FILE_FREE_RESERVE_BYTES + 64 * 1024):
            r = self._upload(client, b"w" * 4096)
        assert r.status_code == 200, r.text

    def test_a_volume_that_fills_mid_upload_keeps_nothing(self, client):
        """The real guarantee. The check above is not a reservation, so two
        uploads can both pass it and then compete; ENOSPC from the write is what
        settles it, and it must not leave a part-file on an already-full disk."""
        import main
        real_writer = main.blobs.writer
        made = []

        class _FullDisk:
            """Refuses what a full disk refuses, at the point a full disk
            refuses it — the write, not the commit."""

            def __init__(self):
                self._inner = real_writer()
                self.aborted = False

            def write(self, chunk):
                raise OSError(errno.ENOSPC, "No space left on device")

            def commit(self):
                raise AssertionError("commit must not be reached")

            def abort(self):
                self.aborted = True
                self._inner.abort()

        def _writer():
            made.append(_FullDisk())
            return made[-1]

        with patch.object(main.blobs, "writer", _writer):
            r = self._upload(client, b"q" * 4096)
        assert r.status_code == 507
        assert "filled while the upload was in flight" in r.json()["detail"]
        assert len(made) == 1 and made[0].aborted, "the part-file was not cleaned up"
        assert self._stored() == [] and self._temps() == []

    def test_a_non_enospc_oserror_is_not_dressed_up_as_a_full_disk(self, client):
        """A permissions or hardware fault must not tell the user to delete files."""
        import main

        class _Broken:
            def write(self, chunk):
                raise OSError(errno.EACCES, "Permission denied")

            def commit(self):
                raise AssertionError("commit must not be reached")

            def abort(self):
                pass

        with patch.object(main.blobs, "writer", _Broken):
            with pytest.raises(OSError) as exc:
                self._upload(client, b"q" * 4096)
        assert exc.value.errno == errno.EACCES



# ===========================================================================
# authorized_keys — the gateway's access-control file (#757, #901)
#
# It had no tests. The file is GENERATED: sync() rewrites every managed line
# from registry state, so a hand-edit of the live file reverts at the next
# device activation with its acceptance check having passed in between. That
# is why the fix is here and not on someone's keyboard.
# ===========================================================================

class TestAuthorizedKeysForcedCommand:
    """`restrict` disables PTY, agent, X11 and user-rc. It does NOT disable
    command execution — so a device key without a forced command gets arbitrary
    one-shot exec in the gateway container. 35 of the 54 keys on the live file
    were in that state."""

    def _sync_and_read(self, tmp_db):
        import authorized_keys as ak
        ak.sync()
        return [l for l in ak.AUTHORIZED_KEYS_PATH.read_text().splitlines() if l.strip()]

    def _device_lines(self, lines):
        return [l for l in lines if ":device:" in l and "samcloud-managed" in l]

    def _add_device(self, device_id="dev-tunnel", pubkey="ssh-ed25519 AAAAC3TESTKEY",
                    tunnel_port=10042):
        db.execute(
            "INSERT INTO devices (id, name, hostname, context, ssh_pubkey, tunnel_port) "
            "VALUES (?, ?, ?, 'samcloud', ?, ?)",
            (device_id, device_id, device_id, pubkey, tunnel_port))

    def test_every_device_key_carries_the_forced_command(self, tmp_db):
        self._add_device("dev-a", tunnel_port=10001)
        self._add_device("dev-b", tunnel_port=10002)
        self._add_device("dev-c", tunnel_port=None)
        dev = self._device_lines(self._sync_and_read(tmp_db))
        assert len(dev) == 3, f"expected 3 device lines, got {len(dev)}"
        missing = [l for l in dev if 'command="/bin/false"' not in l]
        assert missing == [], f"device keys with no forced command: {missing}"

    def test_the_tunnel_options_survive_the_forced_command(self, tmp_db):
        """The whole point: a forced command must not cost the tunnel. These are
        ssh *options*, not a command, so they coexist — and a device dials with
        `ssh -N`, which never opens the session channel the command would run in."""
        self._add_device("dev-port", tunnel_port=10042)
        line = self._device_lines(self._sync_and_read(tmp_db))[0]
        assert 'command="/bin/false"' in line
        assert "port-forwarding" in line
        assert 'permitlisten="localhost:10042"' in line
        assert "restrict" in line

    def test_a_device_with_no_tunnel_port_gets_no_permitlisten(self, tmp_db):
        """Guard on the pre-existing branch, so the option string can't drift
        into emitting an empty permitlisten."""
        self._add_device("dev-noport", tunnel_port=None)
        line = self._device_lines(self._sync_and_read(tmp_db))[0]
        assert "permitlisten" not in line
        assert 'command="/bin/false"' in line

    def test_a_device_with_no_tunnel_port_gets_no_forwarding(self, tmp_db):
        """`port-forwarding` with no permitlisten lets the key -R bind any free
        port in sshd: another device's tunnel port while that device is offline,
        or a service forward's port before the registry starts it."""
        self._add_device("dev-noport", tunnel_port=None)
        line = self._device_lines(self._sync_and_read(tmp_db))[0]
        assert "port-forwarding" not in line
        assert "restrict" in line

    def test_user_access_keys_keep_their_gateway_command(self, tmp_db):
        """Regression guard: the 19 user keys route to the gateway binary and
        must NOT be given /bin/false. Different key class, different command."""
        import json as _json
        db.execute(
            "INSERT INTO tokens (id, token_hash, type, owner_id, scopes) "
            "VALUES (?, ?, 'access_key', ?, ?)",
            ("tok-test-1", "sha256-not-a-real-hash", "someone",
             _json.dumps({"pubkey": "ssh-ed25519 AAAAC3USERKEY", "device": "dev-a"})))
        lines = self._sync_and_read(tmp_db)
        user = [l for l in lines if ":user:" in l or ("samcloud-managed" in l and ":device:" not in l and "command=" in l)]
        assert len(user) == 1, f"expected 1 user line, got {len(user)}: {user}"
        assert "/bin/false" not in user[0], "a user key must still reach the gateway"
        assert "samcloud-gateway" in user[0]
        assert "pty" in user[0], "user keys keep pty; device keys never had it"

    def test_unmanaged_lines_are_preserved(self, tmp_db):
        """sync() rewrites the managed block only. If this ever stopped holding,
        the change would silently delete hand-added keys from the live file."""
        import authorized_keys as ak
        ak.AUTHORIZED_KEYS_PATH.write_text(
            "ssh-ed25519 AAAAHANDADDED someone@laptop\n")
        self._add_device("dev-a")
        lines = self._sync_and_read(tmp_db)
        assert any("AAAAHANDADDED" in l for l in lines), "an unmanaged key was dropped"


# ===========================================================================
# The agent guide documents the revision guard (#900)
#
# `base_rev` exists, is optional, and the page agents read omitted it. The cost
# was not theoretical: a full-body write from a stale copy silently dropped rows
# from doc 115 on 10-08, and on 10-10 a seat overwrote a circulated doc's body
# with a five-word probe to find out the parameter was real. Three seats have
# independently written the mechanism into their own memory because the page did
# not carry it. These assert the page does.
# ===========================================================================

class TestDocsSkillDocumentsTheRevisionGuard:
    def _page(self) -> str:
        return (Path(__file__).resolve().parent / "skills" / "samcloud-agent"
                / "docs.md").read_text()

    def test_the_revisions_example_carries_base_rev(self):
        """Not merely mentioned somewhere — in the call an agent copies."""
        page = self._page()
        lines = [l for l in page.splitlines()
                 if "/docs/{id}/revisions" in l and "POST" in l]
        assert len(lines) == 1, f"expected one POST /revisions example, got {lines}"
        assert "base_rev" in lines[0], f"the example omits base_rev: {lines[0]}"

    def test_the_field_to_read_is_named_current_rev(self):
        """`rev` and `current_rev` return the same value and only one is
        compared. A reader who picks the shorter name gets a 409 they cannot
        diagnose, so the page names the right one and says which."""
        page = self._page()
        assert "current_rev" in page
        assert "not `rev`" in page, "the page does not steer the reader off `rev`"

    def test_append_is_recommended_over_revisions_with_its_reason(self):
        """`/append` reads the head server-side and cannot lose a revision. The
        page has to say why, not just list it — a list is not a recommendation."""
        page = self._page()
        assert "/docs/{id}/append" in page
        assert "cannot lose" in page, "the page lists append without its reason"

    def test_the_409_is_described_as_merge_not_retry(self):
        page = self._page()
        assert "409" in page
        assert "not a retry" in page, "a reader told to retry a 409 will loop"
    def test_the_probe_instruction_is_computable_not_a_belief(self):
        """The first version of this page told the reader to pass a base_rev
        they believed to be stale. A seat followed that on 2026-10-10, passed
        `1` at a doc sitting at rev 1, and a 13,971-character write-up became
        one line — a base_rev EQUAL to the head is an ordinary accepted write,
        not a probe. The phrase is not reproduced here on purpose: it lived in
        a pen header, a test name and this page, and every copy taught it.

        So the page has to give a number the reader can compute from what they
        just read, not a property they have to be right about."""
        page = self._page()
        assert "`base_rev` *is* `current_rev`" in page, \
            "the page does not state base_rev's value, so a reader must infer it"
        assert "you know is stale" not in page, \
            "the page still asks the reader to be right about which rev is stale"

    def test_the_rule_is_about_the_body_not_the_rev(self):
        """Both destroyed docs had a placeholder body. The rev was the proximate
        cause in one case and absent in the other; the common cause was sending
        a body nobody wanted to keep. That is the sentence worth leading with,
        because it holds even when the reader gets the rev wrong."""
        page = self._page()
        assert "Never send a body you would not want to keep" in page


# ===========================================================================
# A file is in exactly one group, and a task link counts (#975)
#
# auth.md has always said "Every ticket, doc, project (and its tasks) and file
# is in exactly one group". Files were the exception: an upload with no
# group_scope stored NULL and was visible to its owner alone. On 2026-10-14
# a concept deck for a projects task was posted that way; it read 404 to every
# projects reader, including the seat the deck was for.
# ===========================================================================

class TestFileScope(_Uploads):
    def _upload(self, client, body=b"deck", name="deck.pdf",
                ctype="application/pdf", **form):
        return client.post("/api/v1/files",
                           files={"file": (name, body, ctype)},
                           data=form or None)

    def test_the_visibility_union_covers_every_link_type_it_accepts(self):
        """`POST /files/{id}/links` accepts doc, ticket and task. The read
        predicate covered the first two, so a file attached to a task — the way
        the skill documents — contributed nothing to who could read it.

        Asserted against the accepted set rather than a remembered list, so
        adding a fourth link type fails here instead of silently not counting.
        """
        import inspect
        import main
        accepted = inspect.getsource(main.add_file_link)
        union = inspect.getsource(main._can_see_link_target)
        assert "_can_see_link_target" in inspect.getsource(main._can_see_file)
        import re
        m = re.search(r'ttype not in \(([^)]*)\)', accepted)
        assert m, "could not find the accepted link types"
        types = [t.strip().strip('"\'') for t in m.group(1).split(",") if t.strip()]
        assert set(types) == {"doc", "ticket", "task"}, types
        for t in types:
            assert f'== "{t}"' in union, \
                f'link type {t!r} is accepted by add_file_link but has no branch in _can_see_link_target'

    def test_a_task_link_is_scoped_through_the_tasks_project(self):
        """There is no `_can_see_task`; a task is scoped by its project, which is
        what `_load_task_and_project` enforces. The file branch has to use the
        same predicate or a link would be more permissive than the task."""
        import inspect
        import main
        union = inspect.getsource(main._can_see_link_target)
        assert "_can_see_project" in union, \
            "the task branch does not go through the project predicate"
        assert "FROM tasks WHERE id" in union and "FROM projects WHERE id" in union, \
            "the task branch does not resolve task -> project"
        loader = inspect.getsource(main._load_task_and_project)
        assert "_can_see_project" in loader, \
            "the predicate this mirrors has moved; the two have drifted"

    def test_a_file_can_be_rescoped_without_re_uploading_it(self):
        """There was no PATCH, so a mis-scoped file was mis-scoped forever and
        the only remedy was sending the bytes again — a gigabyte to change one
        string, under the 1 GB cap."""
        import main
        assert hasattr(main, "update_file"), "no file re-scope endpoint"
        src = __import__("inspect").getsource(main.update_file)
        assert "only group_scope is writable" in src, \
            "the re-scope accepts fields that are not the group"
        assert "Only the owner or an admin" in src, \
            "the re-scope does not carry delete's owner-or-admin guard"

    # --- behavioural: the two defects actually hit ------------------------

    def test_an_unnamed_upload_lands_in_a_group(self, client):
        """The defect in one assertion. No `group_scope` on the request must not
        mean no group on the row — that is the one object on the plane that was
        in no group, and it was visible to its owner alone."""
        r = self._upload(client)
        assert r.status_code == 200, r.text
        assert r.json()["group_scope"], \
            "an unnamed upload stored no group — it is readable by its owner alone"

    def test_a_task_link_makes_the_file_readable_to_the_projects_group(
            self, client, tmp_db):
        """The case from #975, end to end: a reader who can see the task can
        read what is attached to it.

        The file is scoped to a DIFFERENT group on purpose, so its own scope
        cannot be what grants access — only the task link can. Without the task
        branch in `_can_see_file` this is a 404.
        """
        _create_group(client, "deck-group", "Deck Group")
        _create_group(client, "other-group", "Other Group")

        proj = client.post("/api/v1/projects", json={
            "name": "Example Project", "group_scope": "deck-group"})
        assert proj.status_code == 200, proj.text
        pid = proj.json()["id"]
        task = client.post(f"/api/v1/projects/{pid}/tasks", json={
            "title": "read the deck"})
        assert task.status_code == 200, task.text
        tid = task.json()["id"]

        up = self._upload(client, group_scope="other-group")
        assert up.status_code == 200, up.text
        fid = up.json()["id"]
        assert up.json()["group_scope"] == "other-group"

        link = client.post(f"/api/v1/files/{fid}/links",
                           json={"target_type": "task", "target_id": str(tid)})
        assert link.status_code == 200, link.text

        app = _get_app()
        with TestClient(app) as raw:
            token, _uid = _invite_and_activate(
                client, raw, "deck-reader", ["group:deck-group"])
            reader = AuthedClient(raw, token)
            assert reader.get(f"/api/v1/tasks/{tid}").status_code == 200, \
                "the reader cannot see the task, so this proves nothing"
            r = reader.get(f"/api/v1/files/{fid}")
            assert r.status_code == 200, (
                "a file attached to a task the reader can see is still 404 — "
                "the visibility union has no task branch")
            assert reader.get(f"/api/v1/files/{fid}/raw").status_code == 200

    def test_rescoping_moves_a_file_a_reader_could_not_see(self, client, tmp_db):
        """The remedy for the files already stored with no group: change the
        string, keep the bytes. Checked by a reader going 404 -> 200."""
        _create_group(client, "late-group", "Late Group")
        up = self._upload(client)
        fid = up.json()["id"]

        app = _get_app()
        with TestClient(app) as raw:
            token, _uid = _invite_and_activate(
                client, raw, "late-reader", ["group:late-group"])
            reader = AuthedClient(raw, token)
            assert reader.get(f"/api/v1/files/{fid}").status_code == 404

            moved = client.patch(f"/api/v1/files/{fid}",
                                 json={"group_scope": "late-group"})
            assert moved.status_code == 200, moved.text
            assert moved.json()["group_scope"] == "late-group"
            assert reader.get(f"/api/v1/files/{fid}").status_code == 200

    def test_rescoping_refuses_anything_but_the_group(self, client):
        """The name, type and sha are what was uploaded. A file needing a
        different body is a different file."""
        fid = self._upload(client).json()["id"]
        r = client.patch(f"/api/v1/files/{fid}", json={"name": "renamed.pdf"})
        assert r.status_code == 400
        assert "only group_scope is writable" in r.json()["detail"]

    def test_a_link_needs_a_target_the_linker_can_read(self, client, tmp_db):
        """A link widens who reads the file, so linking into a task the linker
        cannot see would publish into it -- and a 404-vs-200 split on the target
        would say which ids exist. The refusal is the same 404 either way."""
        _create_group(client, "closed-group", "Closed Group")
        _create_group(client, "open-group", "Open Group")
        proj = client.post("/api/v1/projects", json={
            "name": "Closed", "group_scope": "closed-group"})
        tid = client.post(f"/api/v1/projects/{proj.json()['id']}/tasks",
                          json={"title": "not yours"}).json()["id"]

        app = _get_app()
        with TestClient(app) as raw:
            token, uid = _invite_and_activate(
                client, raw, "outside-linker", ["group:open-group"])
            client.patch(f"/api/v1/groups/open-group/members/{uid}",
                         json={"role": "editor"})
            linker = AuthedClient(raw, token)
            up = linker.post("/api/v1/files",
                             files={"file": ("x.pdf", b"x", "application/pdf")},
                             data={"group_scope": "open-group"})
            assert up.status_code == 200, up.text
            fid = up.json()["id"]
            hidden = linker.post(f"/api/v1/files/{fid}/links",
                                 json={"target_type": "task", "target_id": str(tid)})
            missing = linker.post(f"/api/v1/files/{fid}/links",
                                  json={"target_type": "task", "target_id": "999999"})
            assert hidden.status_code == missing.status_code == 404, \
                (hidden.status_code, missing.status_code)


class TestGatewayForwardingScope:
    """No key opens local forwards through the gateway sshd, and user access
    keys get no forwarding at all. With `-L` an enrolled device's key reached
    every service forward on sshd's localhost and the compose network (doc 490,
    C8, reproduced in a VM)."""

    def test_sshd_allows_remote_forwards_only(self):
        conf = (Path(__file__).resolve().parent.parent / "deploy" / "sshd-entrypoint.sh").read_text()
        lines = [l.strip() for l in conf.splitlines() if l.strip().startswith("AllowTcpForwarding")]
        assert lines == ["AllowTcpForwarding remote"], lines

    def test_user_access_keys_get_no_forwarding(self, tmp_db):
        import json as _json
        import authorized_keys as ak
        db.execute(
            "INSERT INTO tokens (id, token_hash, type, owner_id, scopes) "
            "VALUES (?, ?, 'access_key', ?, ?)",
            ("tok-fwd-1", "sha256-not-a-real-hash", "someone",
             _json.dumps({"pubkey": "ssh-ed25519 AAAAC3USERKEY", "device": "dev-a"})))
        ak.sync()
        user = [l for l in ak.AUTHORIZED_KEYS_PATH.read_text().splitlines()
                if "AAAAC3USERKEY" in l]
        assert len(user) == 1, user
        assert "restrict,pty " in user[0]
        assert "port-forwarding" not in user[0]

    def test_sshd_waits_for_this_key_policy(self):
        """sshd must not start on a file in an older shape; the two numbers move
        together, so a bump also changes the entrypoint, and the release
        recreates sshd (deploy-lib maps sshd-entrypoint.sh to sshd)."""
        import re as _re
        import authorized_keys as ak
        conf = (Path(__file__).resolve().parent.parent / "deploy" / "sshd-entrypoint.sh").read_text()
        m = _re.search(r"^KEYS_POLICY=(\d+)$", conf, _re.M)
        assert m and int(m.group(1)) == ak.KEYS_POLICY, (m and m.group(1), ak.KEYS_POLICY)
        wait = conf.index("until grep -q")
        assert wait < conf.index("sync_keys &") and wait < conf.index("exec /usr/sbin/sshd")
        assert ak.POLICY_LINE == f"# samcloud-managed keys-policy={ak.KEYS_POLICY}"

    def test_an_existing_volume_is_rewritten_at_startup(self, tmp_db):
        """The upgrade path (Codex on #136): a volume holding main's lines, and no
        access key or activation to call sync(). Starting the registry must leave
        no forwarding on the user key or the tunnel-less device key, keep the
        tunnel device's own, and stamp the policy sshd waits for."""
        import json as _json
        import authorized_keys as ak
        db.execute(
            "INSERT INTO tokens (id, token_hash, type, owner_id, scopes) "
            "VALUES (?, ?, 'access_key', ?, ?)",
            ("tok-up-1", "sha256-not-a-real-hash", "someone",
             _json.dumps({"pubkey": "ssh-ed25519 AAAAC3UPUSER", "device": "dev-a"})))
        for dev, port in (("dev-tun", 10042), ("dev-direct", None)):
            db.execute(
                "INSERT INTO devices (id, name, hostname, context, ssh_pubkey, tunnel_port) "
                "VALUES (?, ?, ?, 'samcloud', ?, ?)",
                (dev, dev, dev, f"ssh-ed25519 AAAAC3UP{dev.upper().replace('-', '')}", port))
        old = [
            "ssh-ed25519 AAAAC3HANDKEY someone-by-hand",
            'command="REGISTRY_URL=x GATEWAY_KEY=y /usr/local/bin/samcloud-gateway someone dev-a",'
            "restrict,pty,port-forwarding ssh-ed25519 AAAAC3UPUSER # samcloud-managed",
            'command="/bin/false",restrict,port-forwarding,permitlisten="localhost:10042" '
            "ssh-ed25519 AAAAC3UPDEVTUN # samcloud-managed:device:dev-tun",
            'command="/bin/false",restrict,port-forwarding '
            "ssh-ed25519 AAAAC3UPDEVDIRECT # samcloud-managed:device:dev-direct",
        ]
        ak.AUTHORIZED_KEYS_PATH.write_text("\n".join(old) + "\n")
        with TestClient(_get_app()):
            pass
        lines = ak.AUTHORIZED_KEYS_PATH.read_text().splitlines()
        by_key = {k: [l for l in lines if k in l] for k in
                  ("AAAAC3HANDKEY", "AAAAC3UPUSER", "AAAAC3UPDEVTUN", "AAAAC3UPDEVDIRECT")}
        assert all(len(v) == 1 for v in by_key.values()), by_key
        assert by_key["AAAAC3HANDKEY"][0] == old[0]
        assert "port-forwarding" not in by_key["AAAAC3UPUSER"][0]
        assert "port-forwarding" not in by_key["AAAAC3UPDEVDIRECT"][0]
        assert 'port-forwarding,permitlisten="localhost:10042"' in by_key["AAAAC3UPDEVTUN"][0]
        assert lines.count(ak.POLICY_LINE) == 1



class TestAccessKeyPairLink:
    """An access route (phone, laptop) has no samcloud token, so it cannot call
    the setup-script itself. A signed-in user mints a single-use pair link and
    the device runs `curl <pair_url> | bash` (#1056). The console's old
    `/api/pair/<device>?user=` proxied the setup-script with no bearer and has
    answered 401 since the /access-keys door shut (#143)."""

    def _path(self, pair_url):
        return pair_url[pair_url.index("/api/v1/pair/"):]

    def test_minting_a_link_takes_the_access_key_rule(self, client, tmp_db):
        app = _get_app()
        with TestClient(app) as raw:
            body = {"username": "admin", "device_name": "attacker"}
            assert raw.post("/api/v1/access-keys/pair", json=body).status_code == 401
            _invite_and_activate(client, raw, "bob", [])
            alice = AuthedClient(raw, _invite_and_activate(client, raw, "alice", [])[0])
            assert alice.post("/api/v1/access-keys/pair",
                              json={"username": "bob", "device_name": "x"}).status_code == 403
            own = alice.post("/api/v1/access-keys/pair",
                             json={"username": "alice", "device_name": "alice-phone"})
            assert own.status_code == 200, own.text
            assert own.json()["key_id"] == "alice@alice-phone"
            assert own.json()["expires_in"] == 3600
        assert client.post("/api/v1/access-keys/pair",
                           json={"username": "bob", "device_name": "bob-laptop"}).status_code == 200

    def test_a_link_installs_a_key_once_with_no_bearer(self, client, tmp_db):
        r = client.post("/api/v1/access-keys/pair",
                        json={"username": "admin", "device_name": "new-laptop"})
        assert r.status_code == 200, r.text
        link = r.json()
        assert link["pair_command"] == f"curl -fsS {link['pair_url']} | bash"
        assert "/api/v1/pair/sc_pair_" in link["pair_url"]
        app = _get_app()
        with TestClient(app) as raw:
            first = raw.get(self._path(link["pair_url"]))
            assert first.status_code == 200
            assert first.headers["content-type"].startswith("text/x-shellscript")
            assert "BEGIN OPENSSH PRIVATE KEY" in first.text
            assert "Host samcloud" in first.text
            again = raw.get(self._path(link["pair_url"]))
            assert again.status_code == 410
            assert "PRIVATE KEY" not in again.text
            assert again.text.rstrip().endswith("exit 1")
        keys = client.get("/api/v1/access-keys").json()
        assert [k["key_id"] for k in keys] == ["admin@new-laptop"]

    def test_an_expired_or_unknown_link_mints_nothing(self, client, tmp_db):
        r = client.post("/api/v1/access-keys/pair",
                        json={"username": "admin", "device_name": "late"})
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        db.execute("UPDATE tokens SET expires_at = ? WHERE type = 'pair'", (past,))
        app = _get_app()
        with TestClient(app) as raw:
            assert raw.get(self._path(r.json()["pair_url"])).status_code == 410
            assert raw.get("/api/v1/pair/sc_pair_nope").status_code == 410
            assert raw.get("/api/v1/pair/sc_enroll_nope").status_code == 410
        assert client.get("/api/v1/access-keys").json() == []

    def test_a_pair_token_is_not_a_bearer(self, client, tmp_db):
        r = client.post("/api/v1/access-keys/pair",
                        json={"username": "admin", "device_name": "d"})
        token = r.json()["pair_url"].rsplit("/", 1)[1]
        app = _get_app()
        with TestClient(app) as raw:
            assert raw.get("/api/v1/auth/verify",
                           headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_the_short_code_installs_a_key_once_and_spends_the_link(self, client, tmp_db):
        """The console draws code_url as a QR, and a headset types it (#1068)."""
        r = client.post("/api/v1/access-keys/pair",
                        json={"username": "admin", "device_name": "headset"})
        link = r.json()
        code = link["code"]
        assert len(code) == 11 and code[5] == "-"
        assert link["code_url"].endswith(f"/p/{code}")
        assert link["code_command"].endswith(f"/api/v1/pair/{code} | bash")
        app = _get_app()
        with TestClient(app) as raw:
            typed = code.replace("-", "").lower()
            first = raw.get(f"/api/v1/pair/{typed}")
            assert first.status_code == 200, first.text
            assert "BEGIN OPENSSH PRIVATE KEY" in first.text
            assert raw.get(f"/api/v1/pair/{code}").status_code == 410
            assert raw.get(self._path(link["pair_url"])).status_code == 410
        keys = client.get("/api/v1/access-keys").json()
        assert [k["key_id"] for k in keys] == ["admin@headset"]

    def test_the_long_link_spends_the_short_code(self, client, tmp_db):
        link = client.post("/api/v1/access-keys/pair",
                           json={"username": "admin", "device_name": "phone"}).json()
        app = _get_app()
        with TestClient(app) as raw:
            for bearer in (link["code"], link["code"].replace("-", ""),
                           link["code"].replace("-", "").lower()):
                assert raw.get("/api/v1/auth/verify", headers={
                    "Authorization": f"Bearer {bearer}"}).status_code == 401
            assert raw.get(self._path(link["pair_url"])).status_code == 200
            assert raw.get(f"/api/v1/pair/{link['code']}").status_code == 410
            assert raw.get("/api/v1/pair/ABCDE-FGHJK").status_code == 410

    def test_minting_again_replaces_the_short_code(self, client, tmp_db):
        body = {"username": "admin", "device_name": "again"}
        old = client.post("/api/v1/access-keys/pair", json=body).json()
        new = client.post("/api/v1/access-keys/pair", json=body).json()
        app = _get_app()
        with TestClient(app) as raw:
            assert raw.get(f"/api/v1/pair/{old['code']}").status_code == 410
            assert raw.get(f"/api/v1/pair/{new['code']}").status_code == 200

    def test_a_route_enrollment_points_at_the_pair_link(self, client, tmp_db):
        r = client.post("/api/v1/enroll", json={"device_id": "alices-phone",
                                                "access_route": "ssh-tunnel"})
        assert r.status_code == 200, r.text
        assert r.json()["pair_link_url"].endswith("/api/v1/access-keys/pair")
        assert any("/api/v1/access-keys/pair" in s for s in r.json()["next_steps"])
