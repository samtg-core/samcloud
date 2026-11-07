"""Scope v1 — the group is the scope (doc 261).

One predicate for the work plane: every ticket, doc, project (and its tasks) and
file is in exactly one group; what an identity can see is the union of its
groups; what it can do in a group is its role there — viewer reads and answers,
editor creates and changes, owner manages people. The plane's admin is owner of
every group. A device or service token belongs to the groups that hold its
device, as an editor of them. Delivery follows the object: a notification row
is written only to an identity that can read the object it is about.

`main.py` keeps the routes; this module answers "may this identity do this to
an object in this group" and nothing else, so reads, lists, writes, thread
fan-out and the gateway cannot drift from each other (doc 262 counted four
answers to "who may change this" before it existed).
"""
import json
from typing import Optional

import db

ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}
ROLE_ALIASES = {"admin": "owner"}           # schema.sql once said admin/editor/viewer
DEFAULT_GROUP = "core"                        # the inner city: where an orphan — a creator with no group at all — lands, seen by few
ADMIN_GROUP = "samcloud-contributors"         # the plane's own work — its records, boards, briefs; where the admin's unnamed writes go
KINDS = {"ticket": "tickets", "doc": "docs", "project": "projects", "file": "files"}


def normalise_role(role: Optional[str]) -> str:
    r = (role or "viewer").strip().lower()
    r = ROLE_ALIASES.get(r, r)
    if r not in ROLE_RANK:
        raise ValueError(f"Unknown role '{role}': one of {', '.join(ROLE_RANK)}")
    return r


def rank(role: Optional[str]) -> int:
    return ROLE_RANK.get(ROLE_ALIASES.get(role or "", role or ""), -1)


# --- memberships ------------------------------------------------------------

def _user_id(owner_id: str) -> Optional[int]:
    if owner_id and owner_id.startswith("user:"):
        try:
            return int(owner_id.split(":", 1)[1])
        except ValueError:
            return None
    return None


def memberships_of_user(user_id: int) -> dict:
    """group id → role, in the order the memberships were made (the first is
    the user's default group)."""
    out = {}
    for r in db.query("SELECT group_id, role FROM group_members WHERE user_id = ? ORDER BY rowid",
                      (user_id,)):
        try:
            out[r["group_id"]] = normalise_role(r["role"])
        except ValueError:
            out[r["group_id"]] = "viewer"
    return out


def _group_exists(group_id: str) -> bool:
    """A `group:<g>` scope naming no group is not a membership."""
    return bool(group_id) and db.query_one("SELECT 1 FROM groups WHERE id = ?", (group_id,)) is not None


def _device_groups(device_id: str) -> list:
    return [r["group_id"] for r in db.query(
        "SELECT group_id FROM group_devices WHERE device_id = ? ORDER BY rowid", (device_id,))]


def memberships(auth) -> Optional[dict]:
    """group id → role for a bearer; None for the admin (owner of every group).

    A user: its membership rows, plus `group:<g>` scopes on the token that have
    no row (a legacy invite) as viewer. A device or service token: the groups
    that hold its device, as editor. `device:`/`services:` scopes name no group."""
    if auth.is_admin():
        return None
    out = {}
    uid = _user_id(auth.owner_id)
    if (uid is None and auth.token_type == "agent" and auth.owner_id
            and "/" not in auth.owner_id and ":" not in auth.owner_id):
        # a legacy agent token carries the bare username as its owner (#167)
        row = db.query_one("SELECT id FROM users WHERE username = ?", (auth.owner_id,))
        uid = row["id"] if row else None
    if uid is not None:
        out.update(memberships_of_user(uid))
    else:
        device_id = (auth.owner_id or "").split("/", 1)[0]
        for g in _device_groups(device_id):
            out.setdefault(g, "editor")
    for s in auth.scopes:
        if s.startswith("group:") and _group_exists(s.split(":", 1)[1]):
            out.setdefault(s.split(":", 1)[1], "viewer")
    return out


def role_in(auth, group_id: Optional[str]) -> Optional[str]:
    if not group_id:
        return "owner" if auth.is_admin() else None
    m = memberships(auth)
    if m is None:
        return "owner"
    return m.get(group_id)


def can_read(auth, group_id: Optional[str]) -> bool:
    return role_in(auth, group_id) is not None


def can_edit(auth, group_id: Optional[str]) -> bool:
    return rank(role_in(auth, group_id)) >= ROLE_RANK["editor"]


def can_manage(auth, group_id: Optional[str]) -> bool:
    return rank(role_in(auth, group_id)) >= ROLE_RANK["owner"]


def readable_groups(auth) -> Optional[set]:
    """The groups a bearer can read, or None for all (admin)."""
    m = memberships(auth)
    return None if m is None else set(m)


def default_group(auth, assignee: Optional[str] = None) -> Optional[str]:
    """Where a create that names no group goes (doc 261, decision 5): the first
    group the writer can edit that the assignee (if any) is in, else the first
    group the writer can edit; the admin's, and a machine's with no group, is
    `core`. None when the writer edits nothing."""
    m = memberships(auth)
    if m is None:
        admin_home = ADMIN_GROUP if _group_exists(ADMIN_GROUP) else DEFAULT_GROUP
        return admin_home if not assignee else (
            next(iter(memberships_of_username(assignee) or {}), None) or admin_home)
    editable = [g for g, r in m.items() if rank(r) >= ROLE_RANK["editor"]]
    if not editable:
        return None
    if assignee:
        theirs = memberships_of_username(assignee) or {}
        for g in editable:
            if g in theirs:
                return g
    return editable[0]


# --- by username (fan-out holds names, not bearers) ---------------------------

def memberships_of_username(username: str) -> Optional[dict]:
    """group id → role for a username; None for an admin; {} for an unknown name
    or a machine identity (`<device>/<service>` → the groups holding its device)."""
    row = db.query_one("SELECT id, role, scopes FROM users WHERE username = ?", (username,))
    if not row:
        if "/" in (username or ""):
            return {g: "editor" for g in _device_groups(username.split("/", 1)[0])}
        return {}
    if (row.get("role") or "") == "admin":
        return None
    try:
        scopes = json.loads(row.get("scopes") or "[]")
        if isinstance(scopes, dict):
            scopes = list(scopes.keys())
    except (TypeError, ValueError):
        scopes = []
    if "*" in scopes or "admin" in scopes:
        return None
    out = memberships_of_user(row["id"])
    for s in scopes:
        if isinstance(s, str) and s.startswith("group:") and _group_exists(s.split(":", 1)[1]):
            out.setdefault(s.split(":", 1)[1], "viewer")
    return out


def user_role_in(username: str, group_id: Optional[str]) -> Optional[str]:
    m = memberships_of_username(username)
    if m is None:
        return "owner"
    if not group_id:
        return None
    return m.get(group_id)


def user_can_read(username: str, group_id: Optional[str]) -> bool:
    return user_role_in(username, group_id) is not None


# --- objects ------------------------------------------------------------------

def object_readable(auth, row: dict, owner_cols=("source", "owner")) -> bool:
    """Admin sees all; the object's author always sees it; otherwise the
    caller must be a member of the object's group. An object with no group is
    the admin's until the backfill reaches it — nothing unscoped is shared."""
    if auth.is_admin():
        return True
    for c in owner_cols:
        v = row.get(c)
        if v and v in (auth.owner_id, auth.identity()):
            return True
    return can_read(auth, row.get("group_scope"))


def identity_object_readable(username: str, row: dict, owner_cols=("source", "owner")) -> bool:
    """The same rule for a username — what fan-out uses before writing a row."""
    for c in owner_cols:
        if row.get(c) and row.get(c) == username:
            return True
    return user_can_read(username, row.get("group_scope"))


def object_editable(auth, row: dict, owner_cols=("source", "owner")) -> bool:
    """Admin, the author, or an editor of the object's group."""
    if auth.is_admin():
        return True
    for c in owner_cols:
        v = row.get(c)
        if v and v in (auth.owner_id, auth.identity()):
            return True
    return can_edit(auth, row.get("group_scope"))


def note_for_outsider(username: str, group_id: Optional[str]) -> str:
    """The system note (doc 261, decision 3) when someone is addressed on an
    object of a group they are not in — the one place a group is named to an
    outsider."""
    return (f"{username} is not in group '{group_id}' and was not notified; "
            f"an owner of the group can add them: POST /api/v1/groups/{group_id}/members")


def outsiders(usernames, group_id: Optional[str]) -> list:
    """Of these usernames, the ones that cannot read the group."""
    return [u for u in usernames if not user_can_read(u, group_id)]
