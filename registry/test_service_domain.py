# Clean-root deployment round-trip check; test behaviour is unchanged.
"""The service domain comes from the instance's config, not a hard-coded default.

A route or service that names no domain is routed under SAMCLOUD_DOMAIN, or
else PUBLIC_URL's host itself; no parent domain is guessed (codex on #1010).
An instance that routes under a parent domain sets SAMCLOUD_DOMAIN in its env files. Ticket 1010,
doc 490 step 1.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("public_url, explicit, expected", [
    ("https://cloud.example.org", "example.org", "example.org"),   # a parent domain, from the env file
    ("https://stg.example.org:9443", "example.org", "example.org"),
    ("https://cloud.example.org", None, "cloud.example.org"),  # no parent is invented
    ("https://box.home.arpa", None, "box.home.arpa"),
    ("https://example.org", None, "example.org"),
    ("http://localhost:7778", None, "localhost"),
    ("http://192.0.2.20:7778", None, "192.0.2.20"),
    ("", None, "localhost"),
    ("https://cloud.example.org", "other.net", "other.net"),
])
def test_service_domain(monkeypatch, public_url, explicit, expected):
    import main
    monkeypatch.setenv("PUBLIC_URL", public_url)
    if explicit is None:
        monkeypatch.delenv("SAMCLOUD_DOMAIN", raising=False)
    else:
        monkeypatch.setenv("SAMCLOUD_DOMAIN", explicit)
    assert main._service_domain() == expected


def test_tunnel_host_falls_back_to_public_url(monkeypatch):
    import main
    monkeypatch.delenv("PLANE_HOST", raising=False)
    monkeypatch.delenv("SLICE_HOST", raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://cloud.example.org")
    assert main._tunnel_host() == "cloud.example.org"
    monkeypatch.setenv("PLANE_HOST", "tunnel.example.org")
    assert main._tunnel_host() == "tunnel.example.org"


def test_the_old_slice_names_still_work_and_plane_names_win(monkeypatch):
    """PLANE_* replaced SLICE_* (#1010); an env file with the old names keeps working."""
    import main
    for n in ("PLANE_HOST", "PLANE_SSH_PORT", "PLANE_SSH_USER"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("SLICE_HOST", "old.example.org")
    monkeypatch.setenv("SLICE_SSH_PORT", "2201")
    monkeypatch.setenv("SLICE_SSH_USER", "olduser")
    assert (main._tunnel_host(), main._plane_ssh_port(), main._plane_ssh_user()) == ("old.example.org", "2201", "olduser")
    monkeypatch.setenv("PLANE_HOST", "new.example.org")
    monkeypatch.setenv("PLANE_SSH_PORT", "2202")
    monkeypatch.setenv("PLANE_SSH_USER", "newuser")
    assert (main._tunnel_host(), main._plane_ssh_port(), main._plane_ssh_user()) == ("new.example.org", "2202", "newuser")
    monkeypatch.setenv("PLANE_SSH_USER", "")          # compose passes an empty PLANE_SSH_USER
    assert main._plane_ssh_user() == "olduser"


def test_route_without_domain_uses_instance_domain(tmp_db, monkeypatch):
    import main
    monkeypatch.setenv("PUBLIC_URL", "https://cloud.example.org")
    monkeypatch.delenv("SAMCLOUD_DOMAIN", raising=False)
    with TestClient(main.app) as c:
        token = c.post("/api/v1/init", json={"username": "admin", "password": "test"}).json()["token"]
        r = c.post("/api/v1/routes", json={"subdomain": "a", "target": "localhost:1"},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["route"] == "a.cloud.example.org"


# --- dns-namecheap is an optional module (doc 490 step 2) -------------------

def _clear_dns_env(monkeypatch, tmp_path):
    for v in ("SAMCLOUD_MODULES", "NAMECHEAP_API_KEY", "NAMECHEAP_API_USER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))   # no ~/.config/samcloud/namecheap.env


@pytest.mark.parametrize("modules, creds, expected", [
    (None, True, True),            # credentials, no switch
    (None, False, False),          # a fresh instance
    ("", True, True),              # compose passes an empty value: same as unset
    ("dns-namecheap", False, True),
    ("none", True, False),
    ("brokers", True, False),
])
def test_dns_module_switch(monkeypatch, tmp_path, modules, creds, expected):
    import main
    _clear_dns_env(monkeypatch, tmp_path)
    if modules is not None:
        monkeypatch.setenv("SAMCLOUD_MODULES", modules)
    if creds:
        monkeypatch.setenv("NAMECHEAP_API_KEY", "k")
        monkeypatch.setenv("NAMECHEAP_API_USER", "u")
    assert main._dns_module_on() is expected


def test_dns_off_is_skipped_not_failed(monkeypatch, tmp_path):
    import main
    _clear_dns_env(monkeypatch, tmp_path)
    out = main._dns_record("add", "a", "example.org")
    assert out["skipped"] is True and out["success"] is None
    assert main.api_index()["modules"] == {"dns-namecheap": False}
