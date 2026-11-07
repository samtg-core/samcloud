#!/usr/bin/env python3
"""Wake rail (RFC doc #17) — served helpers + last_seen observability.

Contract under test:
  1. /helpers/{wake,wake-install} serve with auth; 401 without;
     404 for unknown names.
  2. Served artifacts carry the v1 review deltas: stale-lock steal + watchdog
     (wake), stripped-env preflight (installer), untrusted-data fencing +
     ack-only-handled (contract). __API__ is substituted with the serving host.
  3. users.last_seen: null until the identity reads its inbox, stamped after
     (the rail's liveness signal, delta 5).

Run: python3 sandbox/test_wake.py
"""
import json
import sys
import time
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


def fetch_text(sb, path, token=None):
    """Raw fetch for PlainText helper endpoints (harness api() is JSON-only)."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(
        f"{sb.base_url}{path}",
        headers={"Authorization": f"Bearer {token or sb.admin_token}"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return {"status": resp.status, "text": resp.read().decode()}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "text": e.read().decode(errors="replace")}


def find_user(sb, username):
    r = sb.api("GET", "/api/v1/users")["data"]
    rows = r if isinstance(r, list) else r.get("users", [])
    for u in rows:
        if u.get("username") == username:
            return u
    return None


def main():
    sb = Sandbox()
    sb.start()
    try:
        print("  [1] serving + auth gate")
        r = sb.api("GET", "/api/v1/helpers/wake", _skip_auth=True)
        check("wake 401 without bearer", r["status"] == 401, r["status"])
        r = sb.api("GET", "/api/v1/helpers/nope")
        check("unknown helper 404", r["status"] == 404, r["status"])
        wake = fetch_text(sb, "/api/v1/helpers/wake")
        inst = fetch_text(sb, "/api/v1/helpers/wake-install")
        check("wake serves script", wake["status"] == 200 and "samcloud-wake" in wake["text"])
        check("installer serves", inst["status"] == 200 and "crontab" in inst["text"])

        print("  [2] v1 deltas present in artifacts")
        check("wake: stale-lock steal", "stealing stale lock" in wake["text"])
        check("wake: watchdog kill", "watchdog: killing session" in wake["text"])
        check("wake: 5xx logged not swallowed", "poll HTTP" in wake["text"])
        check("installer: stripped-env preflight", "env -i" in inst["text"]
              and "WAKE-PREFLIGHT-OK" in inst["text"])
        # #208: WAKE_CMD is documented as an env override (header line, #169)
        # but the written config must actually honor it — a non-Claude
        # harness (Hermes etc.) can't register its handler otherwise.
        check("installer: WAKE_CMD env override honored in written config",
              'WAKE_CMD="${WAKE_CMD:-' in inst["text"]
              and 'WAKE_CMD=$BIN_DIR/samcloud-wake' not in inst["text"])
        listen = fetch_text(sb, "/api/v1/helpers/listen")
        check("listener serves", listen["status"] == 200 and "samcloud-listen" in listen["text"])
        check("listener: Last-Event-ID resume", "Last-Event-ID" in listen["text"])
        check("listener: mandatory reconcile on connect",
              "reconcile-on-connect" in listen["text"])
        check("listener: idle watchdog + backoff", "IDLE_TIMEOUT" in listen["text"]
              and "backoff" in listen["text"])
        # #438: a healthy connection never idles out, so update checks (which
        # only run in the connect prologue) could go unbounded without this —
        # measured 4d13h on a live seat. Bound reconnect closes the gap.
        check("listener: bounded connection lifetime forces periodic update check",
              "MAX_CONN_AGE" in listen["text"]
              and "connected_at" in listen["text"])
        check("listener: harness-agnostic WAKE_CMD on stdin",
              "WAKE_CMD" in listen["text"] and "stdin" in listen["text"])
        check("installer: no cron ever — service-manager units only",
              "LaunchDaemon" in inst["text"] and "systemd" in inst["text"]
              and 'echo "*/' not in inst["text"])
        check("installer: migrates legacy v1 cron away",
              "removed legacy v1 cron entry" in inst["text"])
        # The v2 wake-prompt contract is retired (0.12.78): no seat reads it,
        # and a holder's text is the rail's (sandbox/test_rail_lifecycle.py).
        check("installer: token verified against target API",
              "auth/verify" in inst["text"])
        check("__API__ substituted with serving host",
              "__API__" not in wake["text"] and "/api/v1" in wake["text"])

        print("  [3] last_seen liveness stamp")
        sb.api("POST", "/api/v1/groups", {"id": "home", "name": "Home"})   # scope v1: a real group to be an editor of
        tok = mk_user(sb, "wakebot", ["group:home"])
        u = find_user(sb, "wakebot")
        check("last_seen null before any inbox read", u and not u.get("last_seen"),
              u.get("last_seen") if u else "no user")
        r = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(tok))
        check("inbox read ok", r["status"] == 200, r["status"])
        u = find_user(sb, "wakebot")
        check("last_seen stamped after inbox read", u and u.get("last_seen"),
              str(u)[:120] if u else "no user")

        print("  [4] assignee may drive status (#130 — the contract's resolve step)")
        tok2 = mk_user(sb, "wakebot2", ["group:home"])
        # scope v1: the assignee-drives-status rule is for a member who is NOT an
        # editor — make both bots viewers of home (mk_user made them editors)
        for _name in ("wakebot", "wakebot2"):
            _u = find_user(sb, _name)
            sb.api("PATCH", f"/api/v1/groups/home/members/{_u['id']}", {"role": "viewer"})
        r = sb.api("POST", "/api/v1/tickets",
                   {"type": "task", "priority": "low", "assignee": "wakebot",
                    "summary": "round-trip probe", "body": "handle + resolve"})
        tid = r["data"].get("ticket_id")
        check("ticket filed + assigned", r["status"] == 200 and tid, r["data"])
        r = sb.api("PATCH", f"/api/v1/tickets/{tid}", {"status": "in_progress"},
                   headers=as_user(tok))
        check("assignee can PATCH status", r["status"] == 200, r["data"])
        r = sb.api("PATCH", f"/api/v1/tickets/{tid}", {"priority": "high"},
                   headers=as_user(tok))
        check("assignee cannot PATCH beyond status", r["status"] == 403, r["status"])
        r = sb.api("PATCH", f"/api/v1/tickets/{tid}", {"status": "resolved"},
                   headers=as_user(tok2))
        check("non-assignee non-filer still 403", r["status"] == 403, r["status"])
        r = sb.api("PATCH", f"/api/v1/tickets/{tid}", {"status": "resolved"},
                   headers=as_user(tok))
        check("assignee resolves own ticket", r["status"] == 200, r["data"])

        print("  [5] foreground presence (doc #17 — rail defers to live sessions)")
        r = sb.api("GET", "/api/v1/presence", headers=as_user(tok))
        check("presence default false", r["status"] == 200 and r["data"].get("present") is False, r["data"])
        r = sb.api("POST", "/api/v1/presence", {"ttl_seconds": 300}, headers=as_user(tok))
        check("publish presence", r["status"] == 200 and r["data"].get("present") is True, r["data"])
        r = sb.api("GET", "/api/v1/presence", headers=as_user(tok))
        check("presence reads true while fresh", r["data"].get("present") is True, r["data"])
        r = sb.api("POST", "/api/v1/presence", {"ttl_seconds": 0}, headers=as_user(tok))
        check("ttl=0 clears", r["status"] == 200 and r["data"].get("present") is False, r["data"])
        listen2 = fetch_text(sb, "/api/v1/helpers/listen")
        check("listener: per-thread claims defer + catchup (#199)",
              "inbox_state" in listen2["text"] and "unclaimed" in listen2["text"]
              and "deferred-catchup" in listen2["text"])
        wake2 = fetch_text(sb, "/api/v1/helpers/wake")
        check("wake handler publishes + clears presence", wake2["text"].count("/presence") >= 2)
        # #206: a handler that wrote to a ticket (auto-claiming it) but exits
        # without a live watch must not leave that thread deaf to this
        # identity's own rail until TTL expiry — release_claims must run in
        # the listener's finally block around EVERY WAKE_CMD, harness-agnostic
        # (not just the default handler's own trap).
        check("listener: releases claims around WAKE_CMD, harness-agnostic (#206)",
              "release_claims" in listen2["text"]
              and "wake/claims/release" in listen2["text"])
        check("default handler: also releases claims on its own EXIT trap (#206)",
              "wake/claims/release" in wake2["text"])
        # #264 regression fix (v2.5.0/v2.4.0): once wake sessions STAMP their
        # claims with a session_id (v0.7.53), an unstamped release only deletes
        # NULL-session rows — the session's own stamped claim survived exit and
        # the presence-backstop resurrected it, stranding the thread. The
        # listener must generate the nonce, pass it into WAKE_CMD's env, and
        # release WITH it; the handler must inherit it and release with it too.
        check("listener: generates the session nonce and passes it into WAKE_CMD env",
              "SAMCLOUD_SESSION_ID" in listen2["text"]
              and "new_session_id" in listen2["text"])
        check("listener: exit-release carries the session id (deletes its own stamped claim)",
              "X-Session-Id" in listen2["text"]
              and "release_claims(token, session_id=" in listen2["text"])
        check("default handler: inherits the listener's nonce (fallback only for direct runs)",
              'SAMCLOUD_SESSION_ID:-' in wake2["text"])
        check("default handler: EXIT-trap release carries X-Session-Id",
              'X-Session-Id: $SAMCLOUD_SESSION_ID' in wake2["text"])

        print("  [6] one-voice pass (#167 agent-token presence, #162 watch-stamps, #176 listener-publish)")
        # #167: legacy fleet agent tokens carry a BARE owner_id (the username),
        # not user:N — insert one directly, matching enrollment's storage shape.
        # The username must exist as a users row (it does for the real fleet).
        mk_user(sb, "wakeuser", [])
        import hashlib as _hl, sqlite3 as _sq
        agent_tok = "sc_agent_" + "ab" * 16
        conn = _sq.connect(sb.db_path)
        conn.execute("INSERT OR REPLACE INTO tokens (id, token_hash, type, owner_id, scopes, expires_at) "
                     "VALUES ('agent:wakeuser', ?, 'agent', 'wakeuser', '[]', NULL)",
                     (_hl.sha256(agent_tok.encode()).hexdigest(),))
        conn.commit(); conn.close()
        r = sb.api("POST", "/api/v1/presence", {"ttl_seconds": 120}, headers=as_user(agent_tok))
        check("bare-owner_id agent token publishes presence (#167)",
              r["status"] == 200 and r["data"].get("present") is True, r["data"])
        r = sb.api("GET", "/api/v1/presence", headers=as_user(agent_tok))
        check("agent-token presence reads back true", r["data"].get("present") is True, r["data"])
        sb.api("POST", "/api/v1/presence", {"ttl_seconds": 0}, headers=as_user(agent_tok))
        # #162: a held watch IS presence — server stamps it, no client cooperation.
        r = sb.api("GET", "/api/v1/notifications/watch?timeout=1", headers=as_user(agent_tok))
        check("watch returns (timeout path)", r["status"] == 200, r["status"])
        r = sb.api("GET", "/api/v1/presence", headers=as_user(agent_tok))
        check("watch stamped presence server-side (#162)",
              r["data"].get("present") is True, r["data"])
        # extend-only: an explicit longer presence isn't shortened by a watch
        sb.api("POST", "/api/v1/presence", {"ttl_seconds": 7200}, headers=as_user(agent_tok))
        before = sb.api("GET", "/api/v1/presence", headers=as_user(agent_tok))["data"].get("until")
        sb.api("GET", "/api/v1/notifications/watch?timeout=1", headers=as_user(agent_tok))
        after = sb.api("GET", "/api/v1/presence", headers=as_user(agent_tok))["data"].get("until")
        check("watch never shortens explicit presence", after == before, (before, after))
        sb.api("POST", "/api/v1/presence", {"ttl_seconds": 0}, headers=as_user(agent_tok))
        # #176: presence-publish lives in the LISTENER around WAKE_CMD
        check("listener owns presence publish around WAKE_CMD (#176)",
              "publish_presence" in listen2["text"]
              and "finally" in listen2["text"]
              and listen2["text"].count("publish_presence") >= 3)

        print("  [7] fleet registration + self-update contract (doc #22 step 1)")
        # Served artifacts must carry parseable version stamps — the whole
        # update mechanism hangs off these two lines.
        import re as _re
        lv = _re.search(r'^RAIL_VERSION = "([^"]+)"', listen2["text"], _re.M)
        hv = _re.search(r'^WAKE_VERSION="([^"]+)"', wake2["text"], _re.M)
        check("listener carries RAIL_VERSION stamp", bool(lv))
        check("handler carries WAKE_VERSION stamp", bool(hv))
        check("listener registers on connect + self-updates",
              "/wake/register" in listen2["text"]
              and "self_update" in listen2["text"]
              and "os.execv" in listen2["text"])
        r = sb.api("POST", "/api/v1/wake/register",
                   {"wake_cmd": "/home/you/.local/bin/samcloud-wake",
                    "host": "testhost", "listener_version": lv.group(1) if lv else "0",
                    "handler_version": hv.group(1) if hv else "0",
                    "api_env": "http://sandbox"},
                   headers=as_user(agent_tok))
        check("register accepts agent token", r["status"] == 200, r["data"])
        check("current versions match served artifacts",
              r["data"].get("current_versions", {}).get("listen") == (lv.group(1) if lv else None)
              and r["data"].get("current_versions", {}).get("wake") == (hv.group(1) if hv else None),
              r["data"])
        check("up-to-date rail: update_available false",
              r["data"].get("update_available") is False, r["data"])
        r = sb.api("POST", "/api/v1/wake/register",
                   {"wake_cmd": "/home/you/.local/bin/samcloud-wake",
                    "host": "testhost", "listener_version": "0.0.1",
                    "handler_version": hv.group(1) if hv else "0",
                    "api_env": "http://sandbox"},
                   headers=as_user(agent_tok))
        check("stale listener: update_available true",
              r["data"].get("update_available") is True, r["data"])
        r = sb.api("POST", "/api/v1/wake/register",
                   {"wake_cmd": "hermes-wake --inbox", "host": "otherhost",
                    "listener_version": lv.group(1) if lv else "0",
                    "api_env": "http://sandbox"},
                   headers=as_user(agent_tok))
        check("custom harness (null handler_version) never flagged stale",
              r["status"] == 200 and r["data"].get("update_available") is False,
              r["data"])
        # Fleet view: admin sees all rows; upsert means one row per (user, host).
        r = sb.api("GET", "/api/v1/wake/registrations")
        regs = r["data"].get("registrations", [])
        mine = [x for x in regs if x.get("host") == "testhost"]
        check("fleet view lists registration with username join",
              len(mine) == 1 and mine[0].get("username") == "wakeuser",
              [(x.get("host"), x.get("username")) for x in regs])
        check("upsert kept one row per (user,host), latest versions win",
              mine and mine[0].get("listener_version") == "0.0.1", mine)
        check("fleet view computes staleness", mine and mine[0].get("stale") is True, mine)
        # Non-admin sees only their own registrations.
        other_tok = mk_user(sb, "otheruser", [])
        r = sb.api("GET", "/api/v1/wake/registrations", headers=as_user(other_tok))
        check("non-admin sees only own rows",
              r["status"] == 200 and r["data"].get("registrations") == [], r["data"])

        # #193 (same shape as #167): bare-owner_id agent tokens must stamp
        # last_seen on inbox reads, or the liveness metric is blind for
        # exactly the identities the rail serves.
        sb.api("GET", "/api/v1/notifications", headers=as_user(agent_tok))
        u = find_user(sb, "wakeuser")
        check("agent-token inbox read stamps last_seen (#193)",
              u and u.get("last_seen"), u)

        print("  [8] thread claims — live surface owns its threads (#199)")
        r = sb.api("POST", "/api/v1/tickets",
                   {"type": "question", "priority": "low",
                    "summary": "claims contract probe", "body": "x",
                    "assignee": "wakeuser"})
        ctid = r["data"].get("ticket_id")
        check("probe ticket filed", bool(ctid), r["data"])
        # Admin filed it -> admin holds a claim (writing auto-claims).
        r = sb.api("GET", "/api/v1/wake/claims?all=true")
        admins = [c for c in r["data"].get("claims", []) if c["ticket_id"] == ctid]
        check("filing auto-claims the thread for the filer", len(admins) == 1, r["data"])
        # wakeuser comments -> claims it for wakeuser too.
        sb.api("POST", f"/api/v1/tickets/{ctid}/comments", {"body": "on it"},
               headers=as_user(agent_tok))
        r = sb.api("GET", "/api/v1/wake/claims", headers=as_user(agent_tok))
        check("commenting auto-claims for the commenter",
              any(c["ticket_id"] == ctid for c in r["data"].get("claims", [])),
              r["data"])
        # Admin replies -> wakeuser gets a notification row stamped claimed=true.
        sb.api("POST", f"/api/v1/tickets/{ctid}/comments", {"body": "reply for the warm surface"})
        r = sb.api("GET", "/api/v1/notifications?unread=true", headers=as_user(agent_tok))
        rows = [n for n in r["data"]["notifications"] if n.get("ticket_id") == ctid]
        check("reply on a claimed thread carries claimed=true",
              rows and rows[0].get("claimed") is True, rows)
        # The cold predicate must NOT see it...
        r = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                   headers=as_user(agent_tok))
        check("?unclaimed=true hides claimed work from the cold rail",
              not any(n.get("ticket_id") == ctid for n in r["data"]["notifications"]),
              r["data"])
        # ...until the claim is released — then the rail owns it instantly.
        sb.api("POST", "/api/v1/wake/claims/release", {}, headers=as_user(agent_tok))
        r = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                   headers=as_user(agent_tok))
        check("release hands the thread to the cold rail instantly",
              any(n.get("ticket_id") == ctid for n in r["data"]["notifications"]),
              r["data"])
        # Explicit claim + refresh extends; DELETE releases one thread.
        sb.api("POST", f"/api/v1/tickets/{ctid}/claim", {"ttl_seconds": 60},
               headers=as_user(agent_tok))
        before = [c for c in sb.api("GET", "/api/v1/wake/claims",
                  headers=as_user(agent_tok))["data"]["claims"] if c["ticket_id"] == ctid]
        sb.api("POST", "/api/v1/wake/claims/refresh", {"ttl_seconds": 1800},
               headers=as_user(agent_tok))
        after = [c for c in sb.api("GET", "/api/v1/wake/claims",
                 headers=as_user(agent_tok))["data"]["claims"] if c["ticket_id"] == ctid]
        check("refresh extends an unexpired claim",
              before and after and after[0]["claimed_until"] > before[0]["claimed_until"],
              (before, after))
        r = sb.api("DELETE", f"/api/v1/tickets/{ctid}/claim", headers=as_user(agent_tok))
        check("per-thread release works", r["status"] == 200, r["data"])

        print("  [8b] notification lifecycle — seen leases, acked_via, sticky claims (#227/#229)")
        # Payload body fidelity (2026-07-29): the fan-out used a hard body[:200]
        # slice, so a woken agent — which reads the payload AS the message —
        # got long comments clipped mid-sentence and acted on partial text
        # (stranded #277). A normal comment must arrive WHOLE; an over-long one
        # is truncated only with an explicit marker, never a silent cut.
        long_body = "SENTINEL_START " + ("x " * 260) + "SENTINEL_END"  # ~540 chars
        sb.api("POST", f"/api/v1/tickets/{ctid}/comments", {"body": long_body})
        prow = sb.api("GET", f"/api/v1/notifications?unread=true&ticket_id={ctid}",
                      headers=as_user(agent_tok))["data"]["notifications"][0]
        pbody = prow.get("payload", {}).get("body", "")
        check("notification payload carries a >200-char comment whole (no silent 200-cut)",
              "SENTINEL_END" in pbody and len(pbody) > 200, len(pbody))
        mid_body = "short reply, well under the cap"
        sb.api("POST", f"/api/v1/tickets/{ctid}/comments", {"body": mid_body})
        mrow = sb.api("GET", f"/api/v1/notifications?unread=true&ticket_id={ctid}",
                      headers=as_user(agent_tok))["data"]["notifications"][0]
        check("normal comment arrives verbatim, no truncation marker",
              mrow.get("payload", {}).get("body", "") == mid_body,
              mrow.get("payload", {}).get("body", ""))
        # Fresh unread row for the agent: admin comments on the subscribed thread.
        sb.api("POST", f"/api/v1/tickets/{ctid}/comments", {"body": "lifecycle probe"})
        rows = sb.api("GET", f"/api/v1/notifications?unread=true&ticket_id={ctid}",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("probe row delivered unread", bool(rows), rows)
        rid = rows[0]["id"]
        # Seen lease: cold rail skips while fresh...
        r = sb.api("POST", "/api/v1/notifications/seen",
                   {"ids": [rid], "ttl_seconds": 2}, headers=as_user(agent_tok))
        check("seen lease accepted", r["status"] == 200, r["data"])
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("seen row hidden from cold rail while lease fresh",
              not any(n["id"] == rid for n in cold), cold)
        # ...and returns to cold eligibility when the lease lapses, still unread.
        time.sleep(3)
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("lapsed seen lease returns row to cold rail (no display-ack loss)",
              any(n["id"] == rid for n in cold), cold)
        # Ack with surface -> acked_via stored and reported.
        sb.api("POST", "/api/v1/notifications/ack",
               {"ids": [rid], "surface": "wake"}, headers=as_user(agent_tok))
        got = sb.api("GET", f"/api/v1/notifications?unread=false&ticket_id={ctid}",
                     headers=as_user(agent_tok))["data"]["notifications"]
        mine = [n for n in got if n["id"] == rid]
        check("ack stores acked_via for honest attribution",
              mine and mine[0].get("read") and mine[0].get("acked_via") == "wake", mine)
        # Sticky claims: explicit claim survives a default (wake-exit) release...
        sb.api("POST", f"/api/v1/tickets/{ctid}/claim", {"ttl_seconds": 300},
               headers=as_user(agent_tok))
        sb.api("POST", "/api/v1/wake/claims/release", {}, headers=as_user(agent_tok))
        left = sb.api("GET", "/api/v1/wake/claims",
                      headers=as_user(agent_tok))["data"]["claims"]
        check("default release spares sticky claims (#229 twin race)",
              any(c["ticket_id"] == ctid for c in left), left)
        # ...and falls to an explicit include_sticky release.
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers=as_user(agent_tok))
        left = sb.api("GET", "/api/v1/wake/claims",
                      headers=as_user(agent_tok))["data"]["claims"]
        check("include_sticky release clears everything",
              not any(c["ticket_id"] == ctid for c in left), left)
        # Re-assertion: refresh with reassert_ticket_ids resurrects the claim sticky.
        r = sb.api("POST", "/api/v1/wake/claims/refresh",
                   {"ttl_seconds": 600, "reassert_ticket_ids": [ctid],
                    "reassert_ttl_seconds": 300}, headers=as_user(agent_tok))
        check("reassert creates sticky claim (converges to listening surface)",
              ctid in (r["data"].get("reasserted") or [])
              and any(c["ticket_id"] == ctid and c.get("sticky")
                      for c in r["data"].get("active_claims", [])), r["data"])
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers=as_user(agent_tok))

        print("  [8d] session-scoped claims — live sessions exclude each other (#264)")
        # Two surfaces of ONE identity (wakeuser): session A vs session B.
        hdr_a = dict(as_user(agent_tok), **{"X-Session-Id": "sess-A-nonce"})
        hdr_b = dict(as_user(agent_tok), **{"X-Session-Id": "sess-B-nonce"})
        r = sb.api("POST", "/api/v1/tickets",
                   {"type": "question", "priority": "low",
                    "summary": "session exclusion probe", "body": "x",
                    "assignee": "wakeuser"}, headers=hdr_a)
        stid = r["data"].get("ticket_id")
        check("session A files -> owns the claim",
              r["data"].get("claim_owned") is True, r["data"])
        rows = sb.api("GET", "/api/v1/wake/claims",
                      headers=as_user(agent_tok))["data"]["claims"]
        mine = [c for c in rows if c["ticket_id"] == stid]
        check("claim row carries a session fingerprint (not the raw nonce)",
              mine and mine[0].get("session") and len(mine[0]["session"]) == 8
              and "sess-A-nonce" not in str(mine[0]), mine)
        # Session B writes -> comment lands, but claim is NOT hijacked.
        r = sb.api("POST", f"/api/v1/tickets/{stid}/comments",
                   {"body": "second surface butting in"}, headers=hdr_b)
        check("session B's comment still lands (content never blocked)",
              r["status"] == 200, r["data"])
        check("...but B does NOT own the claim (claim_owned=false + held hint)",
              r["data"].get("claim_owned") is False
              and "Another conversation of yours" in r["data"].get("watch", ""), r["data"])
        # Explicit claim attempt from B -> hard 409.
        r = sb.api("POST", f"/api/v1/tickets/{stid}/claim", {"ttl_seconds": 60},
                   headers=hdr_b)
        check("explicit claim from a second live session 409s", r["status"] == 409, r)
        # Legacy writer (no session header) can't dilute a session-owned claim.
        r = sb.api("POST", f"/api/v1/tickets/{stid}/comments",
                   {"body": "legacy surface"}, headers=as_user(agent_tok))
        check("legacy (headerless) write doesn't co-own a session-held thread",
              r["data"].get("claim_owned") is False, r["data"])
        # B's exit release must not clobber A's live claim...
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers=hdr_b)
        rows = sb.api("GET", "/api/v1/wake/claims",
                      headers=as_user(agent_tok))["data"]["claims"]
        check("another session's release spares the owner's claim",
              any(c["ticket_id"] == stid for c in rows), rows)
        # ...while A's own release hands it back to the rail.
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers=hdr_a)
        rows = sb.api("GET", "/api/v1/wake/claims",
                      headers=as_user(agent_tok))["data"]["claims"]
        check("owner's release clears its session claim",
              not any(c["ticket_id"] == stid for c in rows), rows)
        # Dead surface: expired session claim is taken over, not respected.
        sb.api("POST", f"/api/v1/tickets/{stid}/claim", {"ttl_seconds": 1},
               headers=hdr_a)
        time.sleep(2)
        r = sb.api("POST", f"/api/v1/tickets/{stid}/claim", {"ttl_seconds": 60},
                   headers=hdr_b)
        check("expired session claim is taken over by the next live session",
              r["status"] == 200, r)
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers=hdr_b)
        # Differentiation on the record: the two comments carry distinct fps.
        cmts = sb.api("GET", f"/api/v1/tickets/{stid}")["data"].get("comments", [])
        fps = [c.get("session_fp") for c in cmts]
        check("comments carry distinct session fingerprints (+ null for legacy)",
              len([f for f in fps if f]) >= 1 and None in fps
              and len(set(f for f in fps if f)) >= 1, fps)

        print("  [8c] presence backstops the claim TTL — the double-wake fix (#266)")
        # Re-claim the thread and drop a fresh unread reply on it.
        sb.api("POST", f"/api/v1/tickets/{ctid}/claim", {"ttl_seconds": 300},
               headers=as_user(agent_tok))
        sb.api("POST", f"/api/v1/tickets/{ctid}/comments", {"body": "backstop probe reply"})
        # Age the claim to LOOK expired — simulating a long interactive turn (no
        # watch loop to refresh) or a self-inflicted registry restart that forced
        # a reconnect-reconcile mid-turn. This is the exact state that used to
        # cold-wake a parallel session of the same identity.
        import sqlite3 as _sq3
        conn = _sq3.connect(sb.db_path)
        conn.execute("UPDATE thread_claims SET claimed_until = ? "
                     "WHERE identity = 'wakeuser' AND ticket_id = ?",
                     ("2000-01-01T00:00:00+00:00", ctid))
        conn.commit(); conn.close()
        # Baseline (pre-fix behaviour): expired claim + NOT present -> cold-eligible.
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("expired claim + no presence -> thread IS cold-eligible (the bug's state)",
              any(n.get("ticket_id") == ctid for n in cold), cold)
        # The fix: the notif hook's presence heartbeat backstops the expired claim.
        sb.api("POST", "/api/v1/presence", {"ttl_seconds": 300}, headers=as_user(agent_tok))
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("presence backstops the expired claim -> hidden from the cold rail (#266)",
              not any(n.get("ticket_id") == ctid for n in cold), cold)
        disp = sb.api("GET", f"/api/v1/notifications?unread=true&ticket_id={ctid}",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("annotated feed marks the backstopped row claimed=true",
              disp and all(n.get("claimed") for n in disp), disp)
        # Per-thread, NOT a global defer (#199 preserved): a thread the surface
        # never claimed stays cold-eligible even while present.
        utid = sb.api("POST", "/api/v1/tickets",
                      {"type": "question", "priority": "low",
                       "summary": "unclaimed-while-present", "body": "x",
                       "assignee": "wakeuser"})["data"].get("ticket_id")
        sb.api("POST", f"/api/v1/tickets/{utid}/comments",
               {"body": "reply on a thread wakeuser never claimed"})
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("presence does NOT global-defer: unclaimed thread still cold-eligible (#199)",
              any(n.get("ticket_id") == utid for n in cold), cold)
        # Presence lapses -> the aged claim returns to the cold rail (bounded handover).
        sb.api("POST", "/api/v1/presence", {"ttl_seconds": 0}, headers=as_user(agent_tok))
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("presence lapse -> aged claim returns to the cold rail",
              any(n.get("ticket_id") == ctid for n in cold), cold)
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers=as_user(agent_tok))

        print("  [8e] claims cover doc + task threads (#392)")
        # Doc plane: a live surface commenting a doc claims it — its own cold
        # rail must leave that thread alone (the doc-73 regression).
        cdid = sb.api("POST", "/api/v1/docs",
                      {"title": "claim-plane probe", "body": "x"})["data"]["doc_id"]
        # Watch deliberately; the comment below tests claiming, not enrolment.
        sb.api("POST", f"/api/v1/docs/{cdid}/subscribe", headers=as_user(agent_tok))
        r = sb.api("POST", f"/api/v1/docs/{cdid}/comments", {"body": "working this doc"},
                   headers=as_user(agent_tok))
        check("doc comment auto-claims the doc thread",
              r["data"].get("claim_owned") is True, r["data"])
        claims = sb.api("GET", "/api/v1/wake/claims",
                        headers=as_user(agent_tok))["data"]["claims"]
        check("doc claim visible with doc_id set",
              any(c.get("doc_id") == cdid for c in claims), claims)
        sb.api("POST", f"/api/v1/docs/{cdid}/comments", {"body": "admin reply on doc"})
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("claimed doc thread hidden from the cold rail",
              not any(n.get("doc_id") == cdid for n in cold), cold)
        sb.api("DELETE", f"/api/v1/docs/{cdid}/claim", headers=as_user(agent_tok))
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("doc claim release hands the thread to the cold rail",
              any(n.get("doc_id") == cdid for n in cold), cold)
        # Task plane: PATCH claims the deliverable — the exact #392 race shape.
        cslug = sb.api("POST", "/api/v1/projects",
                       {"name": "claim-plane proj"})["data"]["slug"]
        ctkid = sb.api("POST", f"/api/v1/projects/{cslug}/tasks",
                       {"title": "raced deliverable",
                        "assignee": "wakeuser"})["data"]["task_id"]
        r = sb.api("PATCH", f"/api/v1/tasks/{ctkid}", {"status": "in_progress"},
                   headers=as_user(agent_tok))
        check("task PATCH auto-claims the task thread",
              r["data"].get("claim_owned") is True, r["data"])
        sb.api("POST", f"/api/v1/tasks/{ctkid}/comments", {"body": "admin q on task"})
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("claimed task thread hidden from the cold rail",
              not any(n.get("task_id") == ctkid for n in cold), cold)
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers=as_user(agent_tok))
        cold = sb.api("GET", "/api/v1/notifications?unread=true&unclaimed=true",
                      headers=as_user(agent_tok))["data"]["notifications"]
        check("identity-wide release covers every plane",
              any(n.get("task_id") == ctkid for n in cold), cold)

        print("  [8f] holder read — who else is in this row (#529)")
        # The wire shape the finding was measured on: a plain seat asking the
        # fleet question got its OWN rows back, 200, no scope marker, every
        # row carrying an `identity` that always said *you*.
        htid = sb.api("POST", "/api/v1/tickets",
                      {"type": "bug", "priority": "low",
                       "summary": "holder-read probe", "body": "x"},
                      headers={"X-Session-Id": "admin-surface"})["data"]["ticket_id"]
        sb.api("POST", f"/api/v1/tickets/{htid}/comments", {"body": "also working this"},
               headers={**as_user(agent_tok), "X-Session-Id": "agent-surface"})
        r = sb.api("GET", "/api/v1/wake/claims?all=true", headers=as_user(agent_tok))
        check("non-admin ?all=true is REFUSED, not silently narrowed",
              r["status"] == 403 and "?thread=" in str(r["data"]), r)
        r = sb.api("GET", "/api/v1/wake/claims", headers=as_user(agent_tok))
        check("self-scope still served, and names its own scope",
              r["status"] == 200 and r["data"].get("scope") == "self", r["data"])
        r = sb.api("GET", f"/api/v1/wake/claims?thread=ticket:{htid}",
                   headers=as_user(agent_tok))
        holders = {h["identity"]: h for h in r["data"].get("holders", [])}
        check("?thread= shows a plain seat the OTHER identity in the row",
              r["status"] == 200 and set(holders) == {"admin", "wakeuser"}, r["data"])
        check("holder rows carry session FINGERPRINTS, never the nonce",
              "admin-surface" not in json.dumps(r["data"])
              and holders.get("admin", {}).get("session"), r["data"])
        check("holder read states it is advisory, not a lock",
              r["data"].get("advisory") is True and "not a lock" in r["data"].get("note", ""),
              r["data"])
        r = sb.api("GET", "/api/v1/wake/claims?thread=ticket:nope",
                   headers=as_user(agent_tok))
        check("malformed ?thread= is 400, not an ignored param", r["status"] == 400, r)
        # The hint on the read a composing seat actually performs.
        t = sb.api("GET", f"/api/v1/tickets/{htid}", headers=as_user(agent_tok))["data"]
        check("GET /tickets/{id} carries the holder hint",
              t.get("claims", {}).get("advisory") is True
              and any(h["identity"] == "admin" for h in t["claims"]["holders"]),
              t.get("claims"))
        check("the caller's own row is marked self=true",
              any(h["identity"] == "wakeuser" and h["self"] is True
                  for h in t["claims"]["holders"]), t.get("claims"))
        # c3977, replayed exactly as it was measured on prod: release, PATCH,
        # then read the claims back. That sequence used to return ZERO rows —
        # ticket PATCH was the one mutating route on any plane that claimed
        # nothing and did not even accept the nonce header, so triage (the
        # main verb here) left no trace for any holder hint to show.
        sb.api("POST", "/api/v1/wake/claims/release", {"include_sticky": True},
               headers={"X-Session-Id": "admin-surface"})
        holders = sb.api("GET", f"/api/v1/wake/claims?thread=ticket:{htid}",
                         headers=as_user(agent_tok))["data"]["holders"]
        check("release drops the releasing session out of the row",
              not any(h["identity"] == "admin" for h in holders), holders)
        r = sb.api("PATCH", f"/api/v1/tickets/{htid}", {"priority": "high"},
                   headers={"X-Session-Id": "admin-surface"})
        check("ticket PATCH now claims its thread (triage is work)",
              r["status"] == 200 and r["data"].get("claim_owned") is True, r["data"])
        holders = sb.api("GET", f"/api/v1/wake/claims?thread=ticket:{htid}",
                         headers=as_user(agent_tok))["data"]["holders"]
        check("the triaging seat is visible to every OTHER seat",
              any(h["identity"] == "admin" for h in holders), holders)

        print("  [9] the interactive hooks are retired (0.10.0): a session hears through the subscribe helper")
        for _h in ("notif-hook", "stop-hook", "hooks-install"):
            _r = fetch_text(sb, f"/api/v1/helpers/{_h}")
            check(f"{_h} is no longer served", _r["status"] == 404, _r["status"])
        _sub = fetch_text(sb, "/api/v1/helpers/subscribe")
        check("the subscribe helper is served and takes the harness session id when nothing else names the conversation",
              _sub["status"] == 200 and "CLAUDE_CODE_SESSION_ID" in _sub["text"], _sub["status"])
        _idx = sb.api("GET", "/api/v1/", headers=as_user(agent_tok))["data"]
        _paths = {v.get("path") for g in _idx.get("endpoints", {}).values() if isinstance(g, dict) for v in g.values() if isinstance(v, dict)}
        check("the API map no longer lists claims, seen, or the v2 wake helpers",
              not any(p_ for p_ in _paths if "/claim" in p_ or p_.endswith("/seen") or p_.endswith("/helpers/wake") or p_.endswith("/wake-prompt")),
              sorted(p_ for p_ in _paths if "/claim" in p_ or "seen" in p_ or "helpers/wake" in p_))
        check("the API map lists every served helper a skill points at",
              all(f"/api/v1/helpers/{h}" in _paths for h in ("rail", "listen", "wake-install", "say", "subscribe", "wsl-boot")),
              sorted(p_ for p_ in _paths if "/helpers/" in p_))

        # #675: every served executable must PARSE. The Stop hook shipped at
        # 2.5.0 with an apostrophe inside a single-quoted `python3 -c '…'`
        # block ("threads you're in"), which closed the shell string and left
        # the file syntactically broken. It was served, installed, and reported
        # healthy by the fleet view for a full day, because a version stamp is
        # read as TEXT — grepping a file's contents proves nothing about
        # whether it can run. This suite greps these files; it would not have
        # caught it either, until now.
        import subprocess as _sp, tempfile as _tf, os as _os
        for _name, _art, _lang in (("wake", wake, "bash"),
                                   ("say", fetch_text(sb, "/api/v1/helpers/say"), "bash"),
                                   ("rail", fetch_text(sb, "/api/v1/helpers/rail"), "python"),
                                   ("listen", fetch_text(sb, "/api/v1/helpers/listen"), "python"),
                                   ("subscribe", fetch_text(sb, "/api/v1/helpers/subscribe"), "python")):
            _text = (_art or {}).get("text") or ""
            if not _text:
                check(f"served {_name} is non-empty", False, "empty body")
                continue
            _fd, _path = _tf.mkstemp(suffix=".src")
            with _os.fdopen(_fd, "w") as _f:
                _f.write(_text)
            if _lang == "bash":
                _rc = _sp.run(["bash", "-n", _path], capture_output=True, text=True)
                _ok, _why = _rc.returncode == 0, _rc.stderr.strip()[:160]
            else:
                try:
                    compile(_text, _name, "exec")
                    _ok, _why = True, ""
                except SyntaxError as _e:
                    _ok, _why = False, f"{_e.msg} (line {_e.lineno})"
            _os.unlink(_path)
            check(f"served {_name} parses ({_lang})", _ok, _why)
        r = sb.api("POST", "/api/v1/wake/register",
                   {"wake_cmd": "/home/you/.local/bin/samcloud-wake", "host": "hooked",
                    "listener_version": lv.group(1) if lv else "0",
                    "handler_version": hv.group(1) if hv else "0",
                    "notif_hook_version": "2.7.2", "stop_hook_version": "3.0.0",
                    "api_env": "http://sandbox"},
                   headers=as_user(agent_tok))
        check("register still accepts hook versions from older listeners and ignores them: current rail = not stale (0.10.0)",
              r["status"] == 200 and r["data"].get("update_available") is False,
              r["data"])
        regs = sb.api("GET", "/api/v1/wake/registrations")["data"]["registrations"]
        hooked = [x for x in regs if x.get("host") == "hooked"]
        check("fleet view: hook versions no longer make a row stale, and surface_dark is retired (always False)",
              hooked and hooked[0].get("stale") is False and hooked[0].get("surface_dark") is False,
              hooked)

        print("  [9c] WSL persistence primitive (#231)")
        wb = fetch_text(sb, "/api/v1/helpers/wsl-boot")
        check("wsl-boot helper served", wb["status"] == 200, wb["status"])
        wv = _re.search(r"^# WSL_BOOT_VERSION: (\S+)", wb["text"], _re.M)
        check("wsl-boot carries version stamp", bool(wv))
        check("wsl-boot pins the distro + self-restarts on bounce",
              "sleep infinity" in wb["text"] and "exit 1" in wb["text"]
              and "S4U" in wb["text"])
        check("wake-install deploys wsl-boot on WSL hosts",
              "helpers/wsl-boot" in inst["text"] and "SamcloudWSLBoot" in inst["text"]
              and "WSL_BOOT_PATH" in inst["text"])
        check("listener self-updates deployed wsl-boot in place",
              "WSL_BOOT_PATH" in listen2["text"] and "wsl-boot" in listen2["text"])

        # ── [9d] the installer installs the v3 rail (#700) ──────────────────
        # Every live v3 seat was seeded by hand while the served installer went on
        # writing a v2 wake.env and fetching the v2 handler; claude-gpu-1 was found on
        # it, wet, firing one-shots while the fleet was parked. This runs the SERVED
        # installer for real in a throwaway HOME (preflight skipped, nothing started)
        # and reads back what a seat would have, then asks the served listener's own
        # handler detection what it would report on its first connect.
        print("  [9d] installer installs the v3 rail (#700)")
        import shutil as _sh
        seat_tok = mk_user(sb, "seat-install", [])
        rail_srv = fetch_text(sb, "/api/v1/helpers/rail")["text"]
        rail_srv_ver = _re.search(r'^RAIL_VERSION = "([^"]+)"', rail_srv, _re.M).group(1)
        def _run_installer(home, env_extra):
            _os.makedirs(f"{home}/.samcloud", exist_ok=True)
            with open(f"{home}/.samcloud/token", "w") as _f:
                _f.write(seat_tok + "\n")
            _os.chmod(f"{home}/.samcloud/token", 0o600)
            _fd, _sp_path = _tf.mkstemp(suffix=".sh")
            with _os.fdopen(_fd, "w") as _f:
                _f.write(inst["text"])
            env = {"HOME": home, "PATH": _os.environ.get("PATH", "/usr/bin:/bin"),
                   "USER": _os.environ.get("USER", "seat"), "SKIP_PREFLIGHT": "1",
                   "NO_SUPERVISE": "1", **env_extra}
            r = _sp.run(["bash", _sp_path], env=env, capture_output=True, text=True, timeout=120)
            _os.unlink(_sp_path)
            return r
        home1 = _tf.mkdtemp(prefix="samcloud-seat-")
        r1 = _run_installer(home1, {"RAIL_DRY": "1", "MODEL": "sonnet"})
        check("installer runs clean against the sandbox", r1.returncode == 0,
              (r1.stdout + r1.stderr)[-400:])
        conf1 = open(f"{home1}/.config/samcloud/wake.env").read() if _os.path.exists(f"{home1}/.config/samcloud/wake.env") else ""
        rail_path = f"{home1}/.local/bin/samcloud-rail.prod"
        check("installer fetches the rail at the stamped name the listener maintains",
              _os.path.exists(rail_path)
              and _re.search(r'^RAIL_VERSION = "' + _re.escape(rail_srv_ver) + '"', open(rail_path).read(), _re.M) is not None,
              rail_path)
        check("installer fetches the listener and sc-say",
              _os.path.exists(f"{home1}/.local/bin/samcloud-listen")
              and "SAY_VERSION" in open(f"{home1}/.local/bin/sc-say").read()[:200])
        want = ("RAIL_V3=1", f'WAKE_CMD="{home1}/.local/bin/samcloud-rail.prod"', "RAIL_HOLDER=stream",
                f"RAIL_CWD={home1}", f"RAIL_STATE_DIR={home1}/.samcloud/rail-v3-prod",
                "RAIL_TOKEN_MINT_URL=", f"RAIL_MINT_TOKEN_PATH={home1}/.samcloud/token",
                "MODEL=sonnet", "RAIL_DRY=1")
        check("installer writes a v3 wake.env (parked when RAIL_DRY=1 at install)",
              all(w in conf1 for w in want) and "samcloud-wake" not in conf1,
              [w for w in want if w not in conf1])
        check("installer builds the header file 0600 and no staging header",
              _os.path.exists(f"{home1}/.samcloud/token.hdr")
              and (_os.stat(f"{home1}/.samcloud/token.hdr").st_mode & 0o777) == 0o600
              and not _os.path.exists(f"{home1}/.samcloud/stg-token.hdr"))
        check("installer seeds the ground truth and stages supervision without starting it",
              "samcloud:ground-truth" in open(f"{home1}/.claude/CLAUDE.md").read()
              and any("samcloud-listen" in open(_os.path.join(dp, fn)).read()
                      for dp, _, fns in _os.walk(f"{home1}/.config") for fn in fns
                      if fn.endswith((".plist", ".service")))
              and "nothing started" in r1.stdout, r1.stdout[-300:])
        # what the served listener would report on its first connect from this seat
        probe = _sp.run(["python3", "-c",
                         "import importlib.machinery as m, sys, json;"
                         "l = m.SourceFileLoader('l', sys.argv[1]).load_module();"
                         "print(json.dumps(l._handler_spec()))",
                         f"{home1}/.local/bin/samcloud-listen"],
                        env={"HOME": home1, "PATH": _os.environ.get("PATH", "/usr/bin:/bin"),
                             "SAMCLOUD_WAKE_CONF": f"{home1}/.config/samcloud/wake.env"},
                        capture_output=True, text=True, timeout=60)
        spec = None
        try:
            spec = json.loads(probe.stdout.strip().splitlines()[-1])
        except Exception:
            pass
        check("listener would report the rail (handler_version parses as a rail version)",
              bool(spec) and spec[3] == "rail" and spec[1] == rail_srv_ver,
              (probe.stdout + probe.stderr)[-300:])
        # a v2 seat re-running the installer is upgraded in place, with a backup
        home2 = _tf.mkdtemp(prefix="samcloud-seat-")
        _os.makedirs(f"{home2}/.config/samcloud", exist_ok=True)
        with open(f"{home2}/.config/samcloud/wake.env", "w") as _f:
            _f.write(f"API={sb.base_url}/api/v1\nTOKEN_PATH={home2}/.samcloud/token\n"
                     f'LAUNCHER="claude -p"\nMODEL=opus\nMAX_SESSION_MIN=30\n'
                     f'WAKE_CMD="{home2}/.local/bin/samcloud-wake"\n')
        r2 = _run_installer(home2, {})
        conf2 = open(f"{home2}/.config/samcloud/wake.env").read()
        baks = [fn for fn in _os.listdir(f"{home2}/.config/samcloud") if fn.startswith("wake.env.pre-v3-")]
        check("installer upgrades a v2 wake.env in place, keeps its other lines, backs it up",
              r2.returncode == 0 and baks and "RAIL_V3=1" in conf2
              and f'WAKE_CMD="{home2}/.local/bin/samcloud-rail.prod"' in conf2
              and 'LAUNCHER="claude -p"' in conf2 and "samcloud-wake" not in conf2
              and "upgraded" in r2.stdout, (r2.stdout + r2.stderr)[-300:])
        # a seat whose holders run in a dedicated cwd (the operator's own account, doc 180):
        # the ground truth goes to $RAIL_CWD/CLAUDE.md and ~/.claude/CLAUDE.md is never touched
        home3 = _tf.mkdtemp(prefix="samcloud-seat-")
        _os.makedirs(f"{home3}/.config/samcloud", exist_ok=True)
        _os.makedirs(f"{home3}/.claude", exist_ok=True)
        with open(f"{home3}/.claude/CLAUDE.md", "w") as _f:
            _f.write("# the operator's own memory file\n")
        with open(f"{home3}/.config/samcloud/wake.env", "w") as _f:
            _f.write(f"API={sb.base_url}/api/v1\nTOKEN_PATH={home3}/.samcloud/token\nMODEL=opus\n"
                     f'WAKE_CMD="{home3}/.local/bin/samcloud-rail.prod"\nRAIL_V3=1\nRAIL_HOLDER=stream\n'
                     f"RAIL_CWD={home3}/holder\nRAIL_STATE_DIR={home3}/.samcloud/rail-v3-prod\n")
        r3 = _run_installer(home3, {})
        check("installer seeds the ground truth where holders run and leaves the account's own CLAUDE.md alone",
              r3.returncode == 0
              and open(f"{home3}/.claude/CLAUDE.md").read() == "# the operator's own memory file\n"
              and _os.path.exists(f"{home3}/holder/CLAUDE.md")
              and "samcloud:ground-truth" in open(f"{home3}/holder/CLAUDE.md").read()
              and "keeping existing" in r3.stdout, (r3.stdout + r3.stderr)[-300:])
        # a stale v5 ground truth (the fleet's, 2026-09-08: it names one seat "hermes") with
        # nothing appended is replaced whole — 0.10.4 printed "[ok] updated" and left it in place
        home4 = _tf.mkdtemp(prefix="samcloud-seat-")
        _os.makedirs(f"{home4}/.claude", exist_ok=True)
        with open(f"{home4}/.claude/CLAUDE.md", "w") as _f:
            _f.write("<!-- samcloud:ground-truth v5 — installed by wake-install (#240). -->\n# You are hermes\n"
                     "- old doctrine\nAn admin may append a charter section below this stamped block — it is part of your ground truth too.\n")
        r4 = _run_installer(home4, {})
        gt4 = open(f"{home4}/.claude/CLAUDE.md").read()
        check("installer replaces a stale v5 ground truth whole (no appended section) and names the real identity",
              r4.returncode == 0 and "ground-truth v9" in gt4 and "hermes" not in gt4 and "one held instance of seat-install" in gt4
              and "sibling comment, not your own" in gt4
              and "You are seat-install" in gt4 and not _os.path.exists(f"{home4}/.claude/CLAUDE.md.new"),
              (r4.stdout + r4.stderr)[-300:])
        for _h in (home1, home2, home3, home4):
            _sh.rmtree(_h, ignore_errors=True)

    finally:
        sb.stop()

    verdict = "ALL PASSED" if FAIL == 0 else "FAILURES DETECTED"
    print(f"\n  {verdict}: wake rail {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
