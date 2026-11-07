#!/usr/bin/env python3
"""Saved views (#203 v2, doc #34) — filters over data, zero hardcoded sections.

Contract under test:
  1. First GET /views seeds the four default sections AS ROWS (editable data).
  2. CRUD with owner isolation: another user neither sees nor edits your views.
  3. Resolution: :me token, tag filters, status lists — through scope predicates.
  4. Grouping: group_by + limit_per_group reproduce the pick-me-menu semantics.
  5. Validation 400s loudly: bad entity, unknown filter key, bad sort/group.

Run: python3 sandbox/test_views.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness import Sandbox  # noqa: E402

PASS, FAIL = 0, 0


def check(desc, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"      \033[0;32m✓\033[0m {desc}")
    else:
        FAIL += 1
        print(f"      \033[0;31m✗\033[0m {desc}  {detail}")


def mk_user(sb, username, scopes):
    # Scope v1 (doc 261): a seat that works is an editor of its groups; one
    # with no group at all belongs to `samcloud-contributors`, the plane's own work.
    made = sb.api("POST", "/api/v1/users",
                  {"username": username, "password": "pw", "role": "user", "scopes": scopes,
                   "group_roles": {sc.split(":", 1)[1]: "editor" for sc in scopes if sc.startswith("group:")}})
    uid = (made.get("data") or {}).get("user_id")
    if uid and not any(sc.startswith("group:") or sc in ("*", "admin") for sc in scopes):
        sb.api("POST", "/api/v1/groups/samcloud-contributors/members", {"user_id": uid, "role": "editor"})
    r = sb.api("POST", "/api/v1/auth/login",
               {"username": username, "password": "pw"}, _skip_auth=True)
    return r["data"].get("token", "")


def as_user(tok):
    return {"Authorization": f"Bearer {tok}"}


def main():
    sb = Sandbox()
    sb.start()
    try:
        print("  [1] seed-on-first-list")
        u = mk_user(sb, "viewer", ["*"])
        r = sb.api("GET", "/api/v1/views", headers=as_user(u))
        names = [v["name"] for v in r["data"]["views"]]
        check("four seed views created as rows",
              names == ["Blocked on you", "Decisions", "Pick-me menu", "Unread"], names)
        check("seeds carry parsed filters",
              r["data"]["views"][0]["filters"].get("assignee") == ":me",
              r["data"]["views"][0])
        menu_view = next(v for v in r["data"]["views"] if v["name"] == "Pick-me menu")
        unread_view = next(v for v in r["data"]["views"] if v["name"] == "Unread")
        blocked_view = next(v for v in r["data"]["views"] if v["name"] == "Blocked on you")

        print("  [2] owner isolation + CRUD")
        sb.api("POST", "/api/v1/groups", {"id": "home", "name": "Home"})   # scope v1: a group must exist to be edited in
        other = mk_user(sb, "otherguy", ["group:home"])  # NOT admin — "*" would be
        r2 = sb.api("GET", "/api/v1/views", headers=as_user(other))
        check("other user gets their OWN seeds, not viewer's",
              all(v["owner"] == "otherguy" for v in r2["data"]["views"]))
        e = sb.api("PATCH", f"/api/v1/views/{menu_view['id']}", {"name": "steal"},
                   headers=as_user(other))
        check("cannot edit someone else's view", e["status"] == 403, e["status"])
        c = sb.api("POST", "/api/v1/views",
                   {"name": "hot tickets", "entity": "tickets",
                    "filters": {"status": ["open"], "priority": ["high", "critical"]},
                    "sort": "priority"}, headers=as_user(u))
        check("create custom view", c["status"] == 200 and c["data"].get("view_id"),
              c["data"])
        d = sb.api("DELETE", f"/api/v1/views/{c['data']['view_id']}", headers=as_user(u))
        check("delete own view", d["status"] == 200)

        print("  [3] resolution through data")
        sb.api("POST", "/api/v1/tickets",
               {"type": "question", "priority": "high", "summary": "for viewer",
                "assignee": "viewer"}, headers=as_user(other))
        rr = sb.api("GET", f"/api/v1/views/{blocked_view['id']}/results", headers=as_user(u))
        check(":me resolves to the caller",
              rr["data"]["total"] >= 1 and
              all(t["assignee"] == "viewer" for t in rr["data"]["items"]),
              rr["data"])
        ur = sb.api("GET", f"/api/v1/views/{unread_view['id']}/results", headers=as_user(u))
        check("unread notifications view returns the assignment row",
              ur["data"]["total"] >= 1, ur["data"])

        print("  [4] grouping = pick-me-menu semantics")
        for slug, titles in (("proj-a", ["small a", "big a"]), ("proj-b", ["only b"])):
            sb.api("POST", "/api/v1/projects", {"slug": slug, "name": slug},
                   headers=as_user(u))
            for i, t in enumerate(titles):
                sb.api("POST", f"/api/v1/projects/{slug}/tasks",
                       {"title": t, "estimate_min": (i + 1) * 30}, headers=as_user(u))
        mr = sb.api("GET", f"/api/v1/views/{menu_view['id']}/results", headers=as_user(u))
        groups = {g["key"]: g["items"] for g in mr["data"].get("groups", [])}
        check("grouped by project, 1 per group",
              set(groups) >= {"proj-a", "proj-b"} and
              all(len(v) == 1 for v in groups.values()), mr["data"])
        check("smallest estimate wins within group",
              groups.get("proj-a") and groups["proj-a"][0]["title"] == "small a",
              groups.get("proj-a"))

        print("  [4b] docs entity — filters, sort, comment preview")
        d1 = sb.api("POST", "/api/v1/docs",
                    {"title": "runbook one", "body": "how to reboot", "kind": "runbook"},
                    headers=as_user(u))
        doc_id = d1["data"]["doc_id"]
        sb.api("POST", "/api/v1/docs",
              {"title": "an rfc", "body": "proposal text", "kind": "rfc"},
              headers=as_user(u))
        sb.api("POST", f"/api/v1/docs/{doc_id}/comments",
              {"body": "the triggering comment"}, headers=as_user(u))
        dv = sb.api("POST", "/api/v1/views",
                    {"name": "runbooks", "entity": "docs",
                     "filters": {"kind": ["runbook"], "assignee": ":me"},
                     "sort": "updated_at:desc"}, headers=as_user(u))
        check("create docs view", dv["status"] == 200 and dv["data"].get("view_id"),
              dv["data"])
        dr = sb.api("GET", f"/api/v1/views/{dv['data']['view_id']}/results",
                    headers=as_user(u))
        check("docs view filters by kind + assignee(:me)->source",
              dr["data"]["total"] == 1 and dr["data"]["items"][0]["id"] == doc_id,
              dr["data"])
        check("docs view row carries the latest comment as preview",
              dr["data"]["items"][0].get("latest_comment") == "the triggering comment",
              dr["data"]["items"][0])

        print("  [5] validation 400s")
        for bad in ({"name": "x", "entity": "nope"},
                    {"name": "x", "entity": "tasks", "filters": {"vibe": "good"}},
                    {"name": "x", "entity": "tasks", "sort": "sneaky;drop"},
                    {"name": "x", "entity": "notifications", "group_by": "project"}):
            b = sb.api("POST", "/api/v1/views", bad, headers=as_user(u))
            check(f"rejects {list(bad.items())[1:] or bad}", b["status"] == 400,
                  b["status"])

        print("  [6] invite hygiene (#237 — validate before any write)")
        bad = sb.api("POST", "/api/v1/users/invite",
                     {"username": "ghosty", "scopes": ["group:nope"], "role": "agent"})
        check("invite with unknown group 400s loud (was a 500 FK blowup)",
              bad["status"] == 400, bad["status"])
        ok2 = sb.api("POST", "/api/v1/users/invite",
                     {"username": "ghosty", "scopes": [], "role": "agent"})
        check("failed invite leaves NO half-created user (retry succeeds)",
              ok2["status"] == 200, ok2)

    finally:
        sb.stop()

    total = PASS + FAIL
    verdict = "ALL PASSED" if FAIL == 0 else "FAILURES DETECTED"
    print(f"\n  {verdict}: {PASS}/{total} view checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
