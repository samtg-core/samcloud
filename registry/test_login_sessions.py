"""Each login gets its own token, and leaves the user's other tokens alone (#1010, C7).

A login used to replace the user's one fixed-id token, so a dashboard login
revoked the init token an admin kept in a header file, and a second browser's
login ended the first's session.
"""
from datetime import datetime, timedelta, timezone

import db
from test_registry import client  # noqa: F401


def _login(c, user="admin", pw="test"):
    r = c.post("/api/v1/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def _verify(c, token):
    return c.get("/api/v1/auth/verify", headers={"Authorization": f"Bearer {token}"}).status_code


def test_a_login_leaves_the_init_token_and_other_logins_working(client):
    init_token = client._headers["Authorization"].split(" ", 1)[1]
    a = _login(client)
    b = _login(client)
    assert a["token"] != b["token"]
    assert _verify(client, init_token) == 200
    assert _verify(client, a["token"]) == 200 and _verify(client, b["token"]) == 200
    assert a["expires_in"] == 7 * 24 * 3600


def test_logout_ends_that_login_only(client):
    init_token = client._headers["Authorization"].split(" ", 1)[1]
    a, b = _login(client), _login(client)
    r = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {a['token']}"})
    assert r.status_code == 200, r.text
    assert _verify(client, a["token"]) == 401
    assert _verify(client, b["token"]) == 200 and _verify(client, init_token) == 200
    # A long-lived token isn't a login's, so logout won't revoke it.
    r = client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {init_token}"})
    assert r.status_code == 404 and _verify(client, init_token) == 200


def test_a_login_token_expires_and_expired_ones_are_cleared(client):
    a = _login(client)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.execute("UPDATE tokens SET expires_at = ? WHERE id LIKE 'session:%'", (past,))
    assert _verify(client, a["token"]) == 401
    _login(client)
    left = db.query("SELECT expires_at FROM tokens WHERE id LIKE 'session:%'")
    assert len(left) == 1 and left[0]["expires_at"] > past


def _second_admin(c):
    r = c.post("/api/v1/users", json={"username": "ops", "password": "pw", "role": "admin", "scopes": ["*"]})
    assert r.status_code == 200, r.text
    return r.json()["user_id"], _login(c, "ops", "pw")["token"]


def _as(c, token, path):
    return c.get(path, headers={"Authorization": f"Bearer {token}"})


def test_rotating_a_users_token_ends_their_logins(client):
    """A login keeps the scopes it was made with, so a rotation that left it
    alive left a demoted admin with '*' (codex, #1010 review of #133)."""
    init_token = client._headers["Authorization"].split(" ", 1)[1]
    uid, ops_login = _second_admin(client)
    r = client.patch(f"/api/v1/users/{uid}", json={"role": "user", "scopes": [], "rotate_token": True})
    assert r.status_code == 200, r.text
    assert _verify(client, ops_login) == 401
    assert _as(client, r.json()["token"], "/api/v1/auth/verify").json()["scopes"] == []
    assert _verify(client, init_token) == 200          # the admin doing it keeps theirs


def test_regenerating_a_users_token_ends_their_logins(client):
    uid, ops_login = _second_admin(client)
    r = client.post(f"/api/v1/users/{uid}/regenerate-token")
    assert r.status_code == 200, r.text
    assert _verify(client, ops_login) == 401
    assert _verify(client, r.json()["token"]) == 200


def test_an_in_place_scope_edit_narrows_open_logins(client):
    uid, ops_login = _second_admin(client)
    r = client.patch(f"/api/v1/users/{uid}", json={"role": "user", "scopes": []})
    assert r.status_code == 200 and r.json()["token_rotated"] is False, r.text
    v = _as(client, ops_login, "/api/v1/auth/verify")
    assert v.status_code == 200 and v.json()["scopes"] == []
