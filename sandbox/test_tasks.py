#!/usr/bin/env python3
"""Task plane (doc #13, ticket #148) — projects / tasks / subtasks.

The third primitive of the trichotomy: docs = canonical records, tickets =
requests needing another's attention, tasks = actionable items with specific
direction. Contract under test:
  1. Create project: slug derived + unique, owner (source) bearer-derived,
     status validated.
  2. Tasks nest from day one (parent_task_id); parent must be same-project;
     no self-parenting.
  3. Rollups computed on read: counts by status, estimate-weighted progress
     (count-ratio fallback), cancelled excluded from the denominator.
  4. GET project returns the nested task tree (the legible UI shape).
  5. Visibility rides the group-scope predicate (commons/group/owner), read
     AND write side; the flat /tasks list scope-filters through projects.
  6. Noise posture: the ONLY durable notification is task.assigned to the
     assignee (no watcher fan-out, no self-notify).
  7. Lifecycle: project PATCH/DELETE owner-only; task edits open to scope;
     task delete creator/owner-only; deleting a parent re-roots subtasks.
  8. Delegate: the one explicit bridge to tickets — files a linked ticket,
     task flips to blocked with the waiting-handle; re-delegate 409s.
  9. extra="forbid": body-supplied `source` → 422 (provenance is safe).

Run: python3 sandbox/test_tasks.py
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
        # ── 1. create project: slug, bearer-derived owner, status guard ──
        r = sb.api("POST", "/api/v1/projects", {"name": "Printer Fix"})
        check("Create project 200", r["status"] == 200, r["data"])
        pid = r["data"].get("project_id")
        check("Slug derived from name", r["data"].get("slug") == "printer-fix", r["data"].get("slug"))
        p = sb.api("GET", f"/api/v1/projects/{pid}")["data"]
        check("Owner (source) bearer-derived 'admin'", p.get("source") == "admin", p.get("source"))
        check("Resolvable by slug",
              sb.api("GET", "/api/v1/projects/printer-fix")["data"].get("id") == pid)
        check("Same name → distinct slug",
              sb.api("POST", "/api/v1/projects", {"name": "Printer Fix"})["data"].get("slug") == "printer-fix-2")
        check("Bad project status 400",
              sb.api("POST", "/api/v1/projects", {"name": "X", "status": "wat"})["status"] == 400)
        check("Body-supplied `source` → 422 (provenance safe)",
              sb.api("POST", "/api/v1/projects", {"name": "Y", "source": "forged"})["status"] == 422)

        # ── ticket #189 finding 4: `title` alias for `name` (schema drift) ──
        alias_r = sb.api("POST", "/api/v1/projects", {"title": "Alias Named"})
        check("`title` accepted as alias for `name` on create", alias_r["status"] == 200, alias_r["data"])
        alias_pid = alias_r["data"].get("project_id")
        check("Alias landed in `name`",
              sb.api("GET", f"/api/v1/projects/{alias_pid}")["data"].get("name") == "Alias Named")
        wins_pid = sb.api("POST", "/api/v1/projects",
                         {"name": "Real Name", "title": "Ignored"})["data"].get("project_id")
        check("Explicit `name` wins over `title` alias",
              sb.api("GET", f"/api/v1/projects/{wins_pid}")["data"].get("name") == "Real Name")
        check("Neither `name` nor `title` → 422",
              sb.api("POST", "/api/v1/projects", {"description": "no name"})["status"] == 422)
        check("`title` accepted as alias for `name` on PATCH",
              sb.api("PATCH", f"/api/v1/projects/{alias_pid}",
                     {"title": "Renamed via alias"})["status"] == 200)
        check("PATCH alias landed in `name`",
              sb.api("GET", f"/api/v1/projects/{alias_pid}")["data"].get("name") == "Renamed via alias")

        # ── 2. tasks + nesting ──
        t1 = sb.api("POST", f"/api/v1/projects/{pid}/tasks",
                    {"title": "Diagnose paper jam", "estimate_min": 60})
        check("Create task 200", t1["status"] == 200, t1["data"])
        t1_id = t1["data"]["task_id"]
        sub = sb.api("POST", f"/api/v1/projects/{pid}/tasks",
                     {"title": "Test with spare cable", "parent_task_id": t1_id,
                      "estimate_min": 30})
        check("Subtask under parent 200 (nesting from day one)", sub["status"] == 200, sub["data"])
        sub_id = sub["data"]["task_id"]
        t2 = sb.api("POST", f"/api/v1/projects/{pid}/tasks",
                    {"title": "Update printer driver", "estimate_min": 90})
        t2_id = t2["data"]["task_id"]
        other_p = sb.api("POST", "/api/v1/projects", {"name": "Elsewhere"})["data"]["project_id"]
        check("Parent from another project 400",
              sb.api("POST", f"/api/v1/projects/{other_p}/tasks",
                     {"title": "bad", "parent_task_id": t1_id})["status"] == 400)
        check("Self-parent 400",
              sb.api("PATCH", f"/api/v1/tasks/{t1_id}", {"parent_task_id": t1_id})["status"] == 400)
        check("Bad task status 400",
              sb.api("POST", f"/api/v1/projects/{pid}/tasks",
                     {"title": "z", "status": "wat"})["status"] == 400)
        check("Unknown assignee 400",
              sb.api("POST", f"/api/v1/projects/{pid}/tasks",
                     {"title": "z", "assignee": "nobody-real"})["status"] == 400)

        # ── 3+4. tree + rollup ──
        p = sb.api("GET", f"/api/v1/projects/{pid}")["data"]
        roots = p.get("tasks", [])
        check("Tree: 2 roots", len(roots) == 2, [t.get("title") for t in roots])
        check("Tree: subtask nested under its parent",
              any(t["id"] == t1_id and len(t["subtasks"]) == 1
                  and t["subtasks"][0]["id"] == sub_id for t in roots), roots)
        check("Rollup counts 3 tasks", p["rollup"]["task_count"] == 3, p["rollup"])
        check("Rollup 0% before any done", p["rollup"]["progress_pct"] == 0, p["rollup"])
        d = sb.api("PATCH", f"/api/v1/tasks/{t2_id}", {"status": "done"})
        check("PATCH status=done 200", d["status"] == 200, d["data"])
        ru = sb.api("GET", f"/api/v1/projects/{pid}")["data"]["rollup"]
        check("Estimate-weighted progress: 90/180 = 50%",
              ru["progress_pct"] == 50 and ru["progress_basis"] == "estimate-weighted", ru)
        got = sb.api("GET", f"/api/v1/tasks/{t2_id}")["data"]
        check("completed_at stamped on done", bool(got.get("completed_at")), got)
        sb.api("POST", f"/api/v1/projects/{pid}/tasks", {"title": "Abandoned", "status": "cancelled"})
        ru = sb.api("GET", f"/api/v1/projects/{pid}")["data"]["rollup"]
        check("Cancelled excluded from denominator (still 50%)", ru["progress_pct"] == 50, ru)
        cnt_p = sb.api("POST", "/api/v1/projects", {"name": "No Estimates"})["data"]["project_id"]
        sb.api("POST", f"/api/v1/projects/{cnt_p}/tasks", {"title": "a", "status": "done"})
        sb.api("POST", f"/api/v1/projects/{cnt_p}/tasks", {"title": "b"})
        ru2 = sb.api("GET", f"/api/v1/projects/{cnt_p}")["data"]["rollup"]
        check("Count-basis fallback when no estimates (1/2 = 50%)",
              ru2["progress_pct"] == 50 and ru2["progress_basis"] == "count", ru2)
        check("List projects carries rollups",
              all("rollup" in x for x in sb.api("GET", "/api/v1/projects")["data"]["projects"]))

        # ── 5. visibility (group scope, read + write side) ──
        for gid in ("work", "home"):
            sb.api("POST", "/api/v1/groups", {"id": gid, "name": gid.title()})
        worker = mk_user(sb, "worker", ["group:work"])
        # scope v1 (doc 261): the admin's unnamed project `pid` is in `samcloud-contributors`;
        # the worker joins it as an editor so the shared sections below hold
        _wid = sb.api("GET", "/api/v1/users")["data"]
        _wid = next(u["id"] for u in _wid if u["username"] == "worker")
        sb.api("POST", "/api/v1/groups/samcloud-contributors/members", {"user_id": _wid, "role": "editor"})
        wp = sb.api("POST", "/api/v1/projects",
                    {"name": "Work Scoped", "group_scope": "work"})["data"]["project_id"]
        hp = sb.api("POST", "/api/v1/projects",
                    {"name": "Home Scoped", "group_scope": "home"})["data"]["project_id"]
        sb.api("POST", f"/api/v1/projects/{hp}/tasks", {"title": "secret home task"})
        seen = {x["id"] for x in sb.api("GET", "/api/v1/projects",
                                        headers=as_user(worker))["data"]["projects"]}
        check("group:work sees work-scoped project", wp in seen)
        check("a contributor sees the admin's project (nothing is commons)", pid in seen)
        check("group:work does NOT see home-scoped project", hp not in seen)
        check("Direct GET of out-of-scope project is hidden (404)",
              sb.api("GET", f"/api/v1/projects/{hp}", headers=as_user(worker))["status"] == 404)
        flat = sb.api("GET", "/api/v1/tasks", headers=as_user(worker))["data"]["tasks"]
        check("Flat /tasks list excludes out-of-scope projects' tasks",
              not any(t["project_id"] == hp for t in flat))
        check("Non-member cannot scope a project into a foreign group → 403",
              sb.api("POST", "/api/v1/projects",
                     {"name": "Sneaky", "group_scope": "home"},
                     headers=as_user(worker))["status"] == 403)
        check("Unknown group_scope 400",
              sb.api("POST", "/api/v1/projects",
                     {"name": "Ghost", "group_scope": "nope"})["status"] == 400)
        check("Out-of-scope caller cannot add a task → hidden (404)",
              sb.api("POST", f"/api/v1/projects/{hp}/tasks", {"title": "x"},
                     headers=as_user(worker))["status"] == 404)

        # ── 6. noise posture: only the assignee hears, once ──
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(worker))
        at = sb.api("POST", f"/api/v1/projects/{pid}/tasks",
                    {"title": "For worker", "assignee": "worker"})
        check("Task with assignee 200", at["status"] == 200, at["data"])
        winbox = sb.api("GET", "/api/v1/notifications?unread=true",
                        headers=as_user(worker))["data"]["notifications"]
        assigned = [n for n in winbox if n.get("event_type") == "task.assigned"]
        check("Assignee got exactly one task.assigned row", len(assigned) == 1, winbox)
        check("Row carries task_id", assigned and assigned[0].get("task_id") == at["data"]["task_id"], assigned)
        sb.api("POST", "/api/v1/notifications/ack", {"all": True})
        sb.api("POST", f"/api/v1/projects/{pid}/tasks", {"title": "Self", "assignee": "admin"})
        ainbox = sb.api("GET", "/api/v1/notifications?unread=true")["data"]["notifications"]
        check("Self-assignment does NOT notify (quiet plane)",
              not any(n.get("event_type") == "task.assigned" for n in ainbox), ainbox)

        # ── 7. lifecycle gates ──
        check("Non-owner cannot PATCH project → 403",
              sb.api("PATCH", f"/api/v1/projects/{pid}", {"status": "paused"},
                     headers=as_user(worker))["status"] == 403)
        check("In-scope non-owner CAN edit a task (collaboration open)",
              sb.api("PATCH", f"/api/v1/tasks/{t1_id}", {"status": "in_progress"},
                     headers=as_user(worker))["status"] == 200)
        check("In-scope non-creator cannot DELETE a task → 403",
              sb.api("DELETE", f"/api/v1/tasks/{t1_id}", headers=as_user(worker))["status"] == 403)
        del_parent = sb.api("DELETE", f"/api/v1/tasks/{t1_id}")
        check("Owner deletes parent task 200", del_parent["status"] == 200, del_parent["data"])
        re_rooted = sb.api("GET", f"/api/v1/tasks/{sub_id}")["data"]
        check("Orphaned subtask re-rooted (parent cleared, not destroyed)",
              re_rooted.get("parent_task_id") is None, re_rooted)
        check("Owner PATCH project 200",
              sb.api("PATCH", f"/api/v1/projects/{pid}", {"status": "paused"})["status"] == 200)

        # ── 8. delegate: the one explicit bridge to tickets ──
        dt = sb.api("POST", f"/api/v1/projects/{pid}/tasks",
                    {"title": "Fix tray-2 feed workaround", "estimate_min": 45})["data"]["task_id"]
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(worker))
        dg = sb.api("POST", f"/api/v1/tasks/{dt}/delegate",
                    {"assignee": "worker", "note": "have a crack when free"})
        check("Delegate 200", dg["status"] == 200, dg["data"])
        tkid = dg["data"].get("ticket_id")
        check("Delegate filed a real ticket", bool(tkid), dg["data"])
        tk = sb.api("GET", f"/api/v1/tickets/{tkid}")["data"]
        check("Ticket assigned to the agent, type=task",
              tk.get("assignee") == "worker" and tk.get("type") == "task", tk)
        check("Ticket body cites the source task",
              f"task #{dt}" in (tk.get("body") or ""), tk.get("body"))
        task_after = sb.api("GET", f"/api/v1/tasks/{dt}")["data"]
        check("Task flipped to blocked (the waiting handle)",
              task_after.get("status") == "blocked", task_after)
        check("Task carries delegated_ticket_id",
              task_after.get("delegated_ticket_id") == tkid, task_after)
        winbox = sb.api("GET", "/api/v1/notifications?unread=true",
                        headers=as_user(worker))["data"]["notifications"]
        check("Agent heard about the delegation via the ticket inbox",
              any(n.get("ticket_id") == tkid for n in winbox), winbox)
        check("Re-delegate 409s",
              sb.api("POST", f"/api/v1/tasks/{dt}/delegate",
                     {"assignee": "worker"})["status"] == 409)

        # ── 9. project delete cascades ──
        gone_p = sb.api("POST", "/api/v1/projects", {"name": "Doomed"})["data"]["project_id"]
        gt = sb.api("POST", f"/api/v1/projects/{gone_p}/tasks",
                    {"title": "will vanish", "assignee": "worker"})["data"]["task_id"]
        delr = sb.api("DELETE", f"/api/v1/projects/{gone_p}")
        check("Delete project with tasks + notification refs → 200 (not 500)",
              delr["status"] == 200, delr["data"])
        check("Cascaded task is gone", sb.api("GET", f"/api/v1/tasks/{gt}")["status"] == 404)
        check("Project fully gone", sb.api("GET", f"/api/v1/projects/{gone_p}")["status"] == 404)

        # ── 10. project subscribe (#232): opt-in fan-out, quiet by default ──
        watcher = mk_user(sb, "producer-eye", ["*"])
        sp = sb.api("POST", "/api/v1/projects", {"name": "Watched", "group_scope": "work"})["data"]["project_id"]   # scope v1: in the workers' group

        def unread(tok):
            return [n for n in sb.api("GET", "/api/v1/notifications?unread=true",
                                      headers=as_user(tok))["data"]["notifications"]]

        base = len(unread(watcher))
        sb.api("POST", f"/api/v1/projects/{sp}/tasks", {"title": "quiet task"})
        check("Quiet by default: non-watcher hears nothing on task create",
              len(unread(watcher)) == base)
        r = sb.api("POST", f"/api/v1/projects/{sp}/subscribe", headers=as_user(watcher))
        check("Subscribe 200", r["status"] == 200, r["data"])
        wt = sb.api("POST", f"/api/v1/projects/{sp}/tasks",
                    {"title": "watched task"})["data"]["task_id"]
        rows = unread(watcher)
        check("Watcher hears task.created",
              any(n.get("task_id") == wt and n["event_type"] == "task.created"
                  for n in rows), rows[:3])
        sb.api("PATCH", f"/api/v1/tasks/{wt}", {"status": "in_progress"})
        check("Watcher hears task.updated",
              any(n.get("task_id") == wt and n["event_type"] == "task.updated"
                  for n in unread(watcher)))
        n_before = len(unread(watcher))
        sb.api("PATCH", f"/api/v1/tasks/{wt}", {"status": "done"},
               headers=as_user(watcher))
        check("Actor never notified of their own edit",
              len(unread(watcher)) == n_before)
        sb.api("DELETE", f"/api/v1/projects/{sp}/subscribe", headers=as_user(watcher))
        n_before = len(unread(watcher))
        sb.api("POST", f"/api/v1/projects/{sp}/tasks", {"title": "post-unsub task"})
        check("Unsubscribe stops the fan-out", len(unread(watcher)) == n_before)

        # ── 11. task comments (#203 riff: same conversational layer as tickets) ──
        ct = sb.api("POST", f"/api/v1/projects/{sp}/tasks",
                    {"title": "commented task", "assignee": "worker"})["data"]["task_id"]
        r = sb.api("POST", f"/api/v1/tasks/{ct}/comments", {"body": "first note"})
        check("Comment 200 + id", r["status"] == 200 and r["data"].get("comment_id"),
              r["data"])
        got = sb.api("GET", f"/api/v1/tasks/{ct}")["data"]
        check("Comments in GET /tasks/{id}, author bearer-derived",
              len(got.get("comments", [])) == 1
              and got["comments"][0]["author"] == "admin"
              and got["comments"][0]["body"] == "first note", got.get("comments"))
        wrows = [n for n in unread(worker)
                 if n.get("task_id") == ct and n["event_type"] == "task.commented"]
        check("Assignee notified of comment", len(wrows) == 1, wrows)
        r = sb.api("POST", f"/api/v1/tasks/{ct}/comments",
                   {"body": "self note", "author": "spoofed"})
        check("Body-supplied author 422s (provenance)", r["status"] == 422, r["status"])
        # ── 11b. thread-participant fan-out (v0.7.47) ──────────────────────
        # A reply must reach the thread even with no assignee and no watchers
        # (live gap: a question on an unassigned task notified nobody).
        alice = mk_user(sb, "alice", ["group:work"])
        pt = sb.api("POST", f"/api/v1/projects/{sp}/tasks",
                    {"title": "orphan question"},
                    headers=as_user(alice))["data"]["task_id"]  # alice creates, UNASSIGNED
        sb.api("POST", f"/api/v1/tasks/{pt}/comments", {"body": "what do you reckon?"})
        arows = [n for n in unread(alice)
                 if n.get("task_id") == pt and n["event_type"] == "task.commented"]
        check("Creator hears a reply on their unassigned task", len(arows) == 1, arows)
        sb.api("POST", f"/api/v1/tasks/{pt}/comments", {"body": "leaning app store"},
               headers=as_user(alice))
        adminrows = [n for n in sb.api("GET", "/api/v1/notifications?unread=true"
                                       )["data"]["notifications"]
                     if n.get("task_id") == pt]
        check("Prior comment alone does not subscribe its writer", len(adminrows) == 0, adminrows)
        selfrows = [n for n in unread(alice)
                    if n.get("task_id") == pt and n["actor"] == "alice"]
        check("Actor never self-notified", len(selfrows) == 0, selfrows)
        sb.api("POST", f"/api/v1/projects/{sp}/subscribe", headers=as_user(alice))
        before = len([n for n in unread(alice) if n.get("task_id") == pt])
        sb.api("POST", f"/api/v1/tasks/{pt}/comments", {"body": "one more thought"})
        after = [n for n in unread(alice) if n.get("task_id") == pt]
        check("Participant who is also a watcher gets exactly one row",
              len(after) == before + 1, after)
        bystander = mk_user(sb, "bystander", ["group:work"])
        sb.api("POST", f"/api/v1/tasks/{pt}/comments",
               {"body": "needs a look from @bystander (@nosuchuser is just prose)"})
        brows = [n for n in unread(bystander) if n.get("task_id") == pt]
        check("@mention reaches a non-participant as task.mentioned",
              len(brows) == 1 and brows[0]["event_type"] == "task.mentioned", brows)

        delr = sb.api("DELETE", f"/api/v1/tasks/{ct}")
        check("Task with comments deletes cleanly", delr["status"] == 200, delr["data"])

        print()
        print("-" * 60)
        ok = FAIL == 0
        colour = "\033[0;32m" if ok else "\033[0;31m"
        print(f"  {colour}{'ALL PASSED' if ok else 'FAILURES'}: {PASS} passed, {FAIL} failed\033[0m")
        return 0 if ok else 1
    finally:
        sb.stop()


if __name__ == "__main__":
    sys.exit(main())
