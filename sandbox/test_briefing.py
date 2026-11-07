#!/usr/bin/env python3
"""Personal briefing endpoint (#203 — the human-user inbox surface).

Contract under test:
  1. GET /users/me/briefing returns the five sections, identity-scoped.
  2. unread_total counts THIS identity's unread rows; another user's rows
     never leak in.
  3. An @you ticket assignment lands in blocked_on_you (the #203 success
     criterion: an assignment reliably reaches its human).
  4. A task titled 'DECISION: ...' surfaces in decisions until done.
  5. Console ack round-trip: POST /notifications/ack {ids, surface:"console"}
     drops unread_total and stamps acked_via.

Run: python3 sandbox/test_briefing.py
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
        print("  [1] shape + identity scoping")
        alice_tok = mk_user(sb, "alice-test", ["*"])
        other_tok = mk_user(sb, "bystander", ["*"])
        r = sb.api("GET", "/api/v1/users/me/briefing", headers=as_user(alice_tok))
        check("briefing 200 with all sections", r["status"] == 200 and all(
            k in r["data"] for k in ("unread_total", "inbox", "blocked_on_you",
                                     "decisions", "menu", "moved_24h")),
            r["data"])
        base_unread = r["data"]["unread_total"]

        print("  [2] assignment reaches its human")
        t = sb.api("POST", "/api/v1/tickets",
                   {"type": "question", "priority": "high",
                    "summary": "needs alice-test's eyes", "body": "ratify me",
                    "assignee": "alice-test"}, headers=as_user(other_tok))
        tid = t["data"].get("ticket_id")
        check("ticket filed at alice-test", t["status"] in (200, 201) and tid, t["data"])
        r = sb.api("GET", "/api/v1/users/me/briefing", headers=as_user(alice_tok))
        check("assignment shows in blocked_on_you",
              any(b["id"] == tid for b in r["data"]["blocked_on_you"]),
              r["data"]["blocked_on_you"])
        check("assignment produced an unread inbox row",
              r["data"]["unread_total"] > base_unread
              and any(n.get("ticket_id") == tid for n in r["data"]["inbox"]),
              (base_unread, r["data"]["unread_total"]))
        rb = sb.api("GET", "/api/v1/users/me/briefing", headers=as_user(other_tok))
        check("bystander's briefing does not carry alice-test's assignment",
              all(b["id"] != tid for b in rb["data"]["blocked_on_you"]))

        print("  [3] decision-TAGGED tasks surface until decided (data, not title)")
        p = sb.api("POST", "/api/v1/projects",
                   {"slug": "briefing-proj", "name": "briefing test project"},
                   headers=as_user(alice_tok))
        check("project created", p["status"] in (200, 201), p["data"])
        d = sb.api("POST", "/api/v1/projects/briefing-proj/tasks",
                   {"title": "ship it or open-source it?",
                    "detail": "alice's call", "tags": ["decision", "tapes"]},
                   headers=as_user(alice_tok))
        did = d["data"].get("task_id")
        titled = sb.api("POST", "/api/v1/projects/briefing-proj/tasks",
                        {"title": "DECISION in title only, no tag"},
                        headers=as_user(alice_tok))["data"].get("task_id")
        r = sb.api("GET", "/api/v1/users/me/briefing", headers=as_user(alice_tok))
        check("tagged task listed in decisions",
              any(x["task_id"] == did for x in r["data"]["decisions"]),
              r["data"]["decisions"])
        check("title-only convention retired (untagged not listed)",
              all(x["task_id"] != titled for x in r["data"]["decisions"]))
        f = sb.api("GET", "/api/v1/tasks?tag=decision", headers=as_user(alice_tok))
        check("?tag= filter finds tagged task, returns parsed tags",
              any(t["id"] == did and t.get("tags") == ["decision", "tapes"]
                  for t in f["data"]["tasks"]), f["data"]["tasks"][:3])
        bad = sb.api("POST", "/api/v1/projects/briefing-proj/tasks",
                     {"title": "x", "tags": ["Bad Tag!"]}, headers=as_user(alice_tok))
        check("invalid tag 400s loudly", bad["status"] == 400, bad["status"])
        sb.api("PATCH", f"/api/v1/tasks/{did}", {"status": "done"},
               headers=as_user(alice_tok))
        r = sb.api("GET", "/api/v1/users/me/briefing", headers=as_user(alice_tok))
        check("decided task gone from decisions",
              all(x["task_id"] != did for x in r["data"]["decisions"]))

        print("  [4] console ack = handled, attributed")
        row_ids = [n["id"] for n in r["data"]["inbox"]]
        before = r["data"]["unread_total"]
        a = sb.api("POST", "/api/v1/notifications/ack",
                   {"ids": row_ids, "surface": "console"}, headers=as_user(alice_tok))
        check("ack accepts surface=console", a["status"] == 200
              and a["data"].get("acked_via") == "console", a["data"])
        r = sb.api("GET", "/api/v1/users/me/briefing", headers=as_user(alice_tok))
        check("unread_total dropped after console ack",
              r["data"]["unread_total"] < before,
              (before, r["data"]["unread_total"]))

    finally:
        sb.stop()

    total = PASS + FAIL
    verdict = "ALL PASSED" if FAIL == 0 else "FAILURES DETECTED"
    print(f"\n  {verdict}: {PASS}/{total} briefing checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
