#!/usr/bin/env python3
"""samcloud-listen — event-driven wake trigger (RFC doc #17 v2).

Holds the registry's scoped SSE stream open under the agent's own token and
runs the registered WAKE_CMD the moment an event arrives. The durable inbox
stays the source of truth: WAKE_CMD is invoked on every (re)connect too, so
events missed during a disconnect cost nothing (the handler self-checks
unread and no-ops cheaply). Harness-agnostic: WAKE_CMD is any command; the
triggering payload arrives on stdin.

Supervised by the OS service manager (LaunchDaemon with UserName= on macOS,
systemd elsewhere) — no cron, ever. Stdlib only; no dependencies.
"""
import json
import os
import random
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Fleet version (doc #22 step 1). The registry parses this line to know what
# it serves; the listener reports it on every connect via POST /wake/register
# and self-updates (atomic swap + re-exec) when the served version differs.
# Bump on ANY change to this file.
RAIL_VERSION = "2.8.0"

CONF = os.environ.get("SAMCLOUD_WAKE_CONF",
                      os.path.expanduser("~/.config/samcloud/wake.env"))


def load_conf():
    cfg = {}
    if os.path.exists(CONF):
        for line in open(CONF):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k] = v.strip().strip('"')
    return cfg


cfg = load_conf()
API = os.environ.get("API") or cfg.get("API", "__API__")
TOKEN_PATH = os.path.expanduser(os.environ.get("TOKEN_PATH")
                                or cfg.get("TOKEN_PATH", "~/.samcloud/token"))
WAKE_CMD = os.environ.get("WAKE_CMD") or cfg.get(
    "WAKE_CMD", os.path.expanduser("~/.local/bin/samcloud-wake"))
IDLE_TIMEOUT = int(cfg.get("IDLE_TIMEOUT", "45"))     # 3 missed 15s keepalives = dead socket
# #438: self_update() only runs on (re)connect, so a connection that never
# drops never checks for an update either — measured 4d13h on a healthy
# seat, worse when a proxy in the path masks the deploy-restart FIN a
# reconnect would otherwise rely on. Forcing a periodic reconnect bounds
# staleness to this value regardless of socket health or what's upstream.
MAX_CONN_AGE = int(cfg.get("MAX_CONN_AGE_SEC", str(6 * 3600)))
# Named, not inlined, because the truncation report quotes it back to the plane and
# an operator reading "killed at 1920s" needs to know which knob made it 1920.
MAX_SESSION_MIN = int(cfg.get("MAX_SESSION_MIN", "30"))
WAKE_TIMEOUT = MAX_SESSION_MIN * 60 + 120
LOG_PATH = os.path.expanduser(cfg.get("LISTEN_LOG", "~/logs/samcloud-listen.log"))
# Where woken sessions start. $HOME default (a bridge's inherited cwd may be
# unreadable — 2.2.1 fix); envs whose samcloud grounding lives elsewhere set
# WAKE_CWD in wake.env — an UNGROUNDED session correctly refuses the wake
# contract as injection (seen live on one seat 2026-07-12: sessions spawned
# in a bare $HOME declined to touch the token or the API — right instinct,
# wrong target — while repo-grounded sessions engage normally).
WAKE_CWD = os.path.expanduser(os.environ.get("SAMCLOUD_WAKE_CWD")
                              or cfg.get("WAKE_CWD", "~"))
# Events that never carry addressed work — don't bother the handler.
IGNORE_TYPES = {"health.reported", "service.health", "resource.stats"}
# Rail v3 mode (doc 161; #568 §2 — the finding that the v2 lifecycle gates
# and the v3 CONTINUE verb are mutually exclusive). Opt-in per seat via
# RAIL_V3=1 in wake.env; OFF = behaviorally equivalent to v2 dispatch (the
# only off-mode addition is the inert pidfile write below), so fleet-wide
# self-update of this file is safe. NOTE the limit of that invariant (#564
# r3): it is within-file. While prod and staging registries serve DIFFERENT
# listener versions, a seat whose listeners share one binary oscillates
# between them on re-exec — and the served files' default API constants
# differ, so a config that omits API can flip meshes. Pilot listeners MUST
# run a separate binary copy until both registries serve the same version.
# What changes when RAIL_V3 is ON:
#   - every row PASSES THROUGH to the handler: the rail routes on the plane's
#     thread record and the delivery rule (rail 3.9, doc 174) — the listener
#     neither gates on claims nor defers; an empty inbox is its only no-op.
#   - presence is NOT published around WAKE_CMD, and NOT used as a dispatch
#     gate: the handler exits in seconds while holders live for as long as
#     their objects are open — listener presence would measure nothing.
#   - claims are NOT released after handler exit: they are a legacy lease the
#     rail no longer routes on; the rail releases a dead holder's stale
#     claims itself when it observes the ending.
RAIL_V3 = (os.environ.get("RAIL_V3") or cfg.get("RAIL_V3", "")) == "1"

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%F %T')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def inbox_state(token):
    """(has_unread, has_unclaimed) in one call — rows carry per-thread claim
    flags (#199). The wake predicate is has_unclaimed: a live surface owns
    its claimed threads; the rail handles everything else in parallel.
    Fail-open: on error assume unclaimed work exists (missing a defer is
    cheap, missing work is not)."""
    try:
        req = urllib.request.Request(f"{API}/notifications?unread=true&limit=50",
                                     headers={"Authorization": f"Bearer {token}"})
        rows = json.loads(urllib.request.urlopen(req, timeout=5).read()) \
            .get("notifications", [])
        return (len(rows) > 0, any(not r.get("claimed") for r in rows))
    except Exception:
        return (True, True)


DEFERRED = {"pending": False}   # claimed-only work seen; recheck on keepalives
# v3 heartbeat (2.6.3 → OFF by default in 2.7.8). It existed so a dead
# holder's claimed rows would be re-offered on a clock (#564). Under rail 3.9
# nothing routes on claims, delivery is receipt (never re-offered), and a gone
# process is observed by the handler's sweep on the identity's next event —
# there is nothing left for a timer to recover, and "no timers anywhere" is
# the lifecycle's rule (doc 174). RAIL_V3_HEARTBEAT_MIN=N in
# wake.env turns it back on for a seat that wants a periodic inbox poll.
V3_HB = {"last": 0.0}
V3_HB_SEC = int(cfg.get("RAIL_V3_HEARTBEAT_MIN", "0")) * 60
                                # (claims expire if their surface dies — #199)


def presence_live(token):
    """True if a live surface of this identity currently holds presence.

    v2.4.0 (#264 follow-through, 2026-07-29): per-thread claims alone proved
    insufficient — three parallel-surface incidents in 36h where cold wakes
    fired on UNCLAIMED rows while an interactive terminal of the same identity
    was live and watching (a live surface can't claim a thread it hasn't
    written to). Presence now gates cold spawns entirely: rows wait for the
    live surface; the keepalive recheck (DEFERRED) picks them up within ~15s
    of presence lapsing, bounded by the presence TTL if the surface dies.
    Fail-open to ABSENT: waking alongside a live surface is recoverable,
    deafness is not."""
    try:
        req = urllib.request.Request(
            f"{API}/presence",
            headers={"Authorization": f"Bearer {token}"})
        return bool(json.loads(urllib.request.urlopen(req, timeout=5).read())
                    .get("present"))
    except Exception:
        return False


def publish_presence(token, ttl):
    """Presence around WAKE_CMD lives HERE, not in the default handler, so a
    custom harness (Hermes etc.) inherits it (#176 / #169 finding 2).
    Fail-open: a failure never blocks dispatch."""
    # #498: one retry on failure. A 502 landing in a shutdown window used to
    # orphan the presence record for its full TTL — every event then deferred
    # to the ghost (155 defer lines on one seat) and the failure mode was
    # silence. Still fail-open after the retry.
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                f"{API}/presence",
                data=json.dumps({"ttl_seconds": ttl}).encode(),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            return
        except Exception as e:
            if attempt == 1:
                time.sleep(2)
                continue
            log(f"presence publish failed after retry (non-fatal): {e}")


def report_truncated(token, seconds: int, rc: int) -> None:
    """Tell the plane that WE killed this session at the cap (#895).

    This is the one kill nothing reported. The listener's timeout path logged
    `wake handler exceeded Ns — killed` to the local listener log and stopped
    there: no `adapter.jsonl` row (the handler died before writing one), no
    session report (the handler is what posts those, and it is dead), nothing on
    the plane at all. So a seat whose work is being truncated every time looked
    identical to a quiet one.

    `cause: watchdog` specifically, and NOT a latching failure. The registry's
    `_CAUSE_TRUNCATING` is this exact state — "OUR supervisor killing the session
    at MAX_SESSION_MIN … the one outcome where the seat is PROVABLY ALIVE AND
    WORKING at the moment of the verdict" — argued to latching and then reversed
    by measurement: 29 kills, zero stalled, and a 5-long run that at the default
    threshold of 3 would have declared a working seat `failing-launches`. So this
    moves `truncations` and leaves `consecutive_failures` where it was. It is the
    signal that says "raise MAX_SESSION_MIN", never "go fix that seat".

    No double-count: we are reporting a session the handler never got to report,
    because we killed it mid-flight. On every other path the handler posts and we
    post nothing.

    Fail-open and fire-and-forget, like presence: a registry that is down must
    never change what the listener does next.
    """
    if not token:
        return
    try:
        req = urllib.request.Request(
            f"{API}/wake/session",
            data=json.dumps({
                "outcome": "failed",
                # 143 = SIGTERM, which is what a kill at the cap is. `rc` is
                # ground truth at the endpoint, so an `ok` here would be rejected
                # as a confused caller — correctly.
                "rc": rc,
                "cause": "watchdog",
                "error": f"listener killed the wake handler at {seconds}s "
                         f"(MAX_SESSION_MIN={MAX_SESSION_MIN})",
                "host": os.uname().nodename.split(".")[0],
            }).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        log(f"reported truncation to the plane (cause=watchdog, {seconds}s)")
    except Exception as e:
        log(f"truncation report failed (non-fatal): {e}")


def reap_orphans() -> int:
    """Reap any orphan the kernel reparented to us. Returns how many.

    Only meaningful when this process is PID 1, which it is in a profile-env
    container: the rail launches a holder detached (`start_new_session=True`) and
    exits, so when that holder dies ITS children reparent to us, and when the
    holder itself dies it is already our child by reparenting. Nothing reaped
    them. Measured on a container seat after ONE failed turn: the dead
    adapter and its code kernel sat `Zs` with PPID 1 for 40 and 32 minutes. Two
    leaked PIDs per turn on a seat that runs every day is a slow walk to a fork
    failure, and it presents weeks later as "the seat stopped working" with
    nothing in any log.

    CALLED ONLY FROM THE MAIN LOOP, NEVER FROM A SIGNAL HANDLER, and that is the
    whole safety argument. `waitpid(-1)` cannot tell our own subprocess children
    from reparented orphans, so a SIGCHLD handler would fire DURING
    `subprocess.run(WAKE_CMD)` and steal the exit status that `run()` is waiting
    for — the classic PID-1 reaping bug, and it would turn every wake into a
    spurious failure. This module has no threads and no signal handlers (checked:
    zero `threading` and zero `signal.` references), and both `subprocess.run`
    sites are synchronous in the main flow, so a call placed where no
    `subprocess.run` is in flight cannot race one.

    Asserted by sandbox/test_listener_reap.py::test_reap_never_runs_during_a_wake,
    which fails if a future edit moves the call inside the handler's window.
    """
    if os.getpid() != 1:
        return 0
    reaped = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break                      # nothing left to reap
        except OSError:
            break
        if pid == 0:
            break                      # children exist, none has exited
        reaped += 1
    if reaped:
        log(f"reaped {reaped} orphan{'s' if reaped != 1 else ''} (PID 1)")
    return reaped


def new_session_id():
    """Per-wake session nonce (v2.5.0, #264). Generated HERE — not in the
    handler — so the listener's post-WAKE release (below) carries the SAME id
    the spawned session claims under, and can therefore delete that session's
    own stamped claim. It's exported into the WAKE_CMD env; the default
    handler inherits it (its own generation is now a `:-` fallback for direct
    runs), substitutes it into the served contract as __SESSION_ID__, and the
    session sends it as X-Session-Id on every write. Host-pid-rand, 64-cap."""
    host = socket.gethostname().split(".")[0]
    return f"wake-{host}-{os.getpid()}-{os.urandom(5).hex()}"[:64]


def release_claims(token, session_id=None):
    """Release the thread claims this wake's session took, once WAKE_CMD exits
    — the harness-agnostic complement to presence-clear (#199/#206). A handler
    that wrote to a ticket (auto-claiming it) but exits without holding a watch
    otherwise leaves that thread claimed-and-orphaned: no live surface owns it,
    yet this identity's own rail defers waking on it until the claim expires —
    for a low-traffic identity that reads as total deafness.

    v2.5.0 (#264 regression fix): once wake sessions stamp their claims with a
    session_id (v0.7.53), an identity-wide release that sends NO id only
    matches `session_id IS NULL` rows — so a session's OWN stamped claim
    survived its exit, then the presence-backstop (#266) resurrected the
    expired row on the next wake and masked the thread forever (stranded
    a seat on #277, 2026-07-29). Passing this wake's session_id
    makes the release delete exactly this session's rows (+ legacy NULL rows),
    never a parallel live session's. Runs in the listener's `finally`, so it
    fires even if the handler timed out or was killed. Fail-open."""
    # #498: one retry — an eaten release used to leave a ghost claim/presence
    # pair that masked the seat's threads for the full TTL.
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                f"{API}/wake/claims/release", data=b"{}",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json",
                         **({"X-Session-Id": session_id} if session_id else {})})
            urllib.request.urlopen(req, timeout=5).read()
            return
        except Exception as e:
            if attempt == 1:
                time.sleep(2)
                continue
            log(f"claim release failed after retry (non-fatal): {e}")


def _spawn_cwd():
    """WAKE_CWD, verified USABLE FROM A CHILD PROCESS — else $HOME, loudly.

    The check must run in a child (not os.access/listdir in-process): macOS
    TCC grants are per-process-tree, and the failure we're guarding against
    (#226) is precisely 'the listener's children EPERM on a cwd the admin
    could read fine from a terminal'. A launchd listener given WAKE_CWD in
    ~/Documents killed every handler for 2 days while logging exit=0."""
    try:
        r = subprocess.run(["/bin/ls", "."], cwd=WAKE_CWD,
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            return WAKE_CWD
        err = (r.stderr or b"").decode(errors="replace").strip()[:200]
    except Exception as e:
        err = str(e)[:200]
    home = os.path.expanduser("~")
    log(f"WAKE_CWD UNUSABLE from this process tree: {WAKE_CWD} ({err}) — "
        f"falling back to {home}. If this is macOS and the path is under "
        f"~/Documents|Desktop|Downloads, that's TCC (#226): move the rail's "
        f"grounding checkout outside TCC paths.")
    return home


def wake(reason, payload, token=None):
    """Run the registered wake command, payload on stdin. Single-flight by
    construction (called synchronously from the read loop). Per-thread defer
    (#199): wake only when unread work exists that no live surface of this
    identity has claimed — the claimant's held watch delivers claimed rows."""
    if token:
        has_unread, has_unclaimed = inbox_state(token)
        if RAIL_V3:
            # v3 (#568 §2, rail 3.9): no claim gate, no presence gate — the
            # handler owns routing (deliver, or not, per doc 174). Only an
            # empty inbox is a cheap no-op.
            if not has_unread:
                log(f"skip ({reason}): inbox empty")
                return
        else:
            if not has_unclaimed:
                DEFERRED["pending"] = has_unread
                log(f"defer ({reason}): all unread claimed by a live surface"
                    if has_unread else f"skip ({reason}): inbox empty")
                return
            # Presence gate (v2.4.0): unclaimed rows + a live surface = the live
            # surface's inbox, not a cold spawn's. DEFERRED keeps the keepalive
            # recheck polling; wake() re-runs this check each time, so the spawn
            # happens within ~15s of presence lapsing with rows still unread.
            if presence_live(token):
                DEFERRED["pending"] = True
                log(f"defer ({reason}): live surface holds presence — "
                    f"unclaimed rows wait for it")
                return
    DEFERRED["pending"] = False
    log(f"wake: {reason}")
    sess = new_session_id()
    if token and not RAIL_V3:
        publish_presence(token, WAKE_TIMEOUT + 60)
    try:
        r = subprocess.run(shlex.split(WAKE_CMD),
                           input=json.dumps(payload).encode(),
                           timeout=WAKE_TIMEOUT,
                           cwd=_spawn_cwd(),
                           env={**os.environ, "SAMCLOUD_SESSION_ID": sess})
        log(f"wake handler exit={r.returncode}")
    except subprocess.TimeoutExpired:
        log(f"wake handler exceeded {WAKE_TIMEOUT}s — killed")
        # We killed it, so the handler never reported this session — it is the one
        # kill with no record anywhere on the plane (#895). 143 = SIGTERM.
        report_truncated(token, WAKE_TIMEOUT, 143)
    except FileNotFoundError:
        log(f"WAKE_CMD not found: {WAKE_CMD}")
    finally:
        # Safe HERE and nowhere earlier: subprocess.run has returned or raised, so
        # no run() is waiting on a child whose status waitpid(-1) could steal. See
        # reap_orphans' docstring for why this is not a SIGCHLD handler.
        reap_orphans()
        if token and not RAIL_V3:
            # v3: claims belong to still-live held sessions (#568 §2 item 3);
            # the handler releases a dead holder's claims itself. Presence is
            # the held sessions' own signal, never the listener's.
            publish_presence(token, 0)
            release_claims(token, session_id=sess)


# Both handler generations (2.6.4, task 610): the v2 wake script AND the v3
# rail. Before this, only samcloud-wake was recognized — v3 rails carried no
# WAKE_VERSION stamp, so _handler_version() returned None and self_update
# SILENTLY SKIPPED them: no seat ever received a rail update automatically
# (a 400-char-digest rail from Aug 30 was found live on a seat 2026-09-01,
# staging #86, while four rail releases sat undistributed). Suffixed installs
# (samcloud-rail.stg) match by prefix and update from their own plane's API.
_HANDLER_SPECS = {
    "samcloud-wake": (r'^WAKE_VERSION="([^"]+)"', "wake"),
    "samcloud-rail": (r'^RAIL_VERSION = "([^"]+)"', "rail"),
    # The rail's other holder (rail 3.12.0). WAKE_CMD never points at it — the
    # rail invokes it — so only the sibling scan (2.7.11) keeps it current, and
    # a seat whose runtime is not Claude has nothing else that would.
    "samcloud-adapter": (r'^# ADAPTER_VERSION: (\S+)', "adapter"),
}


def _handler_spec():
    """(path, version, pattern, served_name) of the wake handler if WAKE_CMD
    points at one of ours. Detection is by CONTENT STAMP, not filename —
    the #86 anatomy session found a live seat whose rail was a scratch-named
    copy (rail-3.3.0.py) that basename matching would never have updated.
    The version stamp itself is the ownership marker: a file carrying one of
    our stamps is ours to refresh; a stampless file is someone's custom
    harness and is never clobbered (registry treats null as 'not ours')."""
    try:
        path = shlex.split(WAKE_CMD)[0]
        body = open(path).read()
        for _name, (pat, served) in _HANDLER_SPECS.items():
            m = re.search(pat, body, re.M)
            if m:
                return (path, m.group(1), pat, served)
    except Exception:
        pass
    return None


def _sibling_handler_specs():
    """Every OTHER stamped handler of ours beside the active one (2.7.11).

    `_handler_spec()` resolves only what WAKE_CMD points at, so migrating a
    seat to rail-v3 — which repoints WAKE_CMD at `samcloud-rail.prod` — drops
    `samcloud-wake` out of the update set at exactly the moment the seat stops
    running it. It then freezes at whatever bytes it had on migration day,
    forever. Measured on a native-client seat: `samcloud-wake`
    2.4.6, over three weeks old, **7 sites putting the bearer in argv** — the
    exposure #582 fixed and 2.4.7 shipped, on a seat whose listener had
    connected continuously for two weeks and self-updated its rail that day.

    It matters because `wake.env.pre-v3` sits beside it and the filename
    invites a rollback: repointing WAKE_CMD arms the stale file, and the next
    listener connect is up to MAX_CONN_AGE (6 h) away.

    Same ownership rule as `_handler_spec()`: a file carrying one of our
    stamps is ours to refresh; a stampless file is someone's harness and is
    never touched."""
    out = []
    try:
        active = os.path.abspath(shlex.split(WAKE_CMD)[0])
        bindir = os.path.dirname(active) or os.path.expanduser("~/.local/bin")
    except Exception:
        return out
    try:
        names = os.listdir(bindir)
    except OSError:
        return out
    own = ""
    try:
        own = os.path.abspath(sys.argv[0])
    except Exception:
        pass
    for fname in sorted(names):
        path = os.path.join(bindir, fname)
        ap = os.path.abspath(path)
        if ap == active or ap == own or not os.path.isfile(path):
            continue
        try:
            body = open(path).read()
        except (OSError, UnicodeDecodeError):
            continue
        # A LISTENER IS NOT A HANDLER, and this file spells its own stamp
        # `RAIL_VERSION` — the same name the rail uses. So the rail's pattern
        # matches a listener, and before 2.7.13 this scan installed the RAIL
        # OVER IT. Measured 2026-09-22: twelve seats across three machines
        # carrying rail 3.13.0 at ~/.local/bin/samcloud-listen, each
        # one deaf at its next restart and none of them showing it, because the
        # listener already resident kept running from the bytes it was loaded
        # from. `ap == own` covers the running file; this covers a copy under
        # another name (samcloud-listen.stg) that `own` would not.
        if '"""samcloud-listen' in body[:400]:
            continue
        for _name, (pat, served) in _HANDLER_SPECS.items():
            m = re.search(pat, body, re.M)
            if m:
                out.append((path, m.group(1), pat, served))
                break
    return out


def _handler_version():
    spec = _handler_spec()
    return spec[1] if spec else None


# Interactive-surface hooks (#198): field -> (installed filename, version
# stamp, served helper name). Reported on register (null = surface dark) and
# refreshed by self_update — only files carrying our stamp are ever touched.
# HOOKS_DIR in wake.env for project-level installs (repo/.claude/hooks).
HOOKS_DIR = os.path.expanduser(os.environ.get("SAMCLOUD_HOOKS_DIR")
                               or cfg.get("HOOKS_DIR", "~/.claude/hooks"))
HOOK_SPECS = {
    "notif_hook_version": ("samcloud-notif.sh",
                           r'^# NOTIF_HOOK_VERSION: (\S+)', "notif-hook"),
    "stop_hook_version": ("samcloud-presence-stop.sh",
                          r'^# STOP_HOOK_VERSION: (\S+)', "stop-hook"),
}


_HOOK_READ_WARNED = set()


def _hook_version(fname, pattern):
    path = os.path.join(HOOKS_DIR, fname)
    try:
        m = re.search(pattern, open(path).read(), re.M)
        return m.group(1) if m else None
    except PermissionError:
        # Distinct from "not installed": the file may exist and work fine in
        # interactive sessions, but THIS process tree can't read it (macOS
        # TCC, #226) — so the null we report to the fleet view is a reporting
        # blind spot, not a dark surface. Say so once per run.
        if path not in _HOOK_READ_WARNED:
            _HOOK_READ_WARNED.add(path)
            log(f"hook {path} unreadable from this process tree (TCC? #226) — "
                f"reporting null; fleet view may show surface_dark despite "
                f"working hooks. Move HOOKS_DIR outside TCC paths.")
        return None
    except Exception:
        return None


def register(token):
    """Fleet registration (doc #22 step 1): tell the registry this rail
    exists — wake_cmd, versions, host, env. Called on every connect; the
    response carries the served versions. Fail-open: an old registry without
    the endpoint (404) or a network blip never blocks the rail."""
    try:
        body = {"wake_cmd": WAKE_CMD, "host": socket.gethostname(),
                "listener_version": RAIL_VERSION,
                "handler_version": _handler_version(), "api_env": API,
                # #472: the registry derives its dark-seat threshold from the
                # seat's actual reconnect bound, not an assumed default.
                "max_conn_age": MAX_CONN_AGE}
        for field, (fname, pattern, _served) in HOOK_SPECS.items():
            body[field] = _hook_version(fname, pattern)
        req = urllib.request.Request(
            f"{API}/wake/register", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as e:
        log(f"register failed (non-fatal): {e}")
        return {}


def _fetch_helper(token, name):
    req = urllib.request.Request(f"{API}/helpers/{name}",
                                 headers={"Authorization": f"Bearer {token}"})
    return urllib.request.urlopen(req, timeout=15).read().decode()


# Installed agent skills (#588 follow-up). A skill is operating guidance the
# fleet READS — when it goes stale it teaches the wrong thing to every seat at
# once, which is worse than a stale binary because nothing errors. So skills
# ride the same content-stamp path as the rail and the hooks rather than a
# separate fetch: stamped file on disk -> compare to what the plane serves ->
# swap on change. A skill without a stamp is someone's own and is never touched.
SKILLS_DIR = os.path.expanduser(os.environ.get("SAMCLOUD_SKILLS_DIR")
                                or cfg.get("SKILLS_DIR", "~/.claude/skills"))
SKILL_STAMP = r'^<!--\s*SKILL_VERSION:\s*(\S+)\s*-->'


def _fetch_skill(token, skill, filename):
    req = urllib.request.Request(f"{API}/skills/{skill}/{filename}",
                                 headers={"Authorization": f"Bearer {token}"})
    return urllib.request.urlopen(req, timeout=15).read().decode()


def _served_skills(token):
    """What the plane serves: [{dir, chunks:[...]}, ...]."""
    req = urllib.request.Request(f"{API}/skills",
                                 headers={"Authorization": f"Bearer {token}"})
    body = urllib.request.urlopen(req, timeout=15).read().decode()
    return (json.loads(body) or {}).get("skills", [])


def _install_missing_skills(token):
    """First-install branch (2.7.1).

    2.7.0 only REFRESHED skills already on disk, because it enumerated the
    local tree to decide what to fetch. That meant a skill a seat had never
    had could never arrive — the exact case that matters, found on two seats
    within an hour of shipping (staging #90). Refresh-only is not
    distribution. So: ask the plane what it serves, and install what is
    missing, entry point and chunks together.

    Only ever CREATES a skill dir the seat lacks; an existing skill is left to
    the stamp-compare path below, so nothing local is clobbered by this.
    """
    try:
        served = _served_skills(token)
    except Exception as e:
        log(f"skill list unavailable: {e.__class__.__name__}")
        return
    for s in served:
        sdir = s.get("dir") or s.get("name")
        if not sdir or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sdir):
            continue
        target = os.path.join(SKILLS_DIR, sdir)
        if os.path.exists(os.path.join(target, "SKILL.md")):
            continue                      # present -> refresh path owns it
        try:
            files = ["SKILL.md"] + [c for c in (s.get("chunks") or [])
                                    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.md", c)]
            fetched = {f: _fetch_skill(token, sdir, f) for f in files}
        except Exception as e:
            log(f"skill {sdir} fetch failed ({e.__class__.__name__}) — retry next connect")
            continue
        os.makedirs(target, exist_ok=True)
        for fname, text in fetched.items():
            _swap_file(os.path.join(target, fname), text)
        log(f"skill {sdir} installed ({len(fetched)} files)")


def sync_hooks(token):
    """Install/refresh interactive hooks — OUTSIDE the update_available gate.

    2.7.3 fixed the hook guard but left the loop inside self_update(), i.e.
    behind the exact gate 2.7.2 had just lifted skills out of: corrected
    logic, unreachable branch. Caught on two seats by a POSITIVE falsifier —
    the "unstamped" line below is emitted unconditionally whenever the loop
    reaches a stampless hook, and it appeared zero times on a seat guaranteed
    to hit it. Third occurrence of one root cause: a distribution path gated
    on an unrelated component's version can only ever maintain what it
    already maintains.
    """
    # 2.7.2: the `local is None` guard was exempting hooks PERMANENTLY, in
    # two shapes found live (staging #91, claude-services-staging): a hook
    # installed before stamps existed (unstamped, so never refreshed) and a
    # hook never installed at all (absent, so never installed). Same
    # "already-there-and-stamped required" gate that made skills undeliver-
    # able. Absent is safe to fix — nothing of the seat's can be lost — so
    # it is now installed. Unstamped is NOT: it may genuinely be the seat's
    # own file, and clobbering it would destroy work. It is logged instead,
    # so a frozen hook is visible rather than silent.
    for _field, (fname, pattern, served) in HOOK_SPECS.items():
        path = os.path.join(HOOKS_DIR, fname)
        local = _hook_version(fname, pattern)
        adopt = False
        if local is None and os.path.exists(path):
            # 2.7.6: "never touch an unstamped file" was protecting the WRONG
            # files. A hook installed before stamps existed IS ours, is
            # permanently exempt (no stamp -> no version -> never replaced),
            # and rots in place. Measured on a live seat: a 2026-06-14 notif
            # hook that builds a DOUBLED /api/v1 (dead since 07-28) and still
            # instructs ack {"all":true} — now an explicit never. Frozen
            # guidance does not merely go stale, it drifts into contradicting
            # current policy while still being read, and the exemption is what
            # preserved it.
            # So: ADOPT a file carrying our provenance header, backing it up
            # first so the adoption is always reversible. A file without that
            # header is genuinely someone else's and is still never touched.
            try:
                head = open(path).read(4096)
            except OSError:
                continue
            if re.search(rf"^#\s*{re.escape(fname)}\s*[—-]", head, re.M):
                adopt = True
            else:
                log(f"hook {fname} is unstamped and not ours — left alone")
                continue
        try:
            text = _fetch_helper(token, served)
        except Exception as e:
            log(f"hook {fname} fetch failed ({e.__class__.__name__})")
            continue
        m = re.search(pattern, text, re.M)
        if not m:
            continue
        if adopt:
            bak = f"{path}.pre-adopt-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(path, bak)
            except OSError:
                log(f"hook {fname} adopt aborted — could not back up")
                continue
            _swap_file(path, text)
            log(f"hook {fname} ADOPTED at {m.group(1)} (unstamped original kept "
                f"at {os.path.basename(bak)})")
        elif not os.path.exists(path):
            os.makedirs(HOOKS_DIR, exist_ok=True)
            _swap_file(path, text)
            log(f"hook {fname} installed at {m.group(1)}")
        elif m.group(1) != local:
            _swap_file(path, text)
            log(f"hook {fname} updated {local} -> {m.group(1)}")


def sync_skills(token):
    """Install missing skills, then refresh stamped ones. Independent of the
    listener's own version — see the call site for why that matters.

    Logs on every outcome that is not 'nothing to do': a distribution path that
    silently no-ops is exactly the defect this function exists to fix, twice
    over now.
    """
    try:
        _install_missing_skills(token)
    except Exception as e:
        log(f"skill install failed: {e.__class__.__name__}: {e}")
    for skill, fname, path, local in _installed_skills():
        try:
            text = _fetch_skill(token, skill, fname)
        except urllib.error.HTTPError as e:
            # 2.7.10: a stamped chunk the plane no longer serves is retired —
            # it was ours (the stamp says so), so it goes. The stamp also keeps
            # a seat's own unstamped files untouched, as everywhere else here.
            if e.code == 404:
                try:
                    os.remove(path)
                    log(f"skill {skill}/{fname} retired (no longer served)")
                except OSError:
                    pass
            continue
        except Exception:
            continue              # offline — fine
        m = re.search(SKILL_STAMP, text, re.M)
        if m and m.group(1) != local:
            _swap_file(path, text)
            log(f"skill {skill}/{fname} updated {local} -> {m.group(1)}")
    # 2.7.10: chunks the plane serves that this seat's EXISTING skill dir lacks.
    # Until now a new page in a skill the seat already had could never arrive:
    # the refresh loop enumerates local files and the install branch only
    # creates whole missing dirs. Manifest-driven, like the first install.
    try:
        served = _served_skills(token)
    except Exception:
        served = []
    for sk in served:
        sdir = sk.get("dir") or sk.get("name")
        if not sdir or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sdir):
            continue
        target = os.path.join(SKILLS_DIR, sdir)
        if not os.path.exists(os.path.join(target, "SKILL.md")):
            continue                      # the install branch owns missing dirs
        for c in (sk.get("chunks") or []):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}\.md", c) or os.path.exists(os.path.join(target, c)):
                continue
            try:
                _swap_file(os.path.join(target, c), _fetch_skill(token, sdir, c))
                log(f"skill {sdir}/{c} installed (new page)")
            except Exception as e:
                log(f"skill {sdir}/{c} fetch failed ({e.__class__.__name__}) — retry next connect")


def _installed_skills():
    """(skill_name, filename, path, local_version) for every stamped skill file
    installed on this seat. Chunks are updated alongside their SKILL.md, so a
    seat never ends up with a new entry point pointing at old detail."""
    out = []
    try:
        for entry in sorted(os.listdir(SKILLS_DIR)):
            sdir = os.path.join(SKILLS_DIR, entry)
            if not os.path.isdir(sdir):
                continue
            for fname in sorted(os.listdir(sdir)):
                if not fname.endswith(".md"):
                    continue
                path = os.path.join(sdir, fname)
                try:
                    m = re.search(SKILL_STAMP, open(path).read(), re.M)
                except OSError:
                    continue
                if m:
                    out.append((entry, fname, path, m.group(1)))
    except OSError:
        pass
    return out


def _swap_file(path, text):
    tmp = path + ".new"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)


def self_update(token):
    """Fetch the served listener/handler; if genuinely newer, atomically swap
    and re-exec. Version is parsed from the FETCHED bytes (not the register
    response), so a half-served or garbled artifact can never install: no
    parseable version -> no swap. execv preserves pid, so supervision
    (launchd/systemd KeepAlive) is undisturbed."""
    own_path = os.path.abspath(sys.argv[0])
    try:
        # Handler first (no re-exec needed) — v2 wake script OR v3 rail,
        # whichever WAKE_CMD points at (2.6.4 closed the v3 gap: rails were
        # never in the update set and went stale fleet-wide).
        spec = _handler_spec()
        if spec and spec[1] is not None:
            hpath, hv, hpat, hserved = spec
            text = _fetch_helper(token, hserved)
            m = re.search(hpat, text, re.M)
            if m and m.group(1) != hv:
                _swap_file(hpath, text)
                log(f"handler updated {hv} -> {m.group(1)} ({hserved})")
                # The fleet view (GET /wake/registrations) showed the version
                # this listener had at connect for the rest of the connection —
                # 3.9.9 for hours after every seat ran 3.9.10 (staging #105).
                # Re-register now, so the row says what the seat runs.
                try:
                    register(token)
                except Exception as e:
                    log(f"re-register after handler update failed (non-fatal): {e}")
        # The handler this seat no longer points at is still on its disk, and a
        # rollback arms it (2.7.11, #796). Keep every stamped handler current,
        # not just the live one — a frozen `samcloud-wake` is a bearer in argv.
        for _sp, _sv, _spat, _sserved in _sibling_handler_specs():
            try:
                _text = _fetch_helper(token, _sserved)
                _m = re.search(_spat, _text, re.M)
                if _m and _m.group(1) != _sv:
                    _swap_file(_sp, _text)
                    log(f"idle handler updated {_sv} -> {_m.group(1)} "
                        f"({os.path.basename(_sp)}) — not in use, kept current")
            except Exception as e:
                log(f"idle handler refresh failed for {_sp} (non-fatal): {e}")
        # NOTE: hooks (#198) and skills are NOT updated here. Both are synced on
        # every connect by sync_hooks()/sync_skills(), because a listener-version
        # gate can only ever fire on the way OUT of a version. The descriptive
        # comment that used to sit here outlived the code it described and led a
        # reviewer to read the handler compare above as a hook site (staging #91)
        # — a comment about relocated code is a lie the refactor leaves behind.
        # WSL boot-task script (#231): content-only update at the deployed
        # Windows path — the scheduled task runs whatever is there, so no
        # re-registration/elevation is ever needed after first install.
        # WSL_BOOT_PATH is written into wake.env by wake-install on WSL hosts.
        wb_path = cfg.get("WSL_BOOT_PATH", "")
        if wb_path and os.path.exists(wb_path):
            wb_pat = r'^# WSL_BOOT_VERSION: (\S+)'
            local = None
            try:
                lm = re.search(wb_pat, open(wb_path).read(), re.M)
                local = lm.group(1) if lm else None
            except Exception:
                pass
            if local is not None:  # stampless file = someone's custom script
                text = _fetch_helper(token, "wsl-boot")
                m = re.search(wb_pat, text, re.M)
                if m and m.group(1) != local:
                    _swap_file(wb_path, text)
                    log(f"wsl-boot updated {local} -> {m.group(1)}")
        text = _fetch_helper(token, "listen")
        m = re.search(r'^RAIL_VERSION = "([^"]+)"', text, re.M)
        if m and m.group(1) != RAIL_VERSION:
            _swap_file(own_path, text)
            log(f"listener updated {RAIL_VERSION} -> {m.group(1)} — re-exec")
            os.execv(sys.executable, [sys.executable, own_path])
    except Exception as e:
        log(f"self-update failed (non-fatal, still on {RAIL_VERSION}): {e}")


def stream_once(token, last_event_id):
    """One SSE connection. Returns the last event id seen (for resume)."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    req = urllib.request.Request(f"{API}/events", headers=headers)
    resp = urllib.request.urlopen(req, timeout=IDLE_TIMEOUT)
    connected_at = time.time()
    log(f"connected (resume from id {last_event_id})" if last_event_id
        else "connected")

    # Fleet registration + update check (doc #22): every connect refreshes the
    # registry's view of this rail; a deploy restarts the registry, drops all
    # SSE clients, and this line is how the whole fleet self-updates minutes
    # later. Both calls fail open.
    if register(token).get("update_available"):
        self_update(token)
    # Skills sync EVERY connect, outside the update_available gate (2.7.2).
    # Before this, BOTH install and refresh sat inside self_update(), which only
    # runs when register() reports a newer artifact — and `update_available` is
    # computed over {listen, wake, rail, notif-hook, stop-hook, wsl-boot}, of
    # which SKILLS ARE NOT A MEMBER. So all skill distribution was coupled to
    # some other component's release cadence: a skill-only correction could not
    # reach a seat that was otherwise current, no matter how urgent.
    # Two further consequences, both measured on staging #91 rather than argued:
    #   - a version's own install branch can never run during its own tenure
    #     (while behind you execute the OLD code; once current, self_update is
    #     not called). 2.7.1's install fired exactly once, one second before
    #     re-exec'ing into 2.7.2 — on the way OUT of the version that added it.
    #   - nothing was being swallowed; the line was simply never reached.
    # Skills version independently of the listener, so they need a trigger that
    # does not depend on the listener changing. Cost is one GET /skills per
    # connect; failures are logged rather than swallowed, because a silent
    # no-op here is the whole bug.
    sync_skills(token)
    # sync_hooks() retired (2.7.10): the interactive hooks were the pre-feed
    # shape (a prompt-time digest and a Stop heartbeat); a session hears through
    # the subscribe helper. Installed copies on seats are removed by hand.

    # Mandatory reconcile (doc #17): trust the inbox, not the stream.
    wake("reconcile-on-connect", {"reason": "connect"}, token)
    V3_HB["last"] = time.time()     # reconcile IS an inbox poll: a heartbeat
                                    # (if a seat turned one on) counts from here

    cur = {}
    while True:
        raw = resp.readline()          # > IDLE_TIMEOUT silence -> socket.timeout
        if not raw:
            log("stream EOF")
            return last_event_id
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        # Periodic clocks — evaluated at every BLOCK BOUNDARY (`cur` empty, so
        # a reconnect here can never cut a half-read event), NOT only on
        # keepalives. Until 2.7.8 they lived in the keepalive branch, which
        # made every one of them hostage to the server's keepalive: /events
        # resets its 15s timer on any byte written to THIS client, real events
        # included (#184), so a stream busier than one visible event per 15s
        # never emits a `:` line. Admin identities see every fleet event, so
        # the busiest streams belonged to exactly the seats whose clocks
        # mattered most — measured on the admin seat: age-out 1h22m
        # late one day and 1h48m late another, the inbox unpolled
        # across both windows with a `wake.dark` page sitting in it (the one
        # row with no stream event: an inbox poll is its only delivery path).
        if not cur:
            # Reap at the block boundary too, not only after a wake. An orphan
            # appears when the DETACHED holder dies, which is long after the wake
            # that launched it returned — on a seat that then goes quiet, reaping
            # only on wake leaves the zombie until the next event, which may be
            # hours. Same safety argument as the post-wake call: a block boundary
            # is main-loop code with no subprocess.run in flight. Near-free when
            # there is nothing to reap (one WNOHANG waitpid returning ECHILD).
            reap_orphans()
            if RAIL_V3:
                # Optional periodic inbox poll (see V3_HB above; off by
                # default): one inbox_state per interval, and only a
                # non-empty inbox spawns the handler.
                if V3_HB_SEC > 0 and time.time() - V3_HB["last"] > V3_HB_SEC:
                    V3_HB["last"] = time.time()
                    has_unread, _ = inbox_state(token)
                    if has_unread:
                        wake("v3-heartbeat", {"reason": "heartbeat"}, token)
            elif DEFERRED["pending"]:
                # v2 only: claimed-only work was deferred to a live surface —
                # if its claim lapsed (surface died or released), reconcile
                # the backlog. Polls only while deferred work exists, bounded
                # by claim TTL (#199).
                has_unread, has_unclaimed = inbox_state(token)
                if has_unclaimed:
                    wake("deferred-catchup", {"reason": "catchup"}, token)
                elif not has_unread:
                    DEFERRED["pending"] = False
            # #438: a healthy connection never idles out (that's the point of
            # the keepalive) and can therefore go days without ever revisiting
            # register()/self_update() in stream_once's connect prologue. Age
            # out on our own schedule instead of waiting for the socket to.
            if time.time() - connected_at > MAX_CONN_AGE:
                log(f"connection age > {MAX_CONN_AGE}s — reconnecting for update check")
                resp.close()
                return last_event_id
        if line.startswith(":"):
            continue                # keepalive: the clocks above are all it drove
        if line.startswith("id:"):
            cur["id"] = line[3:].strip()
        elif line.startswith("event:"):
            cur["event"] = line[6:].strip()
        elif line.startswith("data:"):
            cur["data"] = line[5:].strip()
        elif line == "" and cur:
            if cur.get("id", "").isdigit():
                last_event_id = int(cur["id"])
            etype = cur.get("event", "")
            if etype and etype not in IGNORE_TYPES:
                try:
                    payload = json.loads(cur.get("data", "{}"))
                except ValueError:
                    payload = {"raw": cur.get("data", "")}
                wake(f"event {etype} (id {cur.get('id')})",
                     {"reason": "event", "event": payload}, token)
            cur = {}


def main():
    last_event_id = None
    backoff = 1.0
    log(f"samcloud-listen v{RAIL_VERSION} starting (api={API}, wake_cmd={WAKE_CMD})")
    # #567 §5: two listeners of one Unix user (prod + staging pilot) have
    # identical argv — ps cannot tell them apart, and killing the wrong one
    # is a live footgun. A pidfile next to this config's log names this
    # instance: pid + api env + conf path.
    try:
        with open(LOG_PATH + ".pid", "w") as f:
            f.write(f"{os.getpid()} api={API} conf={CONF}\n")
    except OSError:
        pass
    while True:
        started = time.time()
        try:
            # #473: the token file is the source of truth and can be
            # re-provisioned under a running listener (rotation, repair).
            # A process-lifetime cache turns any rotation into a 401 loop
            # that only a restart clears — 4h43m deaf on 2026-08-16.
            token = open(TOKEN_PATH).read().strip()
            # #582 closure (2.6.5/2.6.6): the taught auth is a header FILE, but
            # established seats never re-run wake-install, so nothing created it —
            # and the natural fallback on a missing file is the argv form the fix
            # retires (#87 c-1198/c-1205). The listener is the one process alive
            # on every seat, so it maintains `<its own token file>.hdr` — plane-
            # scoped by construction, so a prod header can never be confused with
            # a staging one (#87 c-1243: the plane-ambiguous `hdr` name let
            # ground truth pair a PROD bearer with a staging $API). The legacy
            # `hdr` name stays beside the DEFAULT token only, for stamped text.
            # Refreshed per connect, so rotation propagates here too (#473).
            def _hdr(path):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as hf:
                    hf.write(f"Authorization: Bearer {token}\n")
            try:
                _hdr(TOKEN_PATH + ".hdr")
                if os.path.basename(TOKEN_PATH) == "token":
                    _hdr(os.path.join(os.path.dirname(TOKEN_PATH), "hdr"))
            except OSError as e:
                log(f"hdr maintain failed (non-fatal): {e}")
            last_event_id = stream_once(token, last_event_id)
        except socket.timeout:
            log(f"idle >{IDLE_TIMEOUT}s — assuming dead socket")
        except KeyboardInterrupt:
            log("interrupted — exiting")
            return 0
        except Exception as e:
            log(f"stream error: {e.__class__.__name__}: {e}")
        if time.time() - started > 60:
            backoff = 1.0               # healthy run — reset backoff
        delay = min(backoff, 30) + random.uniform(0, 1)
        log(f"reconnecting in {delay:.1f}s")
        time.sleep(delay)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    sys.exit(main())
