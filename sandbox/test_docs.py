#!/usr/bin/env python3
"""Docs plane (RFC #105) — versioned, contributor-directed knowledge surface.

A sibling to tickets. Contract under test:
  1. Create opens revision 1; owner (source) is bearer-derived; slug derived + unique.
  2. An edit = a new IMMUTABLE revision: head advances, old revision bodies are
     preserved verbatim (?rev=N), history is append-only.
  3. Supersession: a new doc can replace an older one (status flip + doc link).
  4. Links thread a doc back to a ticket (discoverable from ?linked_ticket=); bad
     targets 400.
  5. extra="forbid": a body-supplied `author` is rejected (provenance is safe).
  6. Visibility reuses the group-scope predicate: commons (null) visible to all;
     a scoped doc only to that group's holders.
  7. Ownership gate (safe + contiguous, #86): anyone who can see a doc may
     contribute a revision, but only the owner/admin may PATCH/DELETE it.
  8. Notification fan-out reuses the #37 durable inbox: a revision pings watchers
     (skipping the actor); unsubscribe stops it.

Run: python3 sandbox/test_docs.py
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
    """Create a non-admin user and return a bearer token acting as them."""
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


def user_id_of(sb, username):
    r = sb.api("GET", "/api/v1/users")["data"]
    rows = r if isinstance(r, list) else r.get("users", [])
    for u in rows:
        if u.get("username") == username:
            return u["id"]
    return None


def main():
    sb = Sandbox()
    sb.start()  # admin user + token
    try:
        # ── 1. create → revision 1, derived slug, bearer-derived owner ──
        r = sb.api("POST", "/api/v1/docs",
                   {"title": "Design Note", "body": "v1 body", "kind": "rfc",
                    "note": "first"})
        check("Create returns 200", r["status"] == 200, r["data"])
        doc_id = r["data"].get("doc_id")
        check("Slug derived from title", r["data"].get("slug") == "design-note", r["data"].get("slug"))
        check("Opens at revision 1", r["data"].get("rev") == 1, r["data"].get("rev"))
        check("Creator auto-subscribed", r["data"].get("subscribed") is True)

        d = sb.api("GET", f"/api/v1/docs/{doc_id}")["data"]
        check("Owner (source) is bearer-derived 'admin'", d.get("source") == "admin", d.get("source"))
        check("Head body is v1", d.get("body") == "v1 body", d.get("body"))
        check("Revision count 1", d.get("revision_count") == 1, d.get("revision_count"))
        check("Resolvable by slug", sb.api("GET", "/api/v1/docs/design-note")["data"].get("id") == doc_id)

        # ── 2. edit = new immutable revision; old body preserved ──
        r = sb.api("POST", f"/api/v1/docs/{doc_id}/revisions",
                   {"body": "v2 body", "note": "second pass"})
        check("Revision returns rev 2", r["status"] == 200 and r["data"].get("rev") == 2, r["data"])
        d = sb.api("GET", f"/api/v1/docs/{doc_id}")["data"]
        check("Head body now v2", d.get("body") == "v2 body", d.get("body"))
        check("current_rev advanced to 2", d.get("current_rev") == 2, d.get("current_rev"))
        check("Revision count 2", d.get("revision_count") == 2, d.get("revision_count"))
        old = sb.api("GET", f"/api/v1/docs/{doc_id}?rev=1")["data"]
        check("?rev=1 still returns the ORIGINAL v1 body (immutable history)",
              old.get("body") == "v1 body", old.get("body"))
        hist = sb.api("GET", f"/api/v1/docs/{doc_id}/revisions")["data"]
        check("History has 2 revisions, newest first",
              [x["rev"] for x in hist["revisions"]] == [2, 1], hist["revisions"])
        check("Rev authors are bearer-derived",
              all(x["author"] == "admin" for x in hist["revisions"]))
        miss = sb.api("GET", f"/api/v1/docs/{doc_id}?rev=99")
        check("Nonexistent rev 404s", miss["status"] == 404, miss["status"])

        # ── 3. slug uniqueness ──
        r2 = sb.api("POST", "/api/v1/docs", {"title": "Design Note", "body": "other"})
        check("Same title → distinct slug", r2["data"].get("slug") == "design-note-2", r2["data"].get("slug"))

        # ── 4. supersession ──
        sup = sb.api("POST", "/api/v1/docs",
                     {"title": "Design Note (rewrite)", "body": "fresh", "supersedes": doc_id})
        check("Supersede returns 200", sup["status"] == 200, sup["data"])
        old_doc = sb.api("GET", f"/api/v1/docs/{doc_id}")["data"]
        check("Superseded doc flips status=superseded", old_doc.get("status") == "superseded", old_doc.get("status"))
        new_links = sb.api("GET", f"/api/v1/docs/{sup['data']['doc_id']}")["data"]["links"]
        check("New doc carries a doc-link back to the superseded one",
              any(l["target_type"] == "doc" and l["target_id"] == str(doc_id) for l in new_links), new_links)

        # ── 5. links thread back to a ticket ──
        tk = sb.api("POST", "/api/v1/tickets", {"summary": "drove this doc", "type": "feature"})
        tid = tk["data"]["ticket_id"]
        lk = sb.api("POST", f"/api/v1/docs/{doc_id}/links", {"target_type": "ticket", "target_id": str(tid)})
        check("Link to ticket 200", lk["status"] == 200, lk["data"])
        found = sb.api("GET", f"/api/v1/docs?linked_ticket={tid}")["data"]["docs"]
        check("?linked_ticket finds the doc", any(x["id"] == doc_id for x in found), [x["id"] for x in found])
        bad = sb.api("POST", f"/api/v1/docs/{doc_id}/links", {"target_type": "ticket", "target_id": "99999"})
        check("Link to unknown ticket 400", bad["status"] == 400, bad["status"])

        # ── 6. extra="forbid": body-supplied author is rejected ──
        forge = sb.api("POST", "/api/v1/docs",
                       {"title": "Forged", "body": "x", "author": "someone-else"})
        check("Body-supplied `author` → 422 (provenance is safe)", forge["status"] == 422, forge["status"])

        # ── 7. visibility (group-scope predicate) ──
        for gid in ("work", "home"):
            sb.api("POST", "/api/v1/groups", {"id": gid, "name": gid.title()})
        contrib = mk_user(sb, "contrib", ["group:work"])
        work_doc = sb.api("POST", "/api/v1/docs",
                          {"title": "Work Only", "body": "w", "group_scope": "work"})["data"]["doc_id"]
        home_doc = sb.api("POST", "/api/v1/docs",
                          {"title": "Home Only", "body": "h", "group_scope": "home"})["data"]["doc_id"]
        # scope v1 (doc 261): the admin's unnamed doc goes to `samcloud-contributors`; nothing is commons
        core_doc = sb.api("POST", "/api/v1/docs", {"title": "Contributors By Default", "body": "c"})["data"]
        check("an unnamed doc lands in the writer's group (admin → samcloud-contributors)", core_doc.get("group_scope") == "samcloud-contributors", core_doc)

        seen = {x["id"] for x in sb.api("GET", "/api/v1/docs", headers=as_user(contrib))["data"]["docs"]}
        check("contrib (group:work) sees the work-scoped doc", work_doc in seen)
        check("contrib does NOT see the admin's contributors doc (nothing unscoped is shared)", core_doc["doc_id"] not in seen)
        check("contrib does NOT see the home-scoped doc", home_doc not in seen)
        denied = sb.api("GET", f"/api/v1/docs/{home_doc}", headers=as_user(contrib))
        check("Direct GET of out-of-scope doc is hidden (404)", denied["status"] == 404, denied["status"])
        check("Unknown group_scope on create 400s",
              sb.api("POST", "/api/v1/docs", {"title": "X", "body": "y", "group_scope": "nope"})["status"] == 400)

        # ── 8. ownership gate: a fellow editor can revise, not mutate lifecycle ──
        other = mk_user(sb, "other", ["group:work"])
        owned = sb.api("POST", "/api/v1/docs", {"title": "Owned By Contrib", "body": "1"},
                       headers=as_user(contrib))["data"]["doc_id"]
        rev_by_other = sb.api("POST", f"/api/v1/docs/{owned}/revisions",
                              {"body": "2 by other"}, headers=as_user(other))
        check("Non-owner CAN contribute a revision (contributor-directed)",
              rev_by_other["status"] == 200, rev_by_other["data"])
        rev_author = sb.api("GET", f"/api/v1/docs/{owned}/revisions")["data"]["revisions"][0]["author"]
        check("Contributed revision is attributed to 'other'", rev_author == "other", rev_author)
        patch_by_other = sb.api("PATCH", f"/api/v1/docs/{owned}", {"status": "archived"},
                                headers=as_user(other))
        check("Non-owner CANNOT PATCH lifecycle → 403", patch_by_other["status"] == 403, patch_by_other["status"])
        patch_by_admin = sb.api("PATCH", f"/api/v1/docs/{owned}", {"status": "archived"})
        check("Admin CAN PATCH any doc", patch_by_admin["status"] == 200, patch_by_admin["data"])
        del_by_other = sb.api("DELETE", f"/api/v1/docs/{owned}", headers=as_user(other))
        check("Non-owner CANNOT delete → 403", del_by_other["status"] == 403, del_by_other["status"])

        # ── 9. notification fan-out (durable inbox, skip actor) ──
        watched = sb.api("POST", "/api/v1/docs", {"title": "Watched", "body": "1", "group_scope": "work"})["data"]["doc_id"]  # admin owns+subscribed; in contrib's group
        sb.api("POST", f"/api/v1/docs/{watched}/revisions", {"body": "2"}, headers=as_user(contrib))
        inbox = sb.api("GET", "/api/v1/notifications?unread=true")["data"]["notifications"]
        doc_notifs = [n for n in inbox if n.get("doc_id") == watched and n.get("event_type") == "doc.revised"]
        check("Admin (watcher) got a doc.revised notification", len(doc_notifs) == 1, inbox)
        check("Notification actor is the reviser, not the watcher",
              doc_notifs and doc_notifs[0]["actor"] == "contrib", doc_notifs)
        # ack must actually persist read=true (dogfood flagged a possible no-op;
        # this also guards the shared #37 ack path for doc rows).
        if doc_notifs:
            nid = doc_notifs[0]["id"]
            sb.api("POST", "/api/v1/notifications/ack", {"ids": [nid]})
            allrows = sb.api("GET", "/api/v1/notifications?unread=false")["data"]["notifications"]
            acked = [n for n in allrows if n["id"] == nid]
            check("ack persists read=true on the row", bool(acked) and acked[0].get("read") is True, acked)
        contrib_inbox = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(contrib))["data"]["notifications"]
        check("Actor does NOT get woken by their own revision",
              not any(n.get("doc_id") == watched and n.get("event_type") != "doc.published" for n in contrib_inbox), contrib_inbox)
        sb.api("DELETE", f"/api/v1/docs/{watched}/subscribe")  # admin unsubscribes
        sb.api("POST", "/api/v1/notifications/ack", {"all": True})
        sb.api("POST", f"/api/v1/docs/{watched}/revisions", {"body": "3"}, headers=as_user(contrib))
        after = sb.api("GET", "/api/v1/notifications?unread=true")["data"]["notifications"]
        check("Unsubscribing does not remove the owner's responsibility",
              len([n for n in after if n.get("doc_id") == watched and n["event_type"] == "doc.revised"]) == 1, after)

        # ── 10. delete is atomic even with notification + supersede refs ──
        # Regression (dogfood 2026-06-29): notifications.doc_id and docs.supersedes
        # FK-reference docs(id); a non-transactional delete 500'd AND orphaned the
        # doc with its history already wiped.
        dz = sb.api("POST", "/api/v1/docs", {"title": "Delete With Refs", "body": "1", "group_scope": "work"})["data"]["doc_id"]
        sb.api("POST", f"/api/v1/docs/{dz}/subscribe", headers=as_user(contrib))
        sb.api("POST", f"/api/v1/docs/{dz}/revisions", {"body": "2"})  # admin revises → fan-out row to contrib
        sup2 = sb.api("POST", "/api/v1/docs", {"title": "Superseder", "body": "x", "supersedes": dz})["data"]["doc_id"]
        delr = sb.api("DELETE", f"/api/v1/docs/{dz}")
        check("Delete a doc WITH notification + supersede refs → 200 (not 500)", delr["status"] == 200, delr["data"])
        check("Deleted doc is fully gone (404, not an orphan)",
              sb.api("GET", f"/api/v1/docs/{dz}")["status"] == 404)
        check("Superseder's now-dangling supersedes was cleared",
              sb.api("GET", f"/api/v1/docs/{sup2}")["data"].get("supersedes") is None)

        # ── 11. write-side group_scope is membership-enforced ──
        ok_scope = sb.api("POST", "/api/v1/docs",
                          {"title": "Work By Member", "body": "x", "group_scope": "work"},
                          headers=as_user(contrib))
        check("Member CAN scope a doc to their own group", ok_scope["status"] == 200, ok_scope["data"])
        bad_scope = sb.api("POST", "/api/v1/docs",
                           {"title": "Home By Nonmember", "body": "x", "group_scope": "home"},
                           headers=as_user(contrib))
        check("Non-member CANNOT scope a doc to a group they're not in → 403", bad_scope["status"] == 403, bad_scope["status"])
        admin_scope = sb.api("POST", "/api/v1/docs", {"title": "Admin Scopes Anywhere", "body": "x", "group_scope": "home"})
        check("Admin can scope to any group", admin_scope["status"] == 200, admin_scope["data"])

        # ── 12. comments: the discussion layer, DISTINCT from the body ──
        cdoc = sb.api("POST", "/api/v1/docs", {"title": "Commentable", "body": "the body", "group_scope": "work"})["data"]["doc_id"]  # admin owns+subscribed; in contrib's group
        cm = sb.api("POST", f"/api/v1/docs/{cdoc}/comments", {"body": "first comment"}, headers=as_user(contrib))
        check("Anyone in scope can comment → 200", cm["status"] == 200, cm["data"])
        check("Body-supplied author on a comment → 422",
              sb.api("POST", f"/api/v1/docs/{cdoc}/comments", {"body": "x", "author": "fake"})["status"] == 422)
        dv = sb.api("GET", f"/api/v1/docs/{cdoc}")["data"]
        check("Comment embedded in the doc, author bearer-derived",
              len(dv.get("comments", [])) == 1 and dv["comments"][0]["author"] == "contrib", dv.get("comments"))
        check("A comment does NOT touch the body or rev (commentary != edit)",
              dv["body"] == "the body" and dv["current_rev"] == 1, (dv["body"], dv["current_rev"]))
        sb.api("POST", "/api/v1/notifications/ack", {"all": True})  # reset admin inbox
        sb.api("POST", f"/api/v1/docs/{cdoc}/comments", {"body": "second"}, headers=as_user(contrib))
        ainbox = sb.api("GET", "/api/v1/notifications?unread=true")["data"]["notifications"]
        check("Owner/watcher notified of doc.commented (fan-out, actor skipped)",
              any(n.get("doc_id") == cdoc and n.get("event_type") == "doc.commented" for n in ainbox), ainbox)
        check("Delete a doc WITH comments → 200 (comment FK cleared)",
              sb.api("DELETE", f"/api/v1/docs/{cdoc}")["status"] == 200)

        # ── 13. append helper: additive edit without resubmitting the body ──
        ad = sb.api("POST", "/api/v1/docs", {"title": "Appendable", "body": "line one"})["data"]["doc_id"]
        ap = sb.api("POST", f"/api/v1/docs/{ad}/append", {"block": "line two", "note": "add a line"})
        check("Append → 200, rev advances to 2", ap["status"] == 200 and ap["data"].get("rev") == 2, ap["data"])
        check("Appended body = base + separator + block",
              sb.api("GET", f"/api/v1/docs/{ad}")["data"]["body"] == "line one\n\nline two",
              sb.api("GET", f"/api/v1/docs/{ad}")["data"]["body"])
        check("Append is a real immutable revision (rev1 body intact)",
              sb.api("GET", f"/api/v1/docs/{ad}?rev=1")["data"]["body"] == "line one")
        sb.api("POST", f"/api/v1/docs/{ad}/append", {"block": "line three", "separator": "\n"})
        check("Custom separator honored",
              sb.api("GET", f"/api/v1/docs/{ad}")["data"]["body"] == "line one\n\nline two\nline three")

        # ── 14. reach (#123 follow-up): @mention / doc→ticket link / group fan-out ──
        # @mention in the body → subscribe + notify the mentioned user
        # scope v1: a mention reaches a member of the doc's group — the admin names `work`
        md = sb.api("POST", "/api/v1/docs", {"title": "Mention Body", "body": "intro @contrib please review", "group_scope": "work"})["data"]["doc_id"]
        cinbox = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(contrib))["data"]["notifications"]
        check("@mention in body notifies the mentioned user",
              any(n.get("doc_id") == md and n.get("event_type") == "doc.mentioned" for n in cinbox), cinbox)
        check("mentioned user becomes a watcher",
              any(w["identity"] == "contrib" for w in sb.api("GET", f"/api/v1/docs/{md}/watchers")["data"]["watchers"]))
        check("unknown @handle is ignored (doc still created)",
              sb.api("POST", "/api/v1/docs", {"title": "Ghost", "body": "ping @nonexistent-ghost-xyz"})["status"] == 200)
        # @mention in a comment
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(other))
        sb.api("POST", f"/api/v1/docs/{md}/comments", {"body": "cc @other"})
        oinbox = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(other))["data"]["notifications"]
        check("@mention in a comment notifies the mentioned user",
              any(n.get("doc_id") == md and n.get("event_type") == "doc.mentioned" for n in oinbox), oinbox)
        # #414: revising a doc must NOT re-fire doc.mentioned for names sitting in
        # unchanged prose (whole-body re-parse was a self-amplifying fan-out loop);
        # a mention newly introduced by the revision still fires.
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(contrib))
        sb.api("POST", f"/api/v1/docs/{md}/revisions",
               {"body": "intro @contrib please review\n\nedited elsewhere", "note": "no new mention"})
        c_after = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(contrib))["data"]["notifications"]
        check("revision with mention only in unchanged prose does NOT re-fire doc.mentioned",
              not any(n.get("doc_id") == md and n.get("event_type") == "doc.mentioned" for n in c_after), c_after)
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(other))
        sb.api("POST", f"/api/v1/docs/{md}/revisions",
               {"body": "intro @contrib please review\n\nnow also @other", "note": "adds a mention"})
        o_after = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(other))["data"]["notifications"]
        check("mention newly introduced by a revision fires doc.mentioned once",
              sum(1 for n in o_after if n.get("doc_id") == md and n.get("event_type") == "doc.mentioned") == 1, o_after)
        # REGRESSION (found in prod by a seat within minutes of v0.7.75):
        # an identity-set diff answers "is this user mentioned anywhere already?", so on a
        # long-lived doc where their name appears, a NEW @them never fires and the routed
        # ask reaches nobody. The diff must count occurrences, not membership.
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(contrib))
        sb.api("POST", f"/api/v1/docs/{md}/revisions",
               {"body": "intro @contrib please review\n\nnow also @other\n\n@contrib one more ask",
                "note": "second mention of an already-mentioned user"})
        c2 = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(contrib))["data"]["notifications"]
        check("an ADDITIONAL mention of an already-named user still fires (count, not set)",
              sum(1 for n in c2 if n.get("doc_id") == md and n.get("event_type") == "doc.mentioned") == 1, c2)
        # doc→ticket link notifies the ticket's watchers (bridges into the ticket graph)
        tk = sb.api("POST", "/api/v1/tickets", {"summary": "watched ticket", "type": "task"}, headers=as_user(contrib))["data"]["ticket_id"]
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(contrib))
        ld = sb.api("POST", "/api/v1/docs", {"title": "Linker", "body": "x"})["data"]["doc_id"]
        sb.api("POST", f"/api/v1/docs/{ld}/links", {"target_type": "ticket", "target_id": str(tk)})
        cl = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(contrib))["data"]["notifications"]
        check("doc→ticket link notifies the ticket's watchers (doc.linked)",
              any(n.get("event_type") == "doc.linked" and n.get("ticket_id") == tk for n in cl), cl)
        # Group access is not a publication subscription.
        sb.api("POST", "/api/v1/groups/work/members", {"user_id": user_id_of(sb, "contrib"), "role": "viewer"})
        sb.api("POST", "/api/v1/notifications/ack", {"all": True}, headers=as_user(contrib))
        sb.api("POST", "/api/v1/docs", {"title": "Work Publish", "body": "team note", "group_scope": "work"})
        cg = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(contrib))["data"]["notifications"]
        check("group-scoped publish does not notify group members",
              not any(n.get("event_type") == "doc.published" for n in cg), cg)

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
