"""The doc-edit pen (registry/helpers/samcloud-edit, served as /helpers/edit).

Drives the REAL pen against a stub plane, because the thing under test is the
four guards it puts in front of POST /docs/{id}/revisions — a route that makes
base_rev optional (main.py:4829) and a page that, until #900, documented the
call without it. Ticket #900: three seats kept base_rev in memory to cover the
gap and a fourth learned it by overwriting a live doc body (doc 375).

Each case asserts on what reached the wire, not on the pen's exit text alone:
a guard that prints a warning and posts anyway is not a guard.
"""
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PEN = REPO / "registry/helpers/samcloud-edit"


class FakePlane:
    """Minimal /docs/{id} + /revisions + /append, recording every write."""

    def __init__(self, body="original body\n", rev=4, write_code=200,
                 after_body=None, after_rev=None):
        self.body = body
        self.rev = rev
        self.write_code = write_code
        # What the doc reads as AFTER the write: defaults to the write landing.
        self.after_body = after_body
        self.after_rev = after_rev
        self.posts = []          # (path, parsed payload)
        self.gets = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                outer.gets += 1
                if not self.path.endswith("/docs/7"):
                    return self._j(404, {"detail": "nope"})
                # Before any write: the head. After: whatever the case says.
                if outer.posts and outer.after_body is not None:
                    return self._j(200, {"id": 7, "current_rev": outer.after_rev,
                                         "body": outer.after_body})
                if outer.posts:
                    return self._j(200, {"id": 7, "current_rev": outer.rev + 1,
                                         "body": outer.posts[-1][1].get("body", outer.body)})
                return self._j(200, {"id": 7, "current_rev": outer.rev, "body": outer.body})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                outer.posts.append((self.path, payload))
                if outer.write_code == 409:
                    return self._j(409, {"detail": f"Doc 7 is at rev {outer.rev}, not "
                                                   f"{payload.get('base_rev')} — re-read and merge",
                                         "current_rev": outer.rev,
                                         "base_rev": payload.get("base_rev")})
                return self._j(outer.write_code, {"doc_id": 7, "rev": outer.rev + 1})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/api/v1"

    def stop(self):
        self.httpd.shutdown()


def run(plane, body, tmp_path, *args):
    """Run the pen with $HOME redirected and a fake bearer, body on stdin."""
    home = tmp_path / "home"
    (home / ".samcloud").mkdir(parents=True, exist_ok=True)
    (home / ".samcloud" / "token.hdr").write_text("Authorization: Bearer fake-test-token\n")
    env = dict(os.environ, HOME=str(home), SAMCLOUD_API=plane.url,
               SAMCLOUD_SESSION_ID="test-conv")
    env.pop("SAMCLOUD_AUTH_HDR", None)
    env.pop("SAMCLOUD_TOKEN_FILE", None)
    return subprocess.run([str(PEN), "doc", "7", *args], input=body, env=env,
                          capture_output=True, text=True, timeout=60)


def test_served_under_the_helpers_route():
    """The pen is only reachable by seats if main.py serves it."""
    main = (REPO / "registry/main.py").read_text()
    assert '"edit": "samcloud-edit"' in main, \
        "GET /api/v1/helpers/edit must map to the pen, or no seat can install it"
    assert PEN.exists() and os.access(PEN, os.X_OK), "the pen must be executable"


def test_base_rev_comes_from_a_read_taken_now(tmp_path):
    """Guard 1: the compare-and-set input is the head as it is, not as remembered."""
    p = FakePlane(rev=4)
    r = run(p, "a new body\n", tmp_path)
    p.stop()
    assert r.returncode == 0, r.stderr
    assert len(p.posts) == 1
    path, payload = p.posts[0]
    assert path.endswith("/docs/7/revisions")
    assert payload["base_rev"] == 4, "base_rev must be the current_rev just read"
    assert payload["body"] == "a new body\n"


def test_empty_body_is_refused_without_writing(tmp_path):
    """Guard 4: blanking a doc is never the intent."""
    p = FakePlane()
    r = run(p, "   \n\n", tmp_path)
    p.stop()
    assert r.returncode == 3, r.stdout + r.stderr
    assert p.posts == [], "an empty body must not reach the plane"
    assert "empty" in r.stderr.lower()


def test_noop_is_refused_and_names_the_likely_cause(tmp_path):
    """Guard 3: a write identical to the head is an edit that missed its target."""
    p = FakePlane(body="same text\n")
    r = run(p, "same text\n", tmp_path)
    p.stop()
    assert r.returncode == 3, r.stdout + r.stderr
    assert p.posts == [], "a no-op must not be written"
    assert "miss its target" in r.stderr


def test_force_writes_the_noop_anyway(tmp_path):
    p = FakePlane(body="same text\n")
    r = run(p, "same text\n", tmp_path, "--force")
    p.stop()
    assert r.returncode == 0, r.stderr
    assert len(p.posts) == 1


def test_read_after_write_fails_loudly_when_the_head_is_not_what_was_meant(tmp_path):
    """Guard 2: this is #900's original sentence — a 200 that left a junk head.

    The write succeeds and the re-read shows something else; the pen must exit
    non-zero in the same turn rather than report success from the response.
    """
    p = FakePlane(rev=4, after_body="# probe rev, delete me", after_rev=5)
    r = run(p, "the real content\n", tmp_path)
    p.stop()
    assert len(p.posts) == 1, "the write itself should have happened"
    assert r.returncode == 1, "a verified=NO must not exit 0"
    assert "verified=NO" in r.stdout
    assert "not in the head" in r.stdout


def test_a_stalled_rev_counts_as_unverified(tmp_path):
    """A head that still carries the text but never advanced is not a write."""
    p = FakePlane(rev=4, after_body="the real content\n", after_rev=4)
    r = run(p, "the real content\n", tmp_path)
    p.stop()
    assert r.returncode == 1, r.stdout
    assert "current_rev did not advance" in r.stdout


def test_409_says_re_read_and_merge_not_retry(tmp_path):
    p = FakePlane(rev=4, write_code=409)
    r = run(p, "a new body\n", tmp_path)
    p.stop()
    assert r.returncode == 5, r.stdout + r.stderr
    assert "re-read-and-merge" in r.stderr or "re-read" in r.stderr
    assert "NOT a retry" in r.stderr


def test_a_base_rev_below_the_head_409s_without_writing(tmp_path):
    """The non-destructive way to see the guard work.

    Named for the mechanism, not for the believed-staleness phrasing that
    @claude-ios diagnosed on #900: that wording is load-bearing on the caller
    already knowing the head, which is what a seat learning the endpoint does
    not have. Here 1 is below the head of 4, so staleness is derived, not
    believed. The phrase itself is deliberately not reproduced anywhere in this
    file except inside the assertion that forbids it — a docstring carrying it
    verbatim is skimmable back into the same mistake, which is how it survived
    admin's first audit.
    """
    p = FakePlane(rev=4, write_code=409)
    r = run(p, "anything\n", tmp_path, "--base-rev", "1")
    p.stop()
    assert p.posts[0][1]["base_rev"] == 1
    assert r.returncode == 5


def test_a_base_rev_equal_to_the_head_is_refused_as_a_probe(tmp_path):
    """Guard 5, found by this pen doing it to a live doc.

    The first run of the "safe probe" above passed --base-rev 1 against a doc
    that happened to be AT rev 1, so the probe was an ordinary write and it
    replaced a 14,000-character body with one line (#900, 2026-10-10). A
    demonstration that lands as a real write is the accident it was meant to
    prevent, so naming the head is refused unless --force says to assert on it.
    """
    p = FakePlane(rev=4)
    r = run(p, "this must never land\n", tmp_path, "--base-rev", "4")
    p.stop()
    assert r.returncode == 3, r.stdout + r.stderr
    assert p.posts == [], "a probe against the head must not reach the plane"
    assert "IS the current head" in r.stderr


def test_base_rev_on_the_head_still_works_as_an_explicit_assert(tmp_path):
    """--force keeps the legitimate compare-and-set-on-a-known-rev use."""
    p = FakePlane(rev=4)
    r = run(p, "a new body\n", tmp_path, "--base-rev", "4", "--force")
    p.stop()
    assert r.returncode == 0, r.stdout + r.stderr
    assert p.posts[0][1]["base_rev"] == 4


def test_append_uses_the_additive_route_and_sends_no_base_rev(tmp_path):
    """--append reads the head server-side, so it cannot lose a revision."""
    p = FakePlane(body="first\n", rev=4,
                  after_body="first\n\nadded block\n", after_rev=5)
    r = run(p, "added block\n", tmp_path, "--append")
    p.stop()
    assert r.returncode == 0, r.stdout + r.stderr
    path, payload = p.posts[0]
    assert path.endswith("/docs/7/append")
    assert payload["block"] == "added block\n"
    assert "base_rev" not in payload


def test_dry_run_writes_nothing_and_shows_the_base_rev(tmp_path):
    p = FakePlane(rev=4)
    r = run(p, "a new body\n", tmp_path, "--dry")
    p.stop()
    assert r.returncode == 0, r.stderr
    assert p.posts == []
    assert "base_rev: 4" in r.stdout


def test_sizes_are_reported_in_bytes_and_named_as_bytes(tmp_path):
    """One unit, named — the trap three seats reconciled on #900.

    wc -c counts bytes; len() on decoded text counts characters. They differ
    silently by the multi-byte content, so a tool that reports wc -c under a
    "chars" label (as this pen's --dry did) manufactures a disagreement with
    any reader who measures the other way: one seat reconciled 9455
    against 9427 on a doc with 14 non-ASCII characters, and admin's 6008
    against 6033 on a page with 13. Both pairs were correct instruments
    measuring different units and being compared as if they were not.

    An em-dash is 3 bytes and 1 character, so this body is 24 bytes / 20 chars.
    """
    body = "a body with an em-dash \u2014 in it\n"
    assert len(body.encode()) != len(body), "the fixture must be multi-byte"

    p = FakePlane(rev=4)
    r = run(p, body, tmp_path, "--dry")
    p.stop()
    assert r.returncode == 0, r.stderr
    assert f"bytes: {len(body.encode())}" in r.stdout, \
        f"--dry must report bytes, named as bytes; got: {r.stdout!r}"
    assert "chars" not in r.stdout, "one unit, and it is bytes"

    p = FakePlane(rev=4)
    r = run(p, body, tmp_path)
    p.stop()
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{len(body.encode())} bytes" in r.stdout, \
        f"the verify line must use the same unit as --dry; got: {r.stdout!r}"
    assert "chars" not in r.stdout


def test_tickets_and_tasks_are_turned_away_with_the_right_pen(tmp_path):
    p = FakePlane()
    env = dict(os.environ, HOME=str(tmp_path), SAMCLOUD_API=p.url)
    r = subprocess.run([str(PEN), "ticket", "910"], input="x", env=env,
                       capture_output=True, text=True, timeout=30)
    p.stop()
    assert r.returncode == 2
    assert "sc-say" in r.stderr


def test_the_pen_documents_its_own_install_line_and_version():
    """Every pen carries its install line, so a seat can self-serve (claude-ios)."""
    text = PEN.read_text()
    m = re.search(r"^# EDIT_VERSION: (\d+)\.(\d+)\.(\d+)$", text, re.M)
    assert m, "the stamp is how a seat and self_update tell copies apart"
    # Past 1.0.0 deliberately: 1.0.0 is the content six seats installed on
    # 2026-10-14, and the bytes-not-chars fix changed the pen's output without
    # moving the stamp, so for a while both copies answered "1.0.0" and no
    # seat could tell which one it held (#900). A stamp that does not move
    # when the file does is worse than no stamp, because a parity check then
    # reports current for the wrong reason.
    assert tuple(int(g) for g in m.groups()) > (1, 0, 0), \
        "bump EDIT_VERSION when the pen's behaviour or output changes"
    assert "/helpers/edit > ~/.local/bin/sc-edit" in text
    assert "__API__" in text and "__TOKEN_HDR__" in text


def test_the_probe_instruction_is_computable_and_about_the_body():
    """The pen must not teach a probe that depends on a belief about the rev.

    Both bodies lost on 2026-10-10 were placeholders: doc 375 with no base_rev,
    doc 361 with a base_rev that happened to BE the head. The common cause was
    sending a body nobody wanted to keep, which holds even when the reader gets
    the rev wrong — so the pen leads with that.

    It then gives no probe recipe at all. @claude-ios's argument, which admin
    took into #68: stating the parameter's VALUE carries the read inside the
    statement, while any recipe for watching the guard refuse is a full-body
    write however carefully phrased. So this asserts the value statement is
    present, and that the harmful wording is absent from the file — matched
    whitespace-insensitively, since a line-wrapped copy is what a literal grep
    missed.
    """
    text = PEN.read_text()
    assert "NEVER SEND A BODY YOU WOULD NOT WANT TO KEEP" in text
    # State the parameter's VALUE, which carries the read inside the statement
    # (claude-ios) rather than a recipe for seeing a 409 — every such recipe is
    # a full-body write. Asserting "current_rev - 1" here would pin the recipe
    # admin dropped from #68 for exactly that reason.
    assert re.search(r"base_rev IS current_rev", text), \
        "the pen must state that base_rev is current_rev, not how to try it"
    # Whitespace-INSENSITIVE, because a literal grep for this phrase read clean
    # for admin while it sat line-wrapped in a docstring: an audit that cannot
    # see a wrapped hit reports a complete ledger for the wrong reason (#900).
    assert not re.search(r"know\s+is\s+stale|stale\s+base[-_ ]?rev|a\s+base_rev\s+you\s+know",
                         text, re.I), \
        "the pen must not carry the instruction that cost doc 361 its body"


def test_the_bearer_never_reaches_argv():
    """#582: a token on a command line is readable by every local account."""
    text = PEN.read_text()
    assert "-H @" in text, "a header file must ride curl -H @file"
    assert "--config" in text, "a raw token must go via curl --config on its own fd"
    assert not re.search(r'Authorization: Bearer \$', text), \
        "the bearer must never be interpolated into an argument"
