"""Focused regression test for the /access-keys authentication fix (incident #143).

Runs against an isolated sandbox registry (never touches prod). Proves:
  1. Anonymous POST /access-keys            -> 401 (was: 200, minted an admin key)
  2. Anonymous POST /access-keys/setup-script -> 401
  3. Non-admin minting a key for SOMEONE ELSE -> 403
  4. Non-admin minting their OWN key          -> 200 (legit self-serve preserved)
  5. Admin minting for any user               -> 200 (legit provisioning preserved)

Usage: python sandbox/test_access_key_auth.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox  # noqa: E402


def _make_user(sb, username):
    """Invite + activate a non-admin user, return their token."""
    inv = sb.api("POST", "/api/v1/users/invite",
                 {"username": username, "scopes": [], "role": "user"})
    assert inv["status"] in (200, 201), f"invite failed: {inv}"
    invite_token = inv["data"].get("token")
    act = sb.api("POST", "/api/v1/users/activate",
                 {"token": invite_token, "password": "pw-" + username},
                 _skip_auth=True)
    assert act["status"] in (200, 201), f"activate failed: {act}"
    return act["data"].get("token", "")


def run():
    sb = Sandbox()
    passed = failed = 0

    def check(desc, cond, detail=""):
        nonlocal passed, failed
        ok = bool(cond)
        passed += ok
        failed += (not ok)
        print(f"  {'✓' if ok else '✗'} {desc}" + (f"  [{detail}]" if detail and not ok else ""))

    try:
        sb.start(auto_auth=True)  # admin user 'admin', token stored

        # --- 1 & 2: anonymous is rejected ---
        r = sb.api("POST", "/api/v1/access-keys",
                   {"username": "admin", "device_name": "attacker"}, _skip_auth=True)
        check("anonymous POST /access-keys -> 401", r["status"] == 401, f"got {r['status']}")

        r = sb.api("POST", "/api/v1/access-keys/setup-script",
                   {"username": "admin", "device_name": "attacker"}, _skip_auth=True)
        check("anonymous POST /access-keys/setup-script -> 401", r["status"] == 401, f"got {r['status']}")

        # --- set up two non-admin users ---
        alice_tok = _make_user(sb, "alice")
        _make_user(sb, "bob")

        # --- 3: alice may NOT mint bob's key ---
        r = sb.api("POST", "/api/v1/access-keys",
                   {"username": "bob", "device_name": "alicebox"},
                   headers={"Authorization": f"Bearer {alice_tok}"}, _skip_auth=True)
        check("non-admin minting ANOTHER user's key -> 403", r["status"] == 403, f"got {r['status']}")

        # --- 4: alice MAY mint her own key ---
        r = sb.api("POST", "/api/v1/access-keys",
                   {"username": "alice", "device_name": "alicebox"},
                   headers={"Authorization": f"Bearer {alice_tok}"}, _skip_auth=True)
        check("non-admin minting OWN key -> 200 + private_key",
              r["status"] == 200 and bool(r["data"].get("private_key")), f"got {r['status']}")

        # --- 5: admin may mint for anyone ---
        r = sb.api("POST", "/api/v1/access-keys",
                   {"username": "bob", "device_name": "adminprovision"})  # admin token auto-injected
        check("admin minting for another user -> 200 + private_key",
              r["status"] == 200 and bool(r["data"].get("private_key")), f"got {r['status']}")

        # --- LIST: anonymous rejected; non-admin scoped to self; admin sees all ---
        r = sb.api("GET", "/api/v1/access-keys", _skip_auth=True)
        check("anonymous GET /access-keys -> 401", r["status"] == 401, f"got {r['status']}")

        r = sb.api("GET", "/api/v1/access-keys?username=bob",
                   headers={"Authorization": f"Bearer {alice_tok}"}, _skip_auth=True)
        check("non-admin listing ANOTHER user's keys -> 403", r["status"] == 403, f"got {r['status']}")

        r = sb.api("GET", "/api/v1/access-keys",
                   headers={"Authorization": f"Bearer {alice_tok}"}, _skip_auth=True)
        only_alice = r["status"] == 200 and all(
            k.get("key_id", "").startswith("alice@") for k in (r["data"] if isinstance(r["data"], list) else r["data"].get("keys", [])))
        check("non-admin bare list -> 200, own keys only", only_alice, f"got {r['status']} {r['data']}")

        r = sb.api("GET", "/api/v1/access-keys")  # admin
        check("admin list -> 200", r["status"] == 200, f"got {r['status']}")

        # --- REVOKE: non-admin cannot revoke another's key; can revoke own ---
        r = sb.api("DELETE", "/api/v1/access-keys/bob@adminprovision",
                   headers={"Authorization": f"Bearer {alice_tok}"}, _skip_auth=True)
        check("non-admin revoking ANOTHER user's key -> 403", r["status"] == 403, f"got {r['status']}")

        r = sb.api("DELETE", "/api/v1/access-keys/bob@adminprovision", _skip_auth=True)
        check("anonymous revoke -> 401", r["status"] == 401, f"got {r['status']}")

        r = sb.api("DELETE", "/api/v1/access-keys/alice@alicebox",
                   headers={"Authorization": f"Bearer {alice_tok}"}, _skip_auth=True)
        check("non-admin revoking OWN key -> 200", r["status"] == 200, f"got {r['status']}")

        print(f"\n  {passed} passed, {failed} failed")
        return failed == 0
    finally:
        sb.stop()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
