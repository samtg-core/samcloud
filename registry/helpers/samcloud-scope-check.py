#!/usr/bin/env python3
# SCOPE_CHECK_VERSION: 1.1.0
# samcloud-scope-check — confirm your scopes: expected against actual (doc 263).
#
# Run on a seat with its own header file; it reads what the plane says you
# are (GET /groups — your groups and your role in each) and probes what the
# plane actually does for you, row by row of the stage board's S-rows that a
# seat can run on itself without side effects:
#
#   - every ticket, doc, project, task and file you can list is in a group you
#     are in, or is your own;
#   - every device and service you can list is on a device of a group you are in;
#   - a create named into a group where you are a viewer is refused (403) and
#     creates nothing; a create named into a group you are not in is refused;
#   - a group you are not in answers as if it did not exist;
#   - the API map answers, and the release you are talking to is named.
#
# Prints one line per probe — EXPECTED / ACTUAL / ok or DIFF — and a summary;
# exits 1 if any row differs. It writes nothing that succeeds: the only writes
# it attempts are the ones the plane must refuse.
#
#   curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/scope-check | python3 -
#
# Env: SAMCLOUD_API (default __API__), SAMCLOUD_AUTH_HDR (default ~/.samcloud/__TOKEN_HDR__).
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("SAMCLOUD_API", "__API__").rstrip("/")
HDR = os.path.expanduser(os.environ.get("SAMCLOUD_AUTH_HDR", "~/.samcloud/__TOKEN_HDR__"))


def _auth() -> str:
    try:
        line = open(HDR).read().strip()
    except OSError:
        sys.exit(f"scope-check: no header file at {HDR} (SAMCLOUD_AUTH_HDR)")
    return line.split(":", 1)[1].strip() if ":" in line else line


AUTH = _auth()


SESSION = (os.environ.get("SAMCLOUD_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()[:64]


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": AUTH, "Content-Type": "application/json"}
    if SESSION:
        headers["X-Session-Id"] = SESSION      # every write carries its conversation, refusals included
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except urllib.error.URLError as e:
        sys.exit(f"scope-check: {API} unreachable: {e}")


rows = []


def probe(name: str, expected, actual, detail: str = ""):
    ok = expected == actual
    rows.append((name, expected, actual, ok, detail))
    mark = "ok  " if ok else "DIFF"
    print(f"  {mark}  {name:<58} expected {expected!s:<22} actual {actual!s}" + (f"   {detail}" if detail and not ok else ""))


def listed(st, d, key, what: str):
    """A list that failed is a DIFF, never an empty collection — 1.1.0 (doc 264:
    the 1.0.0 checker read 503s as zero strays and passed)."""
    if st != 200 or d is None:
        probe(f"{what} answers 200 with a list", 200, st, str(d)[:100])
        return None
    out = d.get(key) if isinstance(d, dict) else d
    if not isinstance(out, list):
        probe(f"{what} answers a list", "list", type(out).__name__)
        return None
    return out


def items(d, key):
    if isinstance(d, dict):
        return d.get(key) or d.get("items") or []
    return d or []


# --- who am I -----------------------------------------------------------------
st, me = call("GET", "/auth/verify")
if st != 200:
    sys.exit(f"scope-check: /auth/verify answered {st}: {me}")
ident = me.get("username") or me.get("identity") or me.get("owner_id")
scopes = me.get("scopes") or []
admin = "*" in scopes or "admin" in scopes
st, groups = call("GET", "/groups")
groups = items(groups, "groups")
mine = {}
for g in groups:
    st, gd = call("GET", f"/groups/{g['id']}")
    if st != 200:
        continue
    role = None
    for m in gd.get("members", []):
        if m.get("username") == ident:
            role = m.get("group_role") or m.get("role")
    mine[g["id"]] = {"role": role or ("owner" if admin else "viewer (by scope)"),
                     "devices": {d["id"] for d in gd.get("devices", [])},
                     "services": {s["id"] for s in gd.get("services", [])}}

print(f"scope-check 1.1.0 · {ident} · {API}")
print(f"  groups: " + (", ".join(f"{g}:{v['role']}" for g, v in mine.items()) or "(none)") + ("  · admin" if admin else ""))
if admin:
    print("  (an admin is owner of every group — the refusal probes do not apply)")
print()

my_groups = set(mine)
editable = {g for g, v in mine.items() if v["role"] in ("editor", "owner")}
viewer_only = {g for g, v in mine.items() if v["role"] not in ("editor", "owner")}

# --- S1/S10/S11: what you can list is in your groups, or your own --------------
visible_projects = set()
for kind, path, key, col in (("tickets", "/tickets?limit=500", "tickets", "source"),
                             ("docs", "/docs?limit=200", "docs", "source"),
                             ("projects", "/projects?limit=500", "projects", "source"),
                             ("tasks", "/tasks?limit=500", "tasks", "source"),
                             ("files", "/files?limit=200", "files", "owner")):
    st, d = call("GET", path)
    rows_ = listed(st, d, key, f"GET {path.split('?')[0]}")
    if rows_ is None:
        continue
    if kind == "projects":
        visible_projects = {p.get("id") for p in rows_}
    if kind == "tasks":
        # a task rides its project: every listed task's project must be one I can list
        strays = [t.get("id") for t in rows_ if not admin and t.get("project_id") not in visible_projects]
    else:
        strays = [x.get("id") for x in rows_
                  if not admin and x.get("group_scope") not in my_groups and x.get(col) != ident]
    probe(f"S1 every listed {kind[:-1]} is in a group of mine or my own ({len(rows_)} listed)",
          0, len(strays), f"strays: {strays[:8]}")
    if kind == "tickets" and my_groups:
        # S1b the focus filter is exact (1.1.0): every ticket answered for ?group_scope=<g> is labelled g
        g0 = sorted(my_groups)[0]
        st2, d2 = call("GET", f"/tickets?group_scope={g0}&limit=500")
        f_rows = listed(st2, d2, "tickets", f"GET /tickets?group_scope={g0}")
        if f_rows is not None:
            off = [t.get("id") for t in f_rows if t.get("group_scope") != g0]
            probe(f"S1b ?group_scope={g0} answers only {g0} tickets ({len(f_rows)} listed)", 0, len(off), f"others: {off[:8]}")

# --- S13/S15: devices and services ---------------------------------------------
st, devs = call("GET", "/devices")
devs = listed(st, devs, "devices", "GET /devices") or []
in_scope = set().union(*[v["devices"] for v in mine.values()]) if mine else set()
direct = {s.split(":", 1)[1] for s in scopes if s.startswith("device:")}
stray_devs = [d["id"] for d in devs if not admin and d["id"] not in in_scope and d["id"] not in direct]
probe(f"S13 every listed device is in a group of mine or my device: scope ({len(devs)} listed)",
      0, len(stray_devs), f"strays: {stray_devs[:8]}")
st, svcs = call("GET", "/services")
svcs = listed(st, svcs, "services", "GET /services") or []
stray_svcs = [s["id"] for s in svcs if not admin and (s.get("device_id") not in in_scope and s.get("device_id") not in direct)]
probe(f"S15 every listed service is on a device I can see ({len(svcs)} listed)",
      0, len(stray_svcs), f"strays: {stray_svcs[:8]}")

# --- S4/S5: a create where I am a viewer, or a stranger, is refused ------------
if not admin:
    if viewer_only:
        g = sorted(viewer_only)[0]
        st, d = call("POST", "/tickets", {"summary": "scope-check probe (must be refused)", "group_scope": g})
        probe(f"S4 filing a ticket into '{g}' where I am a viewer is refused", 403, st, str(d)[:120])
        if st in (200, 201):
            print(f"        !! a ticket was created in {g}: {d} — tell the admin")
    else:
        print("  skip  S4 (no group where I am only a viewer)")
    st, allg = call("GET", "/groups")
    known = {g["id"] for g in items(allg, "groups")}
    outsider = next((g for g in ("core", "samcloud-contributors", "services", "work", "containers", "personal", "projects")
                     if g not in my_groups), None)
    if outsider:
        st, d = call("POST", "/tickets", {"summary": "scope-check probe (must be refused)", "group_scope": outsider})
        probe(f"S5 filing a ticket into '{outsider}' where I am not a member is refused", True, st in (400, 403, 404), f"{st} {str(d)[:100]}")
        st, d = call("GET", f"/groups/{outsider}")
        probe(f"S17 a group I am not in ('{outsider}') is refused or hidden", True, st in (403, 404), f"{st}")
    else:
        print("  skip  S5/S17 (I am in every well-known group)")
    st, d = call("POST", "/tickets", {"summary": "scope-check probe (must be refused)", "group_scope": ""})
    probe("S5 a create that clears its group is refused (422)", 422, st)
else:
    print("  skip  S4/S5/S17 (admin)")

# --- S20: the roster ------------------------------------------------------------
st, us = call("GET", "/users")
us = items(us, "users")
probe("S20 GET /users answers a non-admin with itself", True, admin or [u.get("username") for u in us] == [ident], str([u.get("username") for u in us])[:80])

# --- the release ------------------------------------------------------------------
st, idx = call("GET", "/")
release = (idx or {}).get("release") or (idx or {}).get("version")
probe("the API map answers and names the release", True, st == 200 and bool(release), f"{st} {release}")

# --- summary ----------------------------------------------------------------------
diffs = [r for r in rows if not r[3]]
print()
print(f"scope-check: {len(rows) - len(diffs)} ok, {len(diffs)} differ" + (" — paste this output on your feedback doc; the admin reads the DIFF rows" if diffs else ""))
sys.exit(1 if diffs else 0)
