#!/usr/bin/env python3
"""samcloud-rail — wake rail v3 handler: per-thread instances (doc 174).

Invoked by samcloud-listen per event (payload on stdin), or manually. The
durable inbox is the source of truth: rows are fetched unread AND
undelivered, grouped by THREAD (ticket / doc / task / lobby), passed through
the delivery rule (`deliverable`), and each delivered group takes one verb:

    wake      no instance                       -> birth, bind, deliver
    continue  a live instance holds the thread  -> deliver into it
    closing   the batch carries the object's close (resolved/closed/done/
              cancelled) -> deliver it with the wrap-up; the instance ends
              itself when its turn is over
    resume    the instance's process is gone but the object is open -> the
              client reconnects to its own conversation log (native resume,
              same conversation id) and streams again
    oneshot   the process is gone AND the object is resolved -> one native
              client resume, answer, exit; no resident
    idle      nothing to deliver (a close for a thread with no instance, or
              the one-shot counter exhausted)
    closed    (sweep) the process is gone -> the thread is closed. Observed,
              never enforced: no timer, no reaper, no grace.

Delivery is receipt: a row delivered to a conversation is never offered
again to anyone (`POST /notifications/delivered`); ack is the reader's
bookkeeping. No clock anywhere in the lifecycle — a thread lives as long as
its object is open and its process runs, whether that is a minute or a
month. The dispatch lock is held only for the seconds dispatch takes and
dies with this process (flock semantics), so a crashed handler never wedges
the seat.

Opt-in per seat: point WAKE_CMD at this script. Config rides the same
~/.config/samcloud/wake.env as v2, plus RAIL_* keys (see _conf defaults).
The v2 handler (samcloud-wake.sh) is untouched; it self-updates fleet-wide,
which is exactly why the v3 cut is a separate file (doc 161 §3).

Runtimes (per-seat, RAIL_RUNTIME): Claude streams by default. RAIL_STREAM_CMD
opts another client into the same FIFO lifecycle; the client owns its native
conversation mapping, inference, recovery and replies. Without that command,
other runtimes use RAIL_RUNTIME_CMD through the batch adapter. The verbs and
plane bindings are unchanged; runtime capability is declared, not guessed.

Holders (per-seat choice, RAIL_HOLDER):
  tmux    interactive claude in a tmux window per thread (session per env,
          window per thread). Humans can attach. Window
          presence is liveness (the launch script execs claude).
  stream  headless Claude or a configured client on a FIFO — the
          persistent idle -p proven in task 594. The holder inherits the
          FIFO opened O_RDWR as stdin, so it is its own EOF-keeper: writer
          count never reaches zero between deliveries.

Binding: the plane's `threads` record (conversation id <-> object <->
identity, `POST /wake/threads`) is authoritative; ~/.samcloud/rail-v3-*/
threads.json is this seat's cache of the same rows plus seat facts (pid,
FIFO, output file). Holders carry SAMCLOUD_SESSION_ID = the harness uuid, so
every plane write they make is stamped with the conversation the binding
names — which is how a reply on a ticket a held instance FILED routes back
into that instance (`_resolve_entry`).
"""

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

RAIL_VERSION = "3.18.0"   # The plane is named from the serving plane's config, not a hostname.

# ── config ───────────────────────────────────────────────────────────────────

def _read_env_file(path: Path) -> dict:
    """KEY=VALUE lines (export-prefixed ok); no shell evaluation, values with
    $(...) or backticks are ignored rather than half-parsed."""
    out = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            quote, escaped = None, False
            for i, char in enumerate(v):
                if escaped:
                    escaped = False
                elif char == "\\" and quote != "'":
                    escaped = True
                elif quote:
                    if char == quote:
                        quote = None
                elif char in ("'", '"'):
                    quote = char
                elif char == "#" and (i == 0 or v[i - 1].isspace()):
                    v = v[:i].rstrip()
                    break
            if quote:
                continue
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if "$(" in v or "`" in v:
                continue
            out[k.strip()] = v
    except OSError:
        pass
    return out


HOME = Path.home()
_file_conf = _read_env_file(Path(os.environ.get(
    "SAMCLOUD_WAKE_CONF", HOME / ".config/samcloud/wake.env")))


def _conf(key: str, default: str) -> str:
    return os.environ.get(key) or _file_conf.get(key) or default


API = _conf("API", "__API__")
# The rail KNOWS its plane — it is reading its own API out of its own conf.
# Holders must never have to infer it. A skill that guessed the plane from the
# header file's PATH misclassified a live staging holder whose state dir is
# named plain "rail-v3" (no marker), and would have passed a staging bearer to
# prod (staging #91). Declared beats inferred.
#
# The NAME comes from the planes the serving plane names (SAMCLOUD_PLANES, else
# itself under SAMCLOUD_ENV), rendered in when this file is served (#1010). It
# matched one instance's staging hostname before. PLANE in wake.env overrides.
try:
    _PLANES = json.loads('__PLANES_JSON__')
except ValueError:          # an unrendered copy, run from a checkout
    _PLANES = {}
_SERVED_ENV = "__ENV_NAME__"
PLANE = (_conf("PLANE", "")
         or next((n for n, u in _PLANES.items() if API.rstrip("/") in (u, u + "/api/v1")), "")
         or (_SERVED_ENV if not _SERVED_ENV.startswith("__") else "prod"))
TOKEN_PATH = Path(_conf("TOKEN_PATH", str(HOME / ".samcloud/token")))
MODEL = _conf("MODEL", "claude-opus-5")   # an id, never an alias (doc 255 D11): `opus` meant opus-4-8 on one seat and opus-5 on the rest
PERMISSION_ARGS = _conf("PERMISSION_ARGS", "--dangerously-skip-permissions")
HOLDER = _conf("RAIL_HOLDER", "stream")          # stream | tmux — stream is what production runs; tmux is the interactive layout (3.9.16: the safe default)
# Claude uses the built-in streaming command. Other clients opt into the same
# FIFO lifecycle explicitly; without it, a runtime uses the batch adapter.
# Capability is declared, never guessed from the runtime or model's name.
RUNTIME = _conf("RAIL_RUNTIME", "claude")
STREAM_CMD = _conf("RAIL_STREAM_CMD", "")
RUNTIME_STREAMS = RUNTIME == "claude" or bool(STREAM_CMD)
if STREAM_CMD and HOLDER != "stream":
    raise ValueError("RAIL_STREAM_CMD requires RAIL_HOLDER=stream")
ADAPTER_BIN = _conf("RAIL_ADAPTER", str(HOME / ".local/bin/samcloud-adapter"))
TMUX_SESSION = _conf("RAIL_TMUX_SESSION", "sc-rail")
STATE_DIR = Path(_conf("RAIL_STATE_DIR", str(HOME / ".samcloud/rail")))
LOG_PATH = Path(_conf("LOG", str(HOME / "logs/samcloud-wake.log")))
PAYLOAD_CAP = max(4, int(_conf("WAKE_PAYLOAD_CAP_KB", "24"))) * 1024
REDELIVER_MIN = int(_conf("RAIL_REDELIVER_MIN", "30"))  # seen-lease length only (3.9): re-offer is gone, delivery is receipt
# Holder cwd: must be a dir the seat's harness already trusts (else interactive
# holders stall on the folder-trust dialog) and — #226 — NEVER under
# ~/Documents / ~/Desktop / ~/Downloads when spawned from launchd.
HOLDER_CWD = _conf("RAIL_CWD", _conf("WAKE_CWD", str(HOME)))
# Birth policy (#567 round 2 — mention-driven session sprawl; rail 3.9 makes
# it the delivery rule, `deliverable`): a subscription alone must not birth a
# held session, or every cross-pilot poke turns the social graph into the
# session graph. Only what opens a thread toward you births one; everything
# else stays in the inbox. Nothing routes to a lobby session any more.
# Settle window (#87 c585: rate-damping is gone — back and forth is
# fine; the job is debouncing so near-simultaneous repliers COME IN TOGETHER
# instead of as parallel crossing turns). After a non-empty poll the rail
# waits this long and re-polls, so messages posted within a few seconds of
# each other land in ONE chronological batch per thread. That is the whole
# congestion mechanism now — nothing is ever slowed, held, or lectured.
SETTLE_SEC = float(_conf("RAIL_SETTLE_SEC", "8"))
# Thread lifecycle (doc 174 / rail 3.9): a thread lives while its object is
# open. No timers anywhere: a `closing` holder runs until
# its own turn is over and ends itself — the rail marks `closed` when the
# process is gone, an observation, not a deadline. A thread with no close
# event (a doc) closes when its holder decides it is done and exits. A closed
# thread's late messages get this many one-shot resumes (a counter, bounding
# failure — not a clock) before they are left for a human.
ONESHOT_MAX = int(_conf("RAIL_ONESHOT_ATTEMPTS", "3"))
# Per-row digest body cap. 400 sliced the operator's own 409-char message
# mid-word (2026-08-31); 4000 truncated two real messages in the #87 stress
# test. 16000 makes truncation "basically never" (c585) — a row that
# still cuts carries truncated:true, its true length and the exact GET.
DIGEST_BODY_CAP = int(_conf("RAIL_DIGEST_BODY_CHARS", "16000"))
# tmux delivery ceiling (#567 c5017): one send-keys -l payload beyond the
# server's command-size tolerance fails the dispatch — and a failed dispatch
# re-offers forever. 8000 is comfortably under the measured failure region
# while fitting several whole typical messages.
TMUX_MAX_CHARS = int(_conf("RAIL_TMUX_MAX_CHARS", "8000"))
DRY = _conf("RAIL_DRY", "") == "1"
# Where holders run claude from — a fixed path that keeps macOS folder grants
# across Claude Code updates (claude_exe). "off" = `claude` from PATH.
CLAUDE_STABLE = _conf("RAIL_CLAUDE_STABLE", str(HOME / ".local/samcloud/claude-stable/claude"))
if CLAUDE_STABLE == "off":
    CLAUDE_STABLE = ""
FAKE_INBOX = _conf("RAIL_FAKE_INBOX", "")        # test hook: path to a rows-JSON file
# Harness-credential mint (task 594 finding): a setsid-detached holder cannot
# use or refresh keychain OAuth ("OAuth session expired and could not be
# refreshed" — measured 2026-08-31), so long-held holders authenticate with a
# per-launch token minted by the google-broker (claude-run pattern, doc 33).
# Empty = inherit ambient auth (seats whose credential store works detached).
MINT_URL = _conf("RAIL_TOKEN_MINT_URL", "")
# The broker validates against PROD; a rail pointed at staging carries a
# staging TOKEN_PATH the broker would refuse — so mint auth gets its own
# token path, defaulting to the rail's own.
MINT_TOKEN_PATH = Path(_conf("RAIL_MINT_TOKEN_PATH", str(TOKEN_PATH)))

# The launcher usually lives in ~/.local/bin; supervised listeners don't
# reliably have it on PATH (#153) — mirror the v2 handler's fix.
os.environ["PATH"] = (f"{HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:"
                      + os.environ.get("PATH", "/usr/bin:/bin"))


def log(msg: str) -> None:
    line = f"[{time.strftime('%F %T')}] rail3: {msg}\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        sys.stderr.write(line)


def _token() -> str:
    try:
        return TOKEN_PATH.read_text().strip()
    except OSError:
        return ""


def _write_0600(path: Path, text: str) -> None:
    """A credential file is BORN 0600 — created with the mode, never created
    at umask and narrowed after (#615/#637: write_text-then-chmod leaves the
    bearer world-readable for the gap, and a 0700 parent is a seat property,
    not a property of the write)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)            # an existing file keeps 0600 too


def _api(method: str, path: str, body: dict = None, timeout: int = 20):
    tok = _token()
    if not tok:
        raise RuntimeError(f"no token at {TOKEN_PATH}")
    req = urllib.request.Request(
        API + path, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ── thread model ─────────────────────────────────────────────────────────────

def thread_key(row: dict) -> str:
    """One unit of work, one key (doc 239): `thread-<id>` from the row's
    stamp. A row from before the universal thread keys on its object; unkeyed
    events land in the seat's lobby."""
    tid = row.get("thread_id")
    if tid is not None:
        return f"thread-{tid}"
    for field, prefix in (("ticket_id", "ticket"), ("doc_id", "doc"),
                          ("task_id", "task")):
        v = row.get(field)
        if v is None:
            v = (row.get("payload") or {}).get(field)
        if v is not None:
            return f"{prefix}-{v}"
    return "lobby"


def thread_ref(key: str) -> str:
    """A shared conversation id, distinct from any member's work title."""
    if key == "lobby":
        return "general inbox (lobby)"
    if key.startswith("thread-"):
        return f"thread {key[7:]}"
    return key.replace("-", " #", 1)


def _row_ref(r: dict) -> str:
    """The object a row sits on: `ticket #706`, `task #648`, `doc #237`."""
    for field, prefix in (("ticket_id", "ticket"), ("doc_id", "doc"), ("task_id", "task")):
        v = r.get(field)
        if v is None:
            v = (r.get("payload") or {}).get(field)
        if v is not None:
            return f"{prefix} #{v}"
    return ""


_THREAD_INFO: dict = {}   # per-run cache of GET /threads/{id}


def _thread_info(key: str) -> dict:
    """The universal thread behind a `thread-<id>` key: root, state, title,
    open_members. One GET per run; an unreadable thread reads as empty."""
    if key not in _THREAD_INFO:
        info = {}
        if key.startswith("thread-") and key[7:].isdigit():
            try:
                info = _api("GET", f"/threads/{key[7:]}") or {}
            except Exception as e:
                log(f"{key}: thread read failed ({e})")
        _THREAD_INFO[key] = info
    return _THREAD_INFO[key]


def _thread_id_of(key: str):
    """The universal thread id for a key: direct for `thread-N`; looked up
    for a pre-v19 object key (the plane creates the thread if the object has
    none)."""
    if key.startswith("thread-") and key[7:].isdigit():
        return int(key[7:])
    plane, _, oid = key.partition("-")
    if plane in ("ticket", "doc", "task") and oid.isdigit():
        try:
            return (_api("GET", f"/threads/of/{plane}/{oid}") or {}).get("id")
        except Exception as e:
            log(f"{key}: thread lookup failed ({e})")
    return None


def migrate_keys(reg: dict) -> list:
    """Re-key pre-v19 entries (`ticket-702`) to their universal thread
    (`thread-331`), once, asking the plane. The entry's directory keeps its
    old name; a symlink under the new key points at it, so a live holder's
    fifo and out file stay where its process opened them."""
    notes = []
    for key in list(reg):
        plane, _, oid = key.partition("-")
        if plane not in ("ticket", "doc", "task") or not oid.isdigit():
            continue
        if reg[key].get("unmigratable"):
            continue
        tid = _thread_id_of(key)
        if not tid:
            # the plane refused or has no thread for it (a pre-v19 entry on an
            # object this seat can no longer read): asked once, not every
            # pass (3.11.1 — two 403s per event on one seat, forever)
            reg[key]["unmigratable"] = True
            continue
        new = f"thread-{tid}"
        entry = reg.pop(key)
        entry["key"] = new
        entry["object_key"] = key
        if new in reg:                      # two objects of one thread: keep the live one
            other = reg[new]
            if other.get("state", "open") in ("open", "closing") and holder_alive(other):
                reg[new] = other
                continue
        reg[new] = entry
        old_dir, new_dir = STATE_DIR / "threads" / key, STATE_DIR / "threads" / new
        try:
            if old_dir.is_dir() and not new_dir.exists():
                new_dir.symlink_to(old_dir.name)
        except OSError as e:
            log(f"{key}: could not link {new}: {e}")
        notes.append(f"{key} -> {new}")
    return notes


# ── seat-local binding registry ──────────────────────────────────────────────

QUIET_PATH = STATE_DIR / "quiet.json"


# Bump this ONLY when `deliverable()` would judge an old row differently.
# Not on a version bump for anything else (3.11.6).
QUIET_EPOCH = 1


def load_quiet() -> dict:
    """Rows this rail has judged inbox-only, by thread key → highest row id
    (3.11.0). A judged row is not judged again: before this every pass
    re-read the thread and its bindings for every undeliverable row it had
    ever seen — thirty rows and two 403s per event on one seat, forever —
    and the log repeated the verdicts until the real decisions were hard to
    find.

    The marks used to be keyed on RAIL_VERSION, so **every ship re-judged
    everything every seat had ever ignored, fleet-wide** (3.11.6). Shipping
    3.11.5 did exactly that: a native-client seat was woken as holder of
    a pre-v19 `thread-615` entry with a null thread_id and bodies dated
    two weeks earlier, and its reply target 404s — a holder with no reply path,
    started by a deploy that had nothing to do with it. A rail ship is not a
    reason to reconsider two weeks of decisions; a change to the DELIVERY RULE
    is, and that is what QUIET_EPOCH names. Marks written by an older epoch are
    dropped; marks written by any version of this epoch are kept."""
    try:
        q = json.loads(QUIET_PATH.read_text())
        # `rail` is the legacy key: a file from before 3.11.6 carries a version
        # where the epoch goes. Treat it as epoch 1 — the judging rule has not
        # changed since those marks were written, so they are still good.
        epoch = q.get("epoch", 1 if q.get("rail") else None)
        if epoch == QUIET_EPOCH:
            return {k: int(v) for k, v in (q.get("marks") or {}).items()}
    except (OSError, ValueError):
        pass
    return {}


def save_quiet(marks: dict) -> None:
    try:
        _write_0600(QUIET_PATH, json.dumps({"epoch": QUIET_EPOCH, "rail": RAIL_VERSION,
                                            "marks": marks}))
    except OSError as e:
        log(f"quiet marks not saved (non-fatal): {e}")


_SURFACE_HELD: set = set()    # keys judged inbox-only because a connected surface holds them (never marked quiet)


def load_registry() -> dict:
    p = STATE_DIR / "threads.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_registry(reg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "threads.json.tmp"
    tmp.write_text(json.dumps(reg, indent=1))
    tmp.replace(STATE_DIR / "threads.json")


def claude_exe() -> str:
    """The claude a holder runs: a copy at a fixed path, refreshed when the
    install moves (3.13.3). macOS keys a Documents/Desktop/Downloads grant to
    the executable's path plus its signature, and Claude Code installs every
    release at a new path (~/.local/share/claude/versions/<v>) — so each
    auto-update arrives ungranted, and a holder's first touch of ~/Documents
    asks a question nobody can answer: the open() blocks forever. On
    2026-10-06 three admin holders hung that way within minutes of the rail
    being unparked. The copy keeps one path and the same signature, so a
    grant made once survives every update. RAIL_CLAUDE_STABLE=off runs
    `claude` from PATH as before."""
    if not CLAUDE_STABLE:
        return "claude"
    try:
        found = next((str(Path(d) / "claude") for d in os.environ.get("PATH", "").split(":")
                      if d and os.access(Path(d) / "claude", os.X_OK)), None)
        if not found:
            return "claude"
        real = os.path.realpath(found)
        stable = Path(CLAUDE_STABLE)
        if real == str(stable):
            return real
        mark = stable.with_name(stable.name + ".from")
        st = os.stat(real)
        want = f"{real} {st.st_size} {int(st.st_mtime)}"
        if not (stable.exists() and mark.exists() and mark.read_text().strip() == want):
            stable.parent.mkdir(parents=True, exist_ok=True)
            tmp = stable.with_name(f".{stable.name}.{os.getpid()}")
            subprocess.run(["cp", "-p", real, str(tmp)], check=True, timeout=120)
            tmp.replace(stable)          # a running holder keeps the old inode
            mark.write_text(want + "\n")
            log(f"claude: stable copy refreshed from {real}")
        return str(stable)
    except Exception as e:                # never block a birth on this
        log(f"claude: stable copy unavailable ({e}); running claude from PATH")
        return "claude"


def session_file_exists(sid: str) -> bool:
    """Harness session store: ~/.claude/projects/<escaped-cwd>/<sid>.jsonl —
    resume is not cwd-scoped (RFC 157 2.6), so search every project dir.

    This is one runtime's store. A seat running anything else keeps its
    conversation handle in its client mapping or the batch adapter's record."""
    if RUNTIME != "claude" or STREAM_CMD:
        return False
    root = HOME / ".claude/projects"
    try:
        return any(root.glob(f"*/{sid}.jsonl"))
    except OSError:
        return False


# ── holders ──────────────────────────────────────────────────────────────────

def _tmux(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True)
    except FileNotFoundError:
        # no tmux on this seat: every tmux question answers "no" (a tmux
        # holder cannot exist here), instead of a traceback out of sweep
        return subprocess.CompletedProcess(["tmux", *args], 127, "", "tmux: not installed")


def _tmux_window(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", key)


def tmux_alive(key: str) -> bool:
    r = _tmux("list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}")
    if r.returncode != 0 or _tmux_window(key) not in r.stdout.split():
        return False
    # #565: an unauthenticated harness idles on its login screen forever —
    # window presence said "alive" while every delivery fell into a pane
    # that will never act (the tmux twin of the stream Not-logged-in zombie,
    # #568 r2). Declaring it dead routes it through the normal reap path:
    # window killed, claims released, rebirth with --resume + a fresh mint
    # (receipt in mint.log). A pane merely QUOTING the marker can false-
    # positive; the cost is one same-sid rebirth, the conversation survives.
    cap = _tmux("capture-pane", "-p", "-t", f"{TMUX_SESSION}:{_tmux_window(key)}")
    if cap.returncode == 0 and "Not logged in" in cap.stdout \
            and "/login" in cap.stdout:
        log(f"{key}: tmux holder is auth-dead (login screen in pane) — "
            f"treating as dead for reap/rebirth")
        return False
    return True


def stream_alive(entry: dict) -> bool:
    pid = entry.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                       capture_output=True, text=True)
    if STREAM_CMD:
        # This is process ownership, not runtime/model health. A custom client
        # exits on its own transport failures; Claude's auth heuristics do not
        # apply. The UUID distinguishes a reused pid or a sibling process.
        sid = entry.get("sid")
        return bool(sid and re.search(r"--(?:resume|session-id) " + re.escape(sid)
                                      + r"(?:\s|$)", r.stdout))
    if "claude" not in r.stdout:
        return False
    # Capability-level liveness (#568 §4): an unauthenticated holder idles on
    # its FIFO looking process-healthy forever ("Not logged in", result:
    # success). The stream holder's structured stdout makes the check cheap —
    # an AUTH-dead last result means treat the holder as dead so dispatch
    # RESUMEs it with a fresh mint. Auth text ONLY: `is_error` is a turn's
    # outcome, not the process's liveness. A usage-limit pause arrives as
    # {"is_error": true, "total_cost_usd": 0, "result": "You've hit your
    # session limit · resets 9pm"} and the process takes the next CONTINUE
    # fine; reading it as death reaped a healthy holder and RESUMEd it (full
    # transcript replay, $8-10 a turn) on every pass — 1155 times in one day
    # on one seat (#623, measured 2026-09-05).
    try:
        # Per-generation output (#568 r2): a shared append-only out.jsonl let
        # a dead predecessor's poisoned last-result mark a healthy successor
        # dead. Each holder generation gets its own file, recorded on the
        # binding; legacy bindings fall back to the shared name.
        out = STATE_DIR / "threads" / entry["key"] / entry.get("out", "out.jsonl")
        with open(out, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 16384))
            tail = f.read().decode("utf-8", "replace")
        last = None
        for line in tail.splitlines():
            if '"type":"result"' in line or '"type": "result"' in line:
                try:
                    last = json.loads(line)
                except ValueError:
                    pass
        if last is not None:
            text = str(last.get("result") or "")
            if text.startswith(("Not logged in", "Failed to authenticate")):
                log(f"{entry['key']}: holder pid {pid} alive but auth-dead "
                    f"({text[:60]!r}) — treating as dead")
                return False
    except OSError:
        pass
    return True


def _adapter_pids(entry: dict) -> list:
    """Every adapter turn started on this thread that may still be running.

    A second delivery while a turn runs starts a second adapter, which waits on
    the seat's turn lock. Recording only the newest pid meant the waiter's exit
    read as the thread's end: on one local-model seat the binding on a ticket was
    closed at 10:55 while the turn that held it ran on until 11:23 (#832)."""
    pids = list(entry.get("pids") or [])
    if entry.get("pid") and entry.get("pid") not in pids:
        pids.append(entry.get("pid"))
    return pids


def holder_alive(entry: dict) -> bool:
    if entry.get("holder") == "adapter":
        # A turn IS the holder: alive means SOME turn on this thread is running
        # right now, not only the newest. Between turns nothing holds, which is
        # the honest report — continuity lives in the runtime's conversation store.
        return any(_pid_alive(p) for p in _adapter_pids(entry))
    return tmux_alive(entry["key"]) if entry.get("holder") == "tmux" \
        else stream_alive(entry)


def _adapter_conv(key: str) -> str:
    """The runtime's own handle for this thread's conversation, recorded by
    the adapter after each turn. Empty means this thread has never run."""
    try:
        return (STATE_DIR / "threads" / key / "conv").read_text().strip()
    except OSError:
        return ""


def _mint_oauth() -> str:
    if not MINT_URL or RUNTIME != "claude" or STREAM_CMD:
        return ""
    try:
        mint_tok = MINT_TOKEN_PATH.read_text().strip()
        req = urllib.request.Request(
            MINT_URL, headers={"Authorization": f"Bearer {mint_tok}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("token", "")
    except Exception as e:
        log(f"oauth mint failed (falling back to ambient auth): {e}")
        return ""


def _holder_env(sid: str) -> dict:
    """Env for the held session. SAMCLOUD_SESSION_ID = the harness uuid, so
    the session's plane writes stamp claims with the SAME id the binding
    holds — the claim becomes a plane-visible mirror of the local binding."""
    # #568 §6.4: when the rail is run from inside a harness session (manual
    # dispatch, testing), the launching session's harness-scoped env leaks
    # into holders — one holder reported its parent's session id as its own.
    # Holders get a clean slate of harness vars.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "AI_AGENT")) and k != "SAMCLOUD_CLIENT_PID"}
    # NO bearer in the holder's env (#87 c-1201/c-1208): redundant once
    # auth is by path, inherited by every child the holder spawns, and its
    # existence forced the "do NOT expand $SAMCLOUD_TOKEN" contract line.
    # (Whether harnesses also record env VALUES into transcripts is seat-
    # disputed — the removal is right either way.) Holders auth via
    # auth.hdr / the token file, by path. The PATH itself rides env
    # (non-secret, safe to expand): injected text must never carry
    # `@/abs/path` — the harness reads @-mentioned files into the
    # transcript, which delivered the bearer alongside the very rule
    # forbidding that (#87 c-1239, 2/2 contract updates vs 0/9 others).
    env.update({"SAMCLOUD_SESSION_ID": sid, "SAMCLOUD_API": API,
                "SAMCLOUD_PLANE": PLANE,
                # Auto-memory is ON (3.14.0): the harness loads
                # ~/.claude/projects/<cwd>/memory/MEMORY.md for a `-p` session as
                # for any other, keyed on the cwd. A holder is a new chat opened
                # as the seat, and the seat's memory is its own. (3.11.0 had
                # turned it off because the memories carried the register; the
                # 10-08 forensics put the register in the thread and the
                # contributing page instead — doc 359.)
                # "am I a holder?" answered by a flag instead of a heuristic
                # (3.9.19). The interactive-surface hooks must stay silent
                # inside a holder's turns, and they used to infer that from
                # SAMCLOUD_SESSION_ID being set — which a registered
                # interactive session now sets too (rail 3.10). One variable
                # only the rail writes settles it.
                "SAMCLOUD_HOLDER": "1",
                "SAMCLOUD_AUTH_HDR": str(STATE_DIR / "auth.hdr")})
    minted = _mint_oauth()
    if minted:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = minted
    return env


def _ensure_trusted_cwd() -> None:
    """Pre-trust RAIL_CWD in the harness config (#567 §5): interactive
    holders gate on the folder-trust dialog INDEPENDENTLY of
    --dangerously-skip-permissions, so an untrusted RAIL_CWD wedges every
    tmux holder at boot ($HOME included on some seats). RAIL_CWD is
    seat-chosen rail config — the seat opting into the rail is trusting its
    own configured dir. Idempotent; never touches other projects."""
    cfg_path = HOME / ".claude.json"
    try:
        data = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        proj = data.setdefault("projects", {}).setdefault(HOLDER_CWD, {})
        if not proj.get("hasTrustDialogAccepted"):
            proj["hasTrustDialogAccepted"] = True
            tmp = cfg_path.with_suffix(".json.rail-tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(cfg_path)
            log(f"pre-trusted RAIL_CWD {HOLDER_CWD} in {cfg_path}")
    except Exception as e:
        log(f"could not pre-trust RAIL_CWD (holder may hit the trust dialog): {e}")


def start_tmux_holder(key: str, sid: str, prompt: str, resume: bool) -> None:
    _ensure_trusted_cwd()
    if _tmux("has-session", "-t", TMUX_SESSION).returncode != 0:
        # Window 0 is a plain shell so the session survives any one holder.
        r = _tmux("new-session", "-d", "-s", TMUX_SESSION, "-n", "rail")
        if r.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")
    tdir = STATE_DIR / "threads" / key
    tdir.mkdir(parents=True, exist_ok=True)
    tdir.chmod(0o700)
    (tdir / "prompt.txt").write_text(prompt)
    (tdir / "prompt.txt").chmod(0o600)
    mode = ["--resume", sid] if resume else ["--session-id", sid]
    # exec so the window dies when claude exits — window presence IS liveness.
    launch = tdir / "launch.sh"
    mint_lines = ""
    if MINT_URL:
        # Fetched at exec time (fresh on every resume), never embedded on
        # disk. #565: the old form fell back to ambient auth SILENTLY — a
        # seat with correct config and a working broker still born a
        # Not-logged-in holder with zero receipt anywhere. Every attempt now
        # writes mint.log (curl rc + token length, never the token), so
        # "why is this pane unauthenticated" is one file-read.
        mlog = tdir / "mint.log"
        # #632 / #582: the bearer never enters argv. The mint header file is
        # written here (0600, python, no fork) and the launch script passes
        # its PATH — the tmux launcher used to expand `$(cat token)` into
        # curl's argv, world-readable via ps, and did so live on one seat
        # three times on 08-31 (#615 c5874).
        mint_hdr = Path(f"{MINT_TOKEN_PATH}.hdr")     # the listener maintains this same file beside the token
        try:
            if not mint_hdr.exists():
                _write_0600(mint_hdr, f"Authorization: Bearer {MINT_TOKEN_PATH.read_text().strip()}\n")
        except OSError as e:
            log(f"{key}: mint header file could not be written ({e}) — tmux mint will fall back to ambient auth")
        mint_lines = (
            f"_RAW=$(curl -sf -m 15 -H @{shlex.quote(str(mint_hdr))} {shlex.quote(MINT_URL)})\n"
            "_RC=$?\n"
            "_M=$(printf %s \"$_RAW\" | /usr/bin/python3 -I -c 'import json,sys;"
            "print(json.load(sys.stdin).get(\"token\",\"\"))' 2>/dev/null)\n"
            "if [ -n \"$_M\" ]; then\n"
            "  export CLAUDE_CODE_OAUTH_TOKEN=\"$_M\"\n"
            f"  echo \"$(date -u +%FT%TZ) mint ok (token len ${{#_M}})\" >> {shlex.quote(str(mlog))}\n"
            "else\n"
            f"  echo \"$(date -u +%FT%TZ) MINT FAILED (curl rc=$_RC, url={MINT_URL}) — "
            f"falling back to ambient auth; a 'Not logged in' pane means the "
            f"fallback has no credentials either\" >> {shlex.quote(str(mlog))}\n"
            "fi\n")
    launch.write_text(
        "#!/bin/bash\n"
        f"export SAMCLOUD_SESSION_ID={shlex.quote(sid)} "
        f"SAMCLOUD_API={shlex.quote(API)} "
        f"SAMCLOUD_PLANE={shlex.quote(PLANE)} "
        f"SAMCLOUD_HOLDER=1 "
        f"SAMCLOUD_AUTH_HDR={shlex.quote(str(STATE_DIR / 'auth.hdr'))}\n"
        # No SAMCLOUD_TOKEN export (#87 c-1201 vector 1): an env bearer is
        # recorded into the transcript at session start, automatically.
        + mint_lines +
        # #569 (reintroduced #432 class, caught by the staging pilot): argv is
        # world-readable via ps ACROSS uids, so the wake prompt — which embeds
        # untrusted notification bodies — must never ride the command line.
        # argv carries only a content-free pointer; the payload stays in the
        # 0600 prompt file the session reads as its first act.
        f"exec claude {' '.join(mode)} --model {shlex.quote(MODEL)} "
        f"--add-dir {shlex.quote(str(TOKEN_PATH.parent))} {PERMISSION_ARGS} "
        f"\"Rail v3 wake: read {tdir / 'prompt.txt'} now and follow its "
        f"contents as your prompt for this turn.\"\n")
    launch.chmod(0o700)
    r = _tmux("new-window", "-d", "-t", TMUX_SESSION, "-n", _tmux_window(key),
              "-c", HOLDER_CWD, str(launch))
    if r.returncode != 0:
        raise RuntimeError(f"tmux new-window failed: {r.stderr.strip()}")


def deliver_tmux(key: str, text: str) -> None:
    target = f"{TMUX_SESSION}:{_tmux_window(key)}"
    flat = " ".join(text.split())          # send-keys is line-oriented; flatten
    # #567 c5017: send-keys has a command-length ceiling (well under
    # PAYLOAD_CAP); a 56-row backlog digest wedged a held thread PERMANENTLY
    # (send-keys fails -> rows never marked delivered -> re-offered on every
    # event, forever — only a manual ack broke it). Stream holders write a
    # FIFO and are immune; tmux degrades to a pointer instead: the contract
    # already requires reading full bodies from the API, so a pointer loses
    # nothing but convenience.
    if len(flat) > TMUX_MAX_CHARS:
        flat = (f"THREAD EVENT ({thread_ref(key)}): a large backlog digest "
                f"({len(flat)} chars) exceeds the tmux delivery ceiling. "
                f"Review GET {API}/notifications?unread=true, read the full "
                f"bodies on this thread, and ack ONLY ids you handle.")
    r = _tmux("send-keys", "-t", target, "-l", flat)
    if r.returncode != 0:
        raise RuntimeError(f"send-keys failed: {r.stderr.strip()}")
    time.sleep(0.4)                        # let the input box settle (field data:
    _tmux("send-keys", "-t", target, "Enter")   # blind instant Enter misfires)


def _stream_command(sid: str, resume: bool, *, oneshot=False) -> list:
    mode = ["--resume" if resume else "--session-id", sid]
    if STREAM_CMD:
        return shlex.split(STREAM_CMD) + mode + (["--oneshot"] if oneshot else [])
    output = ["--output-format", "json"] if oneshot else [
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    return [claude_exe(), "-p", *output, *mode, "--model", MODEL,
            "--add-dir", str(TOKEN_PATH.parent), *shlex.split(PERMISSION_ARGS)]


def _wait_stream_ready(path: Path, pid: int, sid: str) -> None:
    """Wait for this client's input endpoint, not for inference or a reply."""
    deadline = time.monotonic() + 45
    with path.open() as output:
        while time.monotonic() < deadline:
            while True:
                offset = output.tell()
                line = output.readline()
                if not line.endswith("\n"):
                    output.seek(offset)  # a writer may still be finishing this record
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "ready" and event.get("plane_id") == sid:
                    return
            if not _pid_alive(pid):
                raise RuntimeError("stream client exited before accepting input")
            time.sleep(.05)
    raise RuntimeError("stream client has not confirmed its input endpoint; no message sent")


def start_stream_holder(key: str, sid: str, prompt: str, resume: bool):
    """Returns (pid, out_filename) — per-generation output file (#568 r2).

    3.9.10: the holder runs under a launcher that waits on it and, when it
    exits — wrap-up, rage-quit, crash — PATCHes the thread's binding `closed`
    on the plane, exactly as the one-shot wrapper does. Before this the
    stream close existed only in the sweep, which runs only on this
    identity's next event: with the heartbeat gone (listener 2.7.8) a quiet
    identity's row read `closing` with a dead pid for 15m28s and then
    forever (staging #104). The exit IS the event; nothing here is a clock.
    The pid recorded is claude's (the launcher writes it), so liveness and
    the reaper still see the process that matters."""
    tdir = STATE_DIR / "threads" / key
    tdir.mkdir(parents=True, exist_ok=True)
    tdir.chmod(0o700)          # transcript + fifo are same-uid only (#569)
    fifo = tdir / "in.fifo"
    if not fifo.exists():
        os.mkfifo(fifo)
    # O_RDWR stdin: the holder is its own writer, so dispatchers can open/
    # write/close the FIFO freely without ever handing it EOF (task 594).
    fd = os.open(fifo, os.O_RDWR)
    out_name = f"out-{time.time_ns()}.jsonl" if STREAM_CMD else f"out-{int(time.time())}.jsonl"
    out = open(tdir / out_name, "ab")
    err = open(tdir / "err.log", "ab")
    cmd = _stream_command(sid, resume)
    pidf, tidf = tdir / "pid", tdir / "thread_id"
    hdr = STATE_DIR / "auth.hdr"
    try:
        pidf.unlink()
    except OSError:
        pass
    # `exec 3<&0` keeps the FIFO: a non-interactive sh gives a background job
    # /dev/null as stdin unless it is redirected explicitly. The bearer never
    # enters argv — the exit report reads the 0600 header file by path.
    script = (
        f"exec 3<&0; {shlex.join(cmd)} <&3 & CP=$!; echo $CP > {shlex.quote(str(pidf))}; "
        f"wait $CP; RC=$?; TID=$(cat {shlex.quote(str(tidf))} 2>/dev/null); "
        f"if [ -n \"$TID\" ]; then curl -sS -o /dev/null -m 20 -H @{shlex.quote(str(hdr))} "
        f"-H 'Content-Type: application/json' -X PATCH {shlex.quote(API + '/wake/threads/')}\"$TID\" "
        f"-d '{{\"state\": \"closed\"}}'; fi; exit $RC")
    subprocess.Popen(["sh", "-c", script], stdin=fd, stdout=out, stderr=err,
                     env=_holder_env(sid), start_new_session=True, cwd=HOLDER_CWD)
    os.close(fd)
    out.close()
    err.close()
    pid = None
    for _ in range(100):                     # the launcher writes the pid at once
        try:
            pid = int(pidf.read_text().strip() or 0) or None
        except (OSError, ValueError):
            pid = None
        if pid:
            break
        time.sleep(0.02)
    if not pid:
        raise RuntimeError(f"{key}: holder launcher did not report a pid")
    if not STREAM_CMD:
        deliver_stream(key, prompt)
    return pid, out_name


def deliver_stream(key: str, text: str) -> None:
    tdir = STATE_DIR / "threads" / key
    fifo = tdir / "in.fifo"
    msg = json.dumps({"type": "user",
                      "message": {"role": "user", "content": text}}) + "\n"
    data = msg.encode()
    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        # Write the WHOLE line, not one os.write: past PIPE_BUF the kernel may
        # take a partial write (short count or EAGAIN when the holder is
        # mid-turn and the pipe is full). A partial line glued to the next
        # dispatch is the corrupt mega-line SyntaxError that killed TEN
        # holders in the #87 stress test — nine on one seat alone, because
        # the failure self-amplifies: a dead holder never acks, the backlog
        # grows, the resume payload gets bigger, the write is more likely to
        # be cut mid-turn again. Not size-thresholded (9339 chars died while
        # 22999 passed) — the variable is whether the holder was draining.
        # Bounded: the holder drains its stdin continuously, so EAGAIN clears
        # as it reads; if it truly stops reading for 60s the dispatch fails
        # loudly and the rows ride the next pass unstamped.
        deadline = time.time() + 60
        while data:
            try:
                n = os.write(fd, data)
                data = data[n:]
            except BlockingIOError:
                if time.time() > deadline:
                    raise RuntimeError(
                        f"fifo write stalled ({len(data)} of {len(msg)} bytes "
                        f"unwritten) — holder not draining stdin")
                time.sleep(0.05)
    finally:
        os.close(fd)
    # Durable inbound log (task 610/1): the transcript records only the
    # outbound side, so without this the rail's own inbox has no on-disk
    # record — append-only, one JSON line per delivery, audit-grade.
    try:
        with open(tdir / "in.jsonl", "a") as f:
            f.write(json.dumps({"ts": int(time.time()), "chars": len(text),
                                "text": text}, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"{key}: in.jsonl append failed (non-fatal): {e}")


# ── prompts ──────────────────────────────────────────────────────────────────
#
# Rail 3.9 (doc 174) — texts per doc 177 (v2): a delivery says what happened
# and where to reply, nothing else. `author (HH:MM UTC):` and the body, status
# changes as sentences, a footer with the thread id and what you hold. No
# mechanism, no security lecture, no ack talk — that is the skill's job, loaded
# on demand and self-updating. The JSON-array digest of 3.8 read like a mail
# server's log, and the holders' transcripts were mostly notifications quoting
# notifications — which is what a $10 RESUME was replaying.

def _thread_title(key: str, rows: list) -> str:
    """Orient to this delivery, not a possibly ancient first object."""
    ref = thread_ref(key)
    r = max(rows, key=lambda r: r.get("id") or 0, default={})
    p = r.get("payload") or {}
    title = str(p.get("summary") or p.get("title") or "").strip()
    return f"{ref} · latest on {_row_ref(r) or ref}: {title}" if title else ref


def _root_ref(r: dict) -> str:
    """`ticket #706` — the first member of the row's thread, if the row is
    stamped; '' otherwise."""
    tid = r.get("thread_id")
    if tid is None:
        return ""
    root = str(_thread_info(f"thread-{tid}").get("root") or "")
    plane, _, oid = root.partition(":")
    return f"{plane} #{oid}" if oid else ""


def _author_label(author, fp=None, home=None) -> str:
    """Home is the writer's origin, never the receiving object's thread."""
    label = author or "?"
    if home is not None:
        label += f"@{home}"
    if fp:
        label += f" [{fp}]"
    return label


def _status_sentence(r: dict) -> str:
    """A status-only row as a sentence: `admin resolved the ticket`."""
    p = r.get("payload") or {}
    who = r.get("actor") or "someone"
    plane = _row_plane(r) or "thread"
    st = str(p.get("status") or "")
    et0 = str(r.get("event_type") or "")
    if et0 == "thread.closed":
        return f"{who} closed the thread"
    if et0 == "doc.assigned":
        return f"{who} assigned the doc to {p.get('owner')}"
    if et0 in ("task.created", "ticket.created"):
        name = p.get("title") or p.get("summary") or ""
        return f"{who} created the {plane}" + (f" · {name}" if name else "")
    changed = p.get("changed")
    if et0.endswith(".updated") and isinstance(changed, (list, dict)) and changed:
        fields = ", ".join(str(field) for field in changed)
        if "status" not in changed:
            return f"{who} updated the {plane} ({fields})"
    if st:
        verb = {"resolved": "resolved", "closed": "closed", "open": "reopened",
                "done": "marked done", "cancelled": "cancelled"}.get(st, f"set status {st}")
        return f"{who} {verb} the {plane}"
    if p.get("assignee"):
        me = _whoami()
        tgt = "you" if p["assignee"] == me else p["assignee"]
        return f"{who} assigned it to {tgt}" + (f" · priority {p['priority']}" if p.get("priority") else "")
    if p.get("priority"):
        return f"{who} set priority {p['priority']}"
    et = str(r.get("event_type") or "")
    if et.startswith("wake."):
        ident = p.get("identity") or ""
        reason = p.get("reason") or ""
        return f"the plane: {ident} {et.split('.', 1)[1].replace('_', ' ')}" + (f" ({reason})" if reason else "")
    if et == "doc.revised":
        rev = p.get("rev"); note = str(p.get("note") or "").strip()
        return f"{who} revised the doc" + (f" (rev {rev})" if rev else "") + (f"; author's note: {note}" if note else "")
    if et == "doc.linked":
        return f"{who} linked the doc" + (f" to {p.get('target')}" if p.get("target") else "")
    return f"{who}: ({et or 'event'})"


_multi: dict = {}   # set by the caller: does this batch span more than one member object?


class DigestOverflow(ValueError):
    """The first `count` rows fit; no prompt containing fewer rows may receipt all."""
    def __init__(self, count: int):
        self.count = count
        super().__init__(f"delivery digest exceeds {PAYLOAD_CAP} chars after {count} row(s); "
                         "unsent rows remain unreceipted")


def _digest(rows: list, tail: bool = True) -> tuple:
    """Returns (text, cut_count): one message per row, chronological —
    `author (HH:MM UTC):` and the body; status changes as sentences. cut_count
    says how many rows were shortened with an inline fetch link.
    Whole rows are never silently omitted: an oversized batch raises so stream
    dispatch can split it before sending or receipting anything (task 828).

    `tail`: quote the three comments the plane attached to a mention row. A
    tail rides a delivery that STARTS a process — a wake, a one-off — because
    that process has not seen the thread. A running holder has: it gets what
    was added (3.13.4). On 2026-10-08 four holders on #904 mentioned each
    other on every comment, so every continue carried the previous three
    quoted whole, and 1.32M of the 1.94M characters one holder was fed on
    that thread were re-reading (doc 355)."""
    out, size, ncut = [], 0, 0
    rows = sorted(rows, key=lambda r: r.get("id") or 0)
    _multi["multi"] = len({_row_ref(r) for r in rows if _row_ref(r)}) > 1 or any(
        r.get("thread_id") is not None and _row_ref(r) and _row_ref(r) != _root_ref(r) for r in rows)
    for r in rows:
        p = r.get("payload") or {}
        when = str(r.get("created_at") or "")[11:16]
        b = str(p.get("body") or p.get("detail") or "").strip()
        on = _row_ref(r)
        on = f" on {on}" if on and _multi.get("multi") else ""
        if b:
            mark = " mentioned you" if str(r.get("event_type") or "").endswith(".mentioned") else ""   # a tag is visible to a resident (3.14.0)
            who = _author_label(r.get("actor"), p.get("actor_session"), p.get("actor_home_thread_id"))
            head = f"{who} ({when} UTC){on}{mark}:"
        else:
            head, b = _status_sentence(r) + (on if on else ""), ""
        source = f"{API}/notifications (notification {r.get('id')})"
        for f, pl in (("ticket_id", "tickets"), ("doc_id", "docs"), ("task_id", "tasks")):
            if r.get(f) is not None:
                source = f"{API}/{pl}/{r[f]}"
                break
        src = f" — GET {source} for the rest"
        shortened = len(b) > DIGEST_BODY_CAP
        if shortened:
            b = b[:DIGEST_BODY_CAP] + f"\n[… cut at {DIGEST_BODY_CAP // 1000}k chars{src}]"
        lines = [head] + ([b] if b else [])
        if not b and r.get("read_object"):
            title = str(p.get("title") or p.get("summary") or "")[:240]
            lines.append((f"{title} — " if title else "") + r["read_object"])
        for tc in (p.get("thread_tail") or [])[:3] if tail else []:
            tb = str(tc.get("body") or "").strip()
            who = _author_label(tc.get("author"), tc.get("session_fp"), tc.get("home_thread_id"))
            lines.append(f"> {who}: " + tb.replace("\n", "\n> "))
        s = "\n".join(lines)
        # Fit a row against the whole cap, never the remaining batch space:
        # otherwise batching would needlessly shorten an intact next message.
        # Mention history is fetchable context, so discard quotes before body.
        if len(s) > PAYLOAD_CAP:
            context = (f"\n[Earlier comments omitted — GET {source}]"
                       if tail and p.get("thread_tail") else "")
            core = "\n".join([head] + ([b] if b else []))
            if len(core) + len(context) > PAYLOAD_CAP:
                suffix = f"\n[… cut to fit delivery{src}]"
                core = core[:PAYLOAD_CAP - len(context) - len(suffix)] + suffix
            s = core + context
            shortened = True
        if size + len(s) > PAYLOAD_CAP:
            raise DigestOverflow(len(out))
        ncut += int(shortened)
        out.append(s)
        size += len(s) + 2
    return "\n\n".join(out), ncut


def _digest_batches(rows: list, tail: bool) -> list:
    """Plan chronological stream deliveries using the actual renderer's limit.

    Only the first delivery can be a wake with historical tails. Validate all
    batches before sending any; the renderer shortens oversized individual rows
    with fetch pointers. The cap bounds the digest, not its lifecycle copy.
    """
    pending = sorted(rows, key=lambda r: r.get("id") or 0)
    batches = []
    while pending:
        try:
            _digest(pending, tail=tail and not batches)
        except DigestOverflow as exc:
            if not exc.count:
                raise
            batches.append(pending[:exc.count])
            pending = pending[exc.count:]
        else:
            batches.append(pending)
            break
    return batches or [[]]


_THREADS_SELF: dict = {}   # per-run cache of GET /wake/threads (all states)


def _threads_all() -> list:
    if "rows" not in _THREADS_SELF:
        try:
            _THREADS_SELF["rows"] = _api("GET", "/wake/threads").get("threads") or []
        except Exception as e:
            log(f"threads view failed (footer degrades): {e}")
            _THREADS_SELF["rows"] = None
    return _THREADS_SELF["rows"]


_SURFACES_SELF: dict = {}


def _surfaces_self() -> list:
    """My identity's registered interactive surfaces (0.9.1: each carries
    registered and not yet `ended`). Read
    once per run; an older registry has no route and the list is empty."""
    if "rows" not in _SURFACES_SELF:
        try:
            _SURFACES_SELF["rows"] = _api("GET", "/wake/surfaces").get("surfaces") or []
        except Exception as e:
            log(f"surfaces view failed (no surface can hold a thread this pass): {e}")
            _SURFACES_SELF["rows"] = []
    return _SURFACES_SELF["rows"]


def _held_by_surface(key: str):
    """The conversation id of a CONNECTED interactive surface of my identity
    that holds this thread, else None (rail 3.9.20). An interactive session
    that writes on an object is bound to it like any holder (comment-to-bind);
    while it is registered it IS the holder — its feed delivers the
    rows — so this seat must not birth a headless twin for the same identity
    (the twin: doc 221, #679 09:24 UTC). When the surface disconnects the
    thread is simply unheld and the next message births a holder as usual."""
    plane, oid = _key_parts(key)
    if plane is None:
        return None
    live = {s.get("conversation_id") for s in _surfaces_self()
            if not s.get("ended") and s.get("conversation_id")}
    if not live:
        return None
    tid = _thread_id_of(key)
    for t in (_threads_all() or []):
        same = (t.get("thread_id") == tid) if (tid and t.get("thread_id")) else \
               (t.get("kind") == plane and t.get("object_id") == oid)
        if same and t.get("state") in ("open", "closing") and t.get("conversation_id") in live:
            return t.get("conversation_id")
    return None


def _threads_self() -> list:
    """What I HOLD: live holder rows. A `conversation` row is a binding (a
    ticket this identity filed or wrote on), not a held thread — counting
    them read `holding 7` on a seat holding one thread (staging #105)."""
    rows = _threads_all()
    return None if rows is None else [t for t in rows if t.get("state") in ("open", "closing")
                                      and t.get("holder") in ("stream", "tmux", "oneshot")]


_FORMER: dict = {}   # per-run cache: key -> identities that HELD the thread (any state)


def _former_holders(key: str) -> set:
    """Identities with a holder row (stream / tmux / oneshot) on this thread —
    the plane's per-thread view, fingerprints only, visibility-gated."""
    if key not in _FORMER:
        plane, oid = _key_parts(key)
        rows = []
        if plane:
            try:
                tid = _thread_id_of(key)
                q = f"thread_id={tid}" if tid else f"thread={plane}:{oid}"
                rows = _api("GET", f"/wake/threads?{q}").get("threads") or []
            except Exception as e:
                log(f"{key}: thread view failed ({e}) — treating peers as outsiders")
        _FORMER[key] = {t.get("identity") for t in rows
                        if t.get("holder") in ("stream", "tmux", "oneshot")}
    return _FORMER[key]


def _reply_sentence() -> str:
    """A Claude holder writes its own reply with sc-say. A runtime the adapter
    runs simply answers: the adapter takes what it said and posts it on the
    thread, under this seat's identity, stamped with this conversation."""
    return ("" if RUNTIME_STREAMS else                     # a Claude holder's verb is in the footer bar (3.14.0)
            "What you say is posted on this thread for you.")


def _end_sentence() -> str:
    """The wrap-up's last clause. `kill -TERM $PPID` ends a held process; a
    runtime the rail did not keep resident has no process to end, and telling
    it to kill its parent would end the adapter mid-post (doc 265 §5: that
    instruction must never reach a runtime that is not a managed holder)."""
    if STREAM_CMD:
        return 'then end your client process: kill -TERM "$SAMCLOUD_CLIENT_PID"'
    return ("then end your process: kill -TERM $PPID" if RUNTIME_STREAMS
            else "this is the last turn on it")


def _footer(key: str) -> str:
    """`— samcloud · thread 12 · ticket #95 · sc-say ticket 95 to reply` — a
    helper bar: where you are, then the verbs that exist, named by what they
    do, nothing imperative (doc 358 §8). Read from the plane at delivery time.
    No census of what else you hold (3.9.22, #694)."""
    plane, oid = _key_parts(key)
    where = _FOOTER_OBJECT.get(key) or (f"{plane} {oid}" if plane else "")
    if RUNTIME_STREAMS:
        reply = f"sc-say {where} to reply" if where else "sc-say <ticket|doc|task> <id> to reply"
    else:
        reply = f"what you say is posted on {where}" if where else "what you say is posted on this thread"
    return f"— samcloud · {thread_ref(key)} · {reply}"


_FOOTER_OBJECT: dict = {}   # key -> "task 648": the member the latest row sat on


def _note_reply_target(key: str, rows: list) -> None:
    """The reply line names the member the conversation is on right now —
    the object of the newest row in the batch."""
    for r in sorted(rows, key=lambda r: r.get("id") or 0, reverse=True):
        ref = _row_ref(r)
        if ref:
            _FOOTER_OBJECT[key] = ref.replace(" #", " ")
            return


def _cut_note(ncut: int) -> str:
    return ""            # the cut is marked inline where it happened (v2, doc 177)


MENTION_EVENTS = {"ticket.mentioned", "doc.mentioned", "task.mentioned"}


def _birth_kind(rows: list, me: str) -> str:
    """`owner` when the batch assigns the work to me or answers on work I own;
    `mention` when the only thing that opens the thread toward me is someone
    naming me (3.11.0, doc 246 D2). One text was used for both, and it told
    a newcomer "you hold this thread" — an owner's line, with no brief."""
    for r in rows:
        et = str(r.get("event_type") or "")
        p = r.get("payload") or {}
        if et in ("task.assigned", "task.delegated") or (
                me and p.get("assignee") == me and et in ("ticket.created", "ticket.updated")):
            return "owner"
        if et in RESPONSE_EVENTS and _row_owner(r) == me:
            return "owner"
    return "mention" if any(str(r.get("event_type") or "") in MENTION_EVENTS for r in rows) else "owner"


def _root_block(rows: list) -> str:
    """Offer the origin as history, not as the recipient's current assignment."""
    for r in sorted(rows, key=lambda r: r.get("id") or 0, reverse=True):
        p = r.get("payload") or {}
        root = p.get("root")
        if root and root.get("read"):
            return (f"Conversation origin (background, not an assignment) — {root.get('kind')} #{root.get('id')} · "
                    f"{str(root.get('title') or '')[:240]}: {root['read']}")
    return ""


def _siblings_line(rows: list) -> str:
    """The plane supplies the same bounded self-context to every transport."""
    row = next((r for r in reversed(rows) if "siblings_text" in r), {})
    return "\n".join(row[k] for k in ("siblings_text", "pending_inbox_text") if row.get(k))


def wake_prompt(key: str, rows: list, resumed: bool, home_key: str = None, closing=None) -> str:
    _note_reply_target(key, rows)
    body, _ = _digest(rows)
    title = _thread_title(key, rows)
    if resumed:
        head = (f"Thread resumed — {title}\n"
                f"(your earlier conversation on this thread; re-read the "
                f"{_key_parts(key)[0] or 'thread'} before relying on memory)")
    elif _birth_kind(rows, _whoami()) == "mention":
        owners = [o for o in (_thread_info(key).get("owners") or []) if o != _whoami()]
        on_it = f"{', '.join(owners)} on it" if owners else "nobody on it yet"
        who = next((r.get("actor") for r in rows
                    if str(r.get("event_type") or "") in MENTION_EVENTS and r.get("actor")), "someone")
        head = (f"{who} mentioned you on {title} ({on_it})\n"
                f"The message, and the last few before it, follow. Replies to you arrive here."
                + (f" {_reply_sentence()}" if _reply_sentence() else ""))
        root = _root_block(rows)
        if root:
            body = f"{root}\n\n{body}"
    else:
        head = (f"New thread — {title}\n"
                f"This is shared conversation context; addressed work is in the messages below."
                + (f" {_reply_sentence()}" if _reply_sentence() else ""))
    tail = f"\n\n{_close_text(key, *closing)}" if closing else ""
    siblings = _siblings_line(rows)
    if siblings:
        head += "\n" + siblings
    return f"{_conversation_sentence(home_key or key)}\n{head}\n\n{body}{tail}\n\n{_footer(key)}"


def _conversation_sentence(key: str) -> str:
    tid = _thread_id_of(key)
    return f"You are {_whoami()} on thread {tid}." if tid is not None else f"You are {_whoami()}; home thread unknown."


def continue_prompt(key: str, rows: list, closing=None, home_key: str = None) -> str:
    """Messages, then — when the batch carries the object's close — the
    wrap-up, then ONE footer (staging #102 finding 4: appending a whole
    close_prompt rendered two footers in one delivery)."""
    _note_reply_target(key, rows)
    body, _ = _digest(rows, tail=False)      # a running holder gets what was added, never the last few again (3.13.4)
    head = f"{len(rows)} messages on {thread_ref(key)}:\n\n" if len(rows) > 1 else ""
    siblings = _siblings_line(rows)
    if siblings:
        head += siblings + "\n\n"
    tail = f"\n\n{_close_text(key, *closing)}" if closing else ""
    return f"{_conversation_sentence(home_key or key)}\n{head}{body}{tail}\n\n{_footer(key)}"


def _close_text(key: str, by: str, why: str) -> str:
    """The wrap-up (doc 177 §5). The exit command stays inline on purpose:
    the first live wrap-up posted its notes and then sat idle for want of it
    (doc 173 F10) — finishing a turn does not end a stream session."""
    if not key.startswith("thread-"):
        what = ""                                   # a pre-v19 object key: `alice resolved ticket #900`
    elif why == "closed":
        what = "the thread "                        # a person closed it: `admin closed the thread thread 331 …`
    else:
        what = "the last open work in "             # `admin marked done the last open work in thread 331 · ticket #706`
    if not by:
        # No row carried the close (3.11.0, doc 246 D3): your own identity's
        # write from another conversation, or one received elsewhere. Your
        # own writes never ring you — until now that included the one
        # message that ends you.
        return (f"{what.capitalize() or 'The work on '}{thread_ref(key)} is {why}. "
                f"This thread is done. Post closing notes if you have any, {_end_sentence()}.")
    return (f"{by or 'someone'} {why} {what}{thread_ref(key)}. This thread is done. "
            f"Post closing notes if you have any, {_end_sentence()}.")


def close_prompt(key: str, by: str, why: str) -> str:
    """The wrap-up on its own (a holder born straight into a close)."""
    return f"{_close_text(key, by, why)}\n\n{_footer(key)}"


def plane_close_prompt(key: str, why: str) -> str:
    """The wrap-up when the plane says the thread is done and no row carried
    it (3.11.0, doc 246 D3) — `_close_text` with no actor."""
    return f"{_close_text(key, '', why)}\n\n{_footer(key)}"


def oneshot_prompt(key: str, rows: list, fresh: bool = False) -> str:
    _note_reply_target(key, rows)
    body, _ = _digest(rows)
    if fresh:
        root = _root_block(rows)               # a fresh one-off knows nothing: the work rides with it (3.11.2)
        if root:
            body = f"{root}\n\n{body}"
    if _doc_rooted(key):
        head = f"Message on {thread_ref(key)}. One turn, then finish"
    else:
        head = f"Late message on resolved {thread_ref(key)}. One turn: reply if one is due, then finish"
    head = _conversation_sentence(key) + chr(10) + head
    siblings = _siblings_line(rows)
    if siblings:
        head += "\n" + siblings
    return f"{head}:\n\n{body}\n\n{_footer(key)}"


def contract_refresh(key: str) -> str:
    return ""            # dropped (doc 177 §4): the contract is the skill, and the skill self-updates


# ── dispatch ─────────────────────────────────────────────────────────────────

def fetch_rows() -> list:
    """Unread rows never delivered to anyone (doc 174: `delivered_to IS
    NULL`). The registry keeps the receipt, so a row the rail wrote into a
    process is not offered again — not to that process, not to a successor,
    not to a one-shot. The 30-minute re-offer of 3.8 is gone; the one replay
    of a closed thread's whole history into a one-shot (doc 173 F9) is why
    delivered is final."""
    if FAKE_INBOX:
        return json.loads(Path(FAKE_INBOX).read_text()).get("notifications", [])
    path = "/notifications?unread=true&undelivered=true&delivery=true"
    # #568 §3: a 502 on the poll used to be a silent exit with nothing
    # scheduled to retry — the dispatch was simply lost until an unrelated
    # later event. Bounded retry here; still a quiet no-op if the registry
    # stays down (the next event re-enters).
    for attempt in (1, 2, 3):
        try:
            return _api("GET", path).get("notifications", [])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log(f"poll HTTP {e.code} — auth failure, outcome=FAILED (tombstone)")
                sys.exit(41)               # same tombstone contract as v2 (#502)
            log(f"poll HTTP {e.code} (attempt {attempt}/3)")
        except Exception as e:             # transient registry trouble
            log(f"poll failed (attempt {attempt}/3): {e}")
        if attempt < 3:
            time.sleep(5)
    log("poll retries exhausted — one re-enter in 30s")
    time.sleep(30)
    try:
        return _api("GET", path).get("notifications", [])
    except Exception as e:
        log(f"poll re-enter failed: {e}")
    report("failed", "rail3 poll-5xx-exhausted (dispatch lost until next event)", 0)
    sys.exit(1)


def _api_quiet(method: str, path: str, body: dict = None, what: str = "") -> dict:
    """Plane writes the rail must not die on (bindings, receipts, acks):
    logged, never raised. FAKE_INBOX runs talk to the harness stub, which
    serves these routes; DRY runs make no writes at all."""
    if DRY:
        return {}
    try:
        return _api(method, path, body, timeout=10) or {}
    except Exception as e:
        log(f"{what or path} failed (non-fatal): {e}")
        return {}


def release_dead_claims(key: str) -> None:
    """Release the identity's claim on a dead or closing holder's THREAD
    (#568 §5 / #564 r2). Claims no longer route (doc 174) but interactive
    surfaces still read them; a dead holder must not look engaged."""
    plane, _, tid = key.partition("-")
    path = {"ticket": "tickets", "doc": "docs", "task": "tasks"}.get(plane)
    if not path or not tid.isdigit():
        return                               # lobby has no thread claim
    try:
        tok = _token()
        req = urllib.request.Request(
            f"{API}/{path}/{tid}/claim", method="DELETE",
            headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.load(r)
        n = resp.get("released")
        if n:
            log(f"{key}: released the holder's thread claim ({n})")
        elif resp.get("status") == "released":
            log(f"{key}: no thread claim to clear")
        else:
            log(f"{key}: claim release returned {json.dumps(resp)[:120]}")
    except Exception as e:
        log(f"{key}: stale-claim release failed (non-fatal): {e}")


_WHOAMI: dict = {}   # per-run cache


def _whoami() -> str:
    """This seat's registry username, from its own bearer. /auth/verify
    carries no username for admin-class bearers (3.8.1 finding F4: the
    assignee rule was silently off on the admin seat), so fall back to the
    briefing, which always names the identity. Empty string on failure."""
    if "name" not in _WHOAMI:
        name = ""
        try:
            name = _api("GET", "/auth/verify").get("username") or ""
        except Exception as e:
            log(f"whoami via auth/verify failed: {e}")
        if not name:
            try:
                name = _api("GET", "/users/me/briefing").get("identity") or ""
            except Exception as e:
                log(f"whoami via briefing failed (delivery rule degrades to mentions only): {e}")
        _WHOAMI["name"] = name
    return _WHOAMI["name"]


def mark_seen(ids: list) -> None:
    """Cold-rail suppressor only (v2 seats, interactive digests): a short
    lease so a row this rail is handling is not also cold-worked. Delivery
    itself is recorded by mark_delivered."""
    if not ids or FAKE_INBOX:
        return
    try:
        _api("POST", "/notifications/seen",
             {"ids": ids, "ttl_seconds": REDELIVER_MIN * 60})
    except Exception as e:
        log(f"seen-lease failed (non-fatal): {e}")


def mark_delivered(ids: list, sid: str) -> None:
    """The delivery receipt (doc 174): these rows reached conversation `sid`.
    The registry will not offer them again while that conversation is open."""
    if not ids or not sid:
        return
    _api_quiet("POST", "/notifications/delivered",
               {"ids": [int(i) for i in ids], "conversation_id": sid}, "delivered receipt")


def ack_rows(ids: list) -> None:
    if ids:
        _api_quiet("POST", "/notifications/ack",
                   {"ids": [int(i) for i in ids], "surface": "wake"}, "ack")


def report(outcome: str, note: str, unread: int) -> None:
    if DRY or FAKE_INBOX:
        return
    try:
        _api("POST", "/wake/session",
             {"outcome": outcome, "rc": 0 if outcome == "ok" else 1,
              "host": os.uname().nodename.split(".")[0],
              "unread": unread, "note": note[:200]})
    except Exception:
        pass                               # fire-and-forget, like v2


def _reap_holder(entry: dict) -> None:
    """Kill a holder the rail has declared dead-but-running (#568 r2: an
    auth-dead process idling on its FIFO) so it cannot race its successor
    for the FIFO. Called from dispatch only, never from sweep — a `closing`
    holder is never killed; it ends itself, however long its turn takes.
    tmux holders die with their window; stream holders need the signal."""
    if STREAM_CMD:
        # The client owns runtime recovery. Do not apply Claude auth-death
        # cleanup (or kill a reused pid) to a different runtime's processes.
        return
    pid = entry.get("pid")
    if not pid or entry.get("holder") == "tmux":
        if entry.get("holder") == "tmux":
            _tmux("kill-window", "-t", f"{TMUX_SESSION}:{_tmux_window(entry['key'])}")
        return
    try:
        os.kill(pid, 15)
        time.sleep(1)
        os.kill(pid, 9)
    except OSError:
        pass


# ── the thread record on the plane (doc 174) ─────────────────────────────────

def _key_parts(key: str):
    """(plane, id) of the object behind a key — for a universal thread, its
    first member (the object that names it)."""
    plane, _, tid = key.partition("-")
    if plane in ("ticket", "doc", "task") and tid.isdigit():
        return plane, int(tid)
    if plane == "thread" and tid.isdigit():
        root = str(_thread_info(key).get("root") or "")
        rp, _, rid = root.partition(":")
        if rp in ("ticket", "doc", "task") and rid.isdigit():
            return rp, int(rid)
    return None, None


def _row_plane(r: dict) -> str:
    for field, prefix in (("ticket_id", "ticket"), ("doc_id", "doc"), ("task_id", "task")):
        if r.get(field) is not None or (r.get("payload") or {}).get(field) is not None:
            return prefix
    return ""


class ThreadHeld(Exception):
    """The plane refuses this binding: a live conversation of my identity
    already holds the thread (409, plane 0.12.18). Not an error to survive —
    an answer. Do not birth."""


def post_binding(key: str, entry: dict, state: str, holder: str = None, **facts):
    """Tell the plane which conversation holds this thread and in what state.
    The lobby is seat-local (no object) and is not recorded. Returns the
    plane's thread id (or None) so a one-shot wrapper can close it. `pid=`
    overrides the entry's: a one-shot posts None, because the entry's pid is
    the dead resident's and a row must never name a process that is not the
    one reading (staging #100, finding 3)."""
    plane, oid = _key_parts(key)
    if plane is None or not entry.get("sid"):
        return None
    body = {
        "thread_id": _thread_id_of(key),
        "kind": plane, "object_id": oid, "conversation_id": entry["sid"],
        "host": os.uname().nodename.split(".")[0],
        "holder": holder or entry.get("holder") or HOLDER,
        "pid": facts["pid"] if "pid" in facts else entry.get("pid"),
        "state": state, "rail_version": RAIL_VERSION,
    }
    # A 409 here is the no-twin refusal and it is the ONE binding failure the
    # rail must not shrug off: every other one is bookkeeping the next pass
    # repairs, this one is the plane saying somebody of mine is already in
    # there. Raised, so the caller can decline to birth.
    if not DRY:
        try:
            resp = _api("POST", "/wake/threads", body, timeout=10) or {}
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise ThreadHeld(e.read().decode("utf-8", "replace")[:300])
            log(f"{key}: binding {state} failed (non-fatal): {e}")
            resp = {}
        except Exception as e:
            log(f"{key}: binding {state} failed (non-fatal): {e}")
            resp = {}
    else:
        resp = {}
    tid = (resp.get("thread") or {}).get("id")
    if tid:
        entry["thread_id"] = tid
        try:                      # the holder's launcher reads it at exit (3.9.10)
            (STATE_DIR / "threads" / key / "thread_id").write_text(str(tid))
        except OSError:
            pass
    return tid


_OBJECT_CACHE: dict = {}


def _object(key: str) -> dict:
    """The object behind a thread key, one GET per run."""
    if key not in _OBJECT_CACHE:
        plane, oid = _key_parts(key)
        path = {"ticket": "tickets", "doc": "docs", "task": "tasks"}.get(plane)
        try:
            _OBJECT_CACHE[key] = _api("GET", f"/{path}/{oid}") if path else {}
        except Exception as e:
            log(f"{key}: object read failed ({e}) — treated as not mine")
            _OBJECT_CACHE[key] = {}
    return _OBJECT_CACHE[key]


def _owner(key: str) -> str:
    """Who owns the object: ticket/task → assignee; doc → owner (legacy author)."""
    plane, _ = _key_parts(key)
    o = _object(key)
    if plane == "doc":
        return str(o.get("owner") or o.get("source") or o.get("author") or "")
    return str(o.get("assignee") or "")


CLOSE_STATES = {"ticket": ("resolved", "closed"), "task": ("done", "cancelled")}


def close_signal(key: str, rows: list):
    """(actor, status) if this batch carries the object's close AND the object
    is closed now, else None. A batch that holds a resolve and a later reopen
    (a resumed holder catching up on a thread that was parked, resolved and
    reopened while its turn 429'd — #686 ×3 on 2026-09-07, doc 221) is not a
    close: the plane's current status decides, not the oldest row (3.9.21)."""
    found = None
    for r in rows:
        et = str(r.get("event_type") or "")
        st = str((r.get("payload") or {}).get("status") or "")
        rp = _row_plane(r)
        if rp and et == f"{rp}.updated" and st in CLOSE_STATES.get(rp, ()):
            found = (r.get("actor") or "", st)
        if et == "thread.closed":
            found = (r.get("actor") or "", "closed")
    if found and _object_open(key):
        return None                           # something in the thread is still open
    return found


def _object_open(key: str) -> bool:
    """Is there still work here? For a universal thread the plane answers
    (`open_members`: any ticket or task in it still open; docs never count)
    unless a person has closed it. For a pre-v19 object key, the object's
    own status."""
    if key.startswith("thread-"):
        info = _thread_info(key)
        if not info:
            return True
        return info.get("state") != "closed" and bool(info.get("open_members"))
    plane, _ = _key_parts(key)
    st = str(_object(key).get("status") or "")
    return st not in CLOSE_STATES.get(plane, ()) if plane else True


def _resolve_entry(key: str, reg: dict):
    """The local holder for a thread key: the entry bound to it directly, or
    — doc 174, "agent 1 is across its tickets" — the entry whose conversation
    the PLANE says holds this object (a held instance that filed a ticket is
    bound to it as the filer; replies on that ticket reach the instance that
    asked, delivered under the ticket's own key). None if nobody holds it."""
    entry = reg.get(key)
    if entry:
        return entry
    plane, oid = _key_parts(key)
    if plane is None:
        return None
    tid = _thread_id_of(key)
    for t in (_threads_all() or []):
        same = (t.get("thread_id") == tid) if (tid and t.get("thread_id")) else \
               (t.get("kind") == plane and t.get("object_id") == oid)
        if same and t.get("state") in ("open", "closing"):
            conv = t.get("conversation_id")
            for e in reg.values():
                if conv and e.get("sid") == conv and e.get("state", "open") in ("open", "closing"):
                    return e
    return None


OPEN_EVENTS = {"ticket.mentioned", "doc.mentioned", "task.mentioned",
               "task.assigned", "task.delegated", "doc.assigned"}
# What counts as a RESPONSE on an object you own (the owner rule births on
# these alone): someone spoke. A record update —
# `doc.updated`, `doc.published`, `doc.linked`, a priority change — is
# information for the inbox, not a conversation to hold; the un-dry birthed
# two doc holders on the assistant's seat to hear `doc.updated` (3.9.15).
RESPONSE_EVENTS = {"ticket.commented", "doc.commented", "task.commented"}
# Parked: a `schedule.fired` row rides its TASK (the
# scheduler writes it with task_id), so under the owner rule it would birth a
# task holder for the assignee and a recurring schedule would CONTINUE into
# that holder forever — a resident that never closes. Until the schedule
# primitive has its own lifecycle (continue if held, else one fresh one-shot
# per firing — a design, not a fix), these rows are never delivered to a
# holder: they stay in the inbox and the digest, where a human sees them.
PARKED_EVENTS = {"schedule.fired", "doc.published"}


def _receiver_rows(rows: list, entry: dict, me: str) -> list:
    """Filter individual rows, including mixed batches, before rendering/receipt.

    A same-identity comment is pinned to an existing different conversation.
    No entry, no sibling delivery: it cannot trigger a cold birth.
    """
    sid = (entry or {}).get("sid")
    fp = hashlib.sha256(sid.encode()).hexdigest()[:8] if sid else None
    out = []
    for r in rows:
        if r.get("event_type") in PARKED_EVENTS:
            continue
        p = r.get("payload") or {}
        target = p.get("recipient_session")
        if target and target != fp:
            continue
        if r.get("actor") == me:
            if not (fp and target == fp and p.get("actor_session") and p["actor_session"] != fp):
                continue
        out.append(r)
    return out


def deliverable(key: str, rows: list, reg: dict, me: str) -> bool:
    """The one rule (doc 174): a batch is DELIVERED to a holder iff
      (a) it is on a thread I hold (open or closing) — or held, now closed:
          someone else spoke, and a closed thread answers as a one-shot;
      (b) it opens a thread toward me — a mention, an assignment, or a
          response on an object I own with no live instance.
    Everything else is not delivered: it stays in the inbox for whoever
    looks (briefing, digest, subscriber); no holder is woken. Unkeyed rows
    (`wake.*` notices about seats) land in the lobby key and are never
    delivered — no plane event addresses the lobby; the registry never
    emitted `agent.nudge`, and `schedule.fired` is keyed by task (parked)."""
    entry = _resolve_entry(key, reg)
    # A sibling comment counts only for its pinned existing receiver.
    # Without that binding it stays inbox-only; it cannot birth a twin.
    others = _receiver_rows(rows, entry, me)
    if key == "lobby":
        return False
    surface = _held_by_surface(key)
    if surface:
        if rows:
            log(f"{key}: held by a registered interactive surface ({surface[:8]}) — its feed delivers; no holder here")
        _SURFACE_HELD.add(key)             # the surface may go; these rows are judged again then
        return False
    # An entry in `closing` whose process is gone is a closed thread NOW
    # (3.11.0, doc 246 D4): until this a peer's closing note landing between a
    # holder's exit and the next pass was "someone spoke on a thread I hold",
    # and dispatch resumed the ended conversation as a one-shot to answer a
    # goodbye with a 2 KB correction. Below, it is a late message like any
    # other — and on a resolved object former holders' notes open nothing.
    dead_closing = bool(entry and entry.get("state") == "closing" and not holder_alive(entry))
    if entry and entry.get("state", "open") in ("open", "closing") and not dead_closing:
        return bool(others)                                            # (a)
    # On a RESOLVED object nothing a former holder writes OPENS anything, for
    # anyone — a closed entry or none. The un-dry's first pass (prod #615,
    # 3.9.14) showed the shape: each fresh one-off's reply @-mentioned peers,
    # a mention on a resolved object with no entry is a fresh one-off, and six
    # seats answered a resolved ticket in ten minutes. A human's late message
    # still reaches every former holder once and opens a one-off for a
    # mentioned newcomer; the agents' own replies reach the inbox and nobody's
    # turn. This sits BELOW the live-holder branch on purpose: 3.9.15 put it
    # above, and a resolve by an identity that also held the thread was read
    # as a former holder's row — the close signal itself was dropped for every
    # live holder, nobody wrapped up, and the dialogues on #606 ran on until
    # the fleet was dried (3.9.18).
    if _key_parts(key)[0] and not _object_open(key):
        former = _former_holders(key)
        others = [r for r in others if (r.get("actor") or "") not in former]
        # A status change, a link, a priority — a row with nothing to answer —
        # never resumes an ended conversation (3.11.0, doc 246 D5): one seat
        # spent a whole `claude -p --resume` to read "admin resolved the ticket".
        others = [r for r in others if _is_message(r)]
        # A late message that names someone is for them (3.11.1, doc 247 P9):
        # a person's "@seat, one question" on a closed thread resumed every
        # former holder once — with five seats, four idle conversations spent
        # to read a question put to the fifth. A late message that names
        # nobody still reaches every former holder once.
        others = [r for r in others if _addresses_me(r, me)]
        if not others:
            if rows:
                log(f"{key}: not delivered — former holders' replies and metadata on a resolved thread open nothing")
            return False
    if entry and (entry.get("state") == "closed" or dead_closing):
        # a late message on a thread this seat HELD: delivered, and dispatch
        # decides how — the same conversation streams again if the object is
        # open, answers once (one-shot, bounded by ONESHOT_MAX) if it is not.
        # On a RESOLVED object, former holders do not answer each other: a
        # one-shot's reply is itself a late message to every other former
        # holder, and two of them answered each other for eight comments on a
        # resolved ticket until the attempts cap stopped it (staging #108).
        # A late message from anyone who never held the thread — a human, the
        # filer's instance, a newcomer — still gets its one-shot; peers with
        # more to say reopen the object, which is the model.
        return bool(others)
    for r in others:                                                   # (b)
        et = str(r.get("event_type") or "")
        p = r.get("payload") or {}
        if et in OPEN_EVENTS:
            return True
        if me and p.get("assignee") == me and (et == "ticket.created" or (
                et == "ticket.updated" and "assignee" in (p.get("changed") or ["assignee"]))):
            # an assignment opens a thread — unless the same row closes the
            # object. `changed` (0.8.20) says whether the assignee moved; a
            # row without it is read as before.
            return close_signal(key, [r]) is None
    if me and _object_open(key) and any(
            str(r.get("event_type") or "") in RESPONSE_EVENTS and _row_owner(r) == me for r in others):
        return close_signal(key, rows) is None                          # (b) owned, no instance
    return False


def _addresses_me(r: dict, me: str) -> bool:
    """A row whose comment names people (`payload.mentions`, 0.12.2) is for
    them; one that names nobody is for everyone on the thread. A mention row
    is addressed to its reader by construction."""
    et = str(r.get("event_type") or "")
    if et in MENTION_EVENTS:
        return True
    names = (r.get("payload") or {}).get("mentions")
    if not isinstance(names, list) or not names:
        return True
    return bool(me) and me in names


def _is_message(r: dict) -> bool:
    """Does this row carry something to answer — a comment, a mention, a
    revision, an assignment — as opposed to a status change or a link?"""
    et = str(r.get("event_type") or "")
    p = r.get("payload") or {}
    return et in RESPONSE_EVENTS or et in OPEN_EVENTS or bool(str(p.get("body") or "").strip())


def _row_owner(r: dict) -> str:
    """Does a response here open toward me? Ownership is the THREAD's (doc
    239): I own a thread when I am the assignee of any ticket or task in it or
    the owner of any doc in it — a reply on a member task of my ticket is a
    reply to me. An unstamped row falls back to its own object's owner."""
    tid = r.get("thread_id")
    if tid is not None:
        owners = _thread_info(f"thread-{tid}").get("owners")
        if owners is not None:
            me = _whoami()
            return me if me in owners else ""
    plane = _row_plane(r)
    if not plane:
        return ""
    oid = r.get(f"{plane}_id") or (r.get("payload") or {}).get(f"{plane}_id")
    return _owner(f"{plane}-{oid}")


# ── one-shot resume (closed thread, late message) ────────────────────────────

def start_oneshot(key: str, entry: dict, rows: list, thread_id) -> None:
    """A late message on a CLOSED thread whose object is resolved: resume
    the old conversation once, natively (`claude -p --resume`), answer,
    exit. No resident. While it runs the binding is `open` with holder
    `oneshot` and no pid; the wrapper acks the rows on success and moves the
    binding back to `closed` when the process ends — events, no clock. The
    rows were marked delivered before the spawn (delivered is final), so a
    failed one-shot is not retried with the same rows; ONESHOT_MAX caps how
    many one-shots a closed thread may run in total."""
    if not RUNTIME_STREAMS:
        # For a runtime the adapter runs, a one-off IS the ordinary turn: run
        # the runtime on this thread's conversation, answer once, exit. The
        # difference a one-off carries is the text and the counter, both of
        # which are already decided by the caller.
        prompt = oneshot_prompt(key, rows, fresh=bool(entry.get("fresh"))
                                and not _adapter_conv(key))
        start_adapter_turn(key, key, entry, prompt,
                           [int(r["id"]) for r in rows], thread_id)
        return
    tdir = STATE_DIR / "threads" / key
    tdir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    stamp = int(time.time() * 1000)          # two one-offs in one second kept distinct files (3.11.3)
    inp = tdir / f"oneshot-{stamp}.in.txt"
    inp.write_text(oneshot_prompt(key, rows, fresh=bool(entry.get("fresh")) and not session_file_exists(entry["sid"])))
    ids = json.dumps([int(r["id"]) for r in rows])
    hdr = STATE_DIR / "auth.hdr"
    resume = session_file_exists(entry["sid"]) or not entry.get("fresh")
    cmd = _stream_command(entry["sid"], resume, oneshot=True)
    # the ack rides the 0600 header file — the bearer never enters argv (#582)
    close = ""
    if thread_id:
        close = (f"; curl -sS -o /dev/null -H @{shlex.quote(str(hdr))} -H 'Content-Type: application/json' "
                 f"-X PATCH {shlex.quote(f'{API}/wake/threads/{thread_id}')} -d '{{\"state\": \"closed\"}}'")
    script = (f"{shlex.join(cmd)} < {shlex.quote(str(inp))} > "
              f"{shlex.quote(str(tdir / f'oneshot-{stamp}.out.json'))} 2>> "
              f"{shlex.quote(str(tdir / 'err.log'))} && curl -sS -o /dev/null "
              f"-H @{shlex.quote(str(hdr))} -H 'Content-Type: application/json' "
              f"-X POST {shlex.quote(API + '/notifications/ack')} "
              f"-d {shlex.quote(json.dumps({'ids': json.loads(ids), 'surface': 'wake'}))}"
              + close)
    subprocess.Popen(["sh", "-c", script], env=_holder_env(entry["sid"]),
                     start_new_session=True, cwd=HOLDER_CWD,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    if STREAM_CMD:
        # Repeated late messages resume the same client mapping. A missing
        # mapping is an explicit client error, never a fresh conversation.
        entry["fresh"] = False
    try:
        with open(tdir / "in.jsonl", "a") as f:
            f.write(json.dumps({"ts": ts, "chars": inp.stat().st_size,
                                "text": f"[oneshot] {len(rows)} row(s)"}) + "\n")
    except OSError:
        pass


def _reply_target(key: str) -> str:
    """`ticket 748` — the member this conversation is on right now. Same
    source as the footer's reply line: whatever the newest row sat on."""
    plane, oid = _key_parts(key)
    return _FOOTER_OBJECT.get(key) or (f"{plane} {oid}" if plane else "")


def _informational_status(row: dict) -> bool:
    """Only known informational changes may bypass inference."""
    event = row.get("event_type")
    payload = row.get("payload") or {}
    if payload.get("body"):
        return False
    if event == "thread.closed":
        return True
    if event not in ("ticket.updated", "task.updated"):
        return False
    changed = payload.get("changed")
    if not isinstance(changed, list) or not changed:
        return False
    if not set(changed) <= {"status", "priority", "tags"}:
        return False
    # Reopening and cancellation may change active work; keep their control path.
    return "status" not in changed or payload.get("status") in ("done", "resolved", "closed")


def _read_status_context(key: str) -> dict:
    path = STATE_DIR / "threads" / key / "status-context.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"rows": []}


def _write_status_context(key: str, value: dict) -> None:
    """Persist before acknowledging transport; a corrupt file fails closed."""
    directory = STATE_DIR / "threads" / key
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "status-context.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _adapter_no_turn(key: str, entry: dict, rows: list, reg: dict,
                     closing=None) -> str:
    """Accept informational context without launching or replying."""
    if DRY:
        dispatch_counts[key] = 0
        return "idle"
    ckey = (entry or {}).get("key") or key
    saved = _read_status_context(ckey)
    sid = (entry or {}).get("sid") or saved.get("sid") or str(uuid.uuid4())
    by_id = {r["id"]: r for r in saved["rows"]}
    by_id.update({r["id"]: r for r in rows})
    _write_status_context(ckey, {"sid": sid, "rows": list(by_id.values())})
    record = {"ts": int(time.time()), "key": key, "sid": sid, "runtime": RUNTIME,
              "outcome": "no_turn", "no_turn": "informational_status",
              "ids": [r["id"] for r in rows], "closing": closing}
    with open(STATE_DIR / "threads" / ckey / "adapter.jsonl", "a") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    verb = "idle"
    # A member closing does not close the other thread whose holder filed it.
    if closing and entry and ckey == key:
        entry["closed_by"], entry["close_why"] = closing
        entry["state"] = "closing" if holder_alive(entry) else "closed"
        post_binding(key, entry, entry["state"], holder="adapter")
        release_dead_claims(key)
        reg[ckey] = entry
        save_registry(reg)
        verb = entry["state"]
    mark_delivered([r["id"] for r in rows], sid)
    dispatch_counts[key] = len(rows)
    log(f"{key}: retained {len(rows)} informational row(s), no runtime turn")
    return verb


def start_adapter_turn(ckey: str, key: str, entry: dict, prompt: str,
                       ids: list, tid) -> int:
    """One delivery, one turn (3.12.0). Returns the adapter's pid.

    The adapter runs the seat's runtime on this thread's conversation, posts
    what it said, acks these rows and closes the binding when it ends. It
    takes the seat's runtime lock first, so a delivery arriving mid-turn
    queues behind it rather than starting a second generation — a local model
    usually sits behind a pool that serves one request at a time, and exo has
    no cancellation path, so a second caller does not lose, it wedges."""
    tdir = STATE_DIR / "threads" / ckey
    tdir.mkdir(parents=True, exist_ok=True)
    tdir.chmod(0o700)                       # prompt + journal are same-uid only (#569)
    context = _read_status_context(ckey)
    if context["rows"]:
        facts = "\n".join(str(r.get("actor") or "someone") + ": "
                          + str(r.get("event_type") or "status") + " on " + (_row_ref(r) or key)
                          + " · " + json.dumps(r.get("payload") or {}, ensure_ascii=False)
                          for r in context["rows"])
        prompt = "Earlier status context (already received):\n" + facts + "\n\n" + prompt
    stamp = int(time.time() * 1000)
    inp = tdir / f"turn-{stamp}.in.txt"
    inp.write_text(prompt)
    cmd = [ADAPTER_BIN, "--key", ckey, "--sid", entry["sid"],
           "--prompt-file", str(inp), "--reply-to", _reply_target(key),
           "--ids", json.dumps([int(i) for i in ids])]
    if tid:
        cmd += ["--binding-thread", str(tid)]
    err = open(tdir / "err.log", "ab")
    try:
        proc = subprocess.Popen(cmd, env=_holder_env(entry["sid"]),
                                cwd=HOLDER_CWD, stdin=subprocess.DEVNULL,
                                stdout=err, stderr=err, start_new_session=True)
    finally:
        err.close()
    if context["rows"]:
        # The launched turn's prompt file now owns this context. If launch failed,
        # the file above remains pending for the next attempt.
        _write_status_context(ckey, {"sid": entry["sid"], "rows": []})
    try:
        with open(tdir / "in.jsonl", "a") as f:
            f.write(json.dumps({"ts": int(time.time()), "chars": len(prompt),
                                "text": f"[{RUNTIME} turn] {len(ids)} row(s)"}) + "\n")
    except OSError:
        pass
    return proc.pid


def dispatch_adapter(key: str, entry: dict, rows: list, reg: dict,
                     closing, now: int, busy: bool) -> str:
    """A seat whose runtime does not stream (3.12.0).

    `wake`, `continue` and `resume` collapse into one act here, because there
    is no resident to keep: run the runtime on this thread's conversation and
    post what it says. That is not a lesser lifecycle — it is the one rail v3
    already runs whenever a holder's process has ended and its object is still
    open, and continuity comes from the same place it always did, the
    runtime's own conversation log."""
    ckey = (entry or {}).get("key") or key
    conv = _adapter_conv(ckey)
    wrap = None
    if closing:
        by, st = closing
        wrap = (by, {"resolved": "resolved", "closed": "closed",
                     "done": "marked done", "cancelled": "cancelled"}.get(st, st))
    if conv:
        prompt = continue_prompt(key, rows, closing=wrap, home_key=ckey)
        verb = "continue" if busy else "resume"
    else:
        prompt = wake_prompt(key, rows, resumed=False, home_key=ckey)
        if wrap:
            prompt = f"{prompt}\n\n{_close_text(key, *wrap)}"
        verb = "wake"
    if DRY:
        log(f"{key}: DRY — would run a {RUNTIME} turn ({verb}, "
            f"{len(rows)} row(s), {len(prompt)} chars)")
        dispatch_counts[key] = len(rows)
        return verb
    sid = (entry or {}).get("sid") or str(uuid.uuid4())
    # Turns already running on this thread stay recorded: the new one will wait
    # on the seat's turn lock behind them, and the thread is held until the last
    # of them ends (#832).
    running = [p for p in _adapter_pids(entry or {}) if _pid_alive(p)] \
        if (entry or {}).get("holder") == "adapter" else []
    entry = {"key": ckey, "sid": sid, "holder": "adapter",
             "born": (entry or {}).get("born", now),
             "oneshots": (entry or {}).get("oneshots", 0),
             "thread_id": (entry or {}).get("thread_id"),
             "contract_v": RAIL_VERSION, "state": "open", "runtime": RUNTIME}
    try:
        tid = post_binding(key, entry, "open", holder="adapter", pid=None)
    except ThreadHeld as e:
        log(f"{key}: no turn — {e}")
        dispatch_counts[key] = 0
        return "idle"
    ids = [r["id"] for r in rows]
    mark_delivered(ids, sid)
    mark_seen(ids)
    entry["pid"] = start_adapter_turn(ckey, key, entry, prompt, ids, tid)
    entry["pids"] = running + [entry["pid"]]
    entry["last_delivery"] = now
    reg[ckey] = entry
    post_binding(key, entry, "open", holder="adapter")
    if wrap:
        entry["state"] = "closing"
        entry["closed_by"], entry["close_why"] = wrap
        post_binding(key, entry, "closing", holder="adapter")
        release_dead_claims(key)
        verb = "closing"
    log(f"{key}: {RUNTIME} turn started (pid {entry['pid']}, {verb}, "
        f"{len(rows)} row(s)" + (f", resuming {conv}" if conv else "") + ")")
    dispatch_counts[key] = len(rows)
    return verb


# The verbs `dispatch()` and `sweep()` can answer with. Stated rather than
# scattered so the lifecycle doc can be checked against the code instead of
# against someone's memory of it (0.12.22) — the same move `_RAIL_HOLDERS`
# made for the holder kinds. `closed` is the sweep's, never dispatch's.
DISPATCH_VERBS = ("wake", "continue", "closing", "resume", "oneshot", "idle", "closed")

dispatch_counts: dict = {}   # rows actually delivered per thread this pass (#570)


def dispatch(key: str, rows: list, reg: dict) -> str:
    """Drain bounded stream batches in this pass; no extra event or timer needed.

    Each batch has the existing send-then-receipt boundary. A later failed send
    leaves only its batch and the remainder pending. Lifecycle close copy belongs
    to the last batch, never the first part of an unfinished delivery.
    """
    entry = _resolve_entry(key, reg)
    alive = bool(entry and holder_alive(entry))
    # Batch runtimes and late one-shots retain one turn/launch per dispatch.
    # Their formatter refuses overflow before receipt rather than dropping rows.
    closed = not alive or (entry or {}).get("state") == "closed"
    if not RUNTIME_STREAMS or (closed and _key_parts(key)[0] and not _object_open(key)):
        return _dispatch_batch(key, rows, reg)
    batches = _digest_batches(rows, tail=not alive or bool(
        STREAM_CMD and entry and entry.get("contract_v") == ""))
    closing = close_signal(key, rows) if _key_parts(key)[0] else None
    total = 0
    first_verb = None
    try:
        for index, batch in enumerate(batches):
            dispatch_counts[key] = 0
            verb = _dispatch_batch(key, batch, reg, defer_close=index < len(batches) - 1,
                                   batch_closing=closing if index == len(batches) - 1 else None)
            total += dispatch_counts.get(key, 0)
            if first_verb is None:
                first_verb = verb
            if verb == "idle":
                break
        return "closing" if verb == "closing" else first_verb
    finally:
        dispatch_counts[key] = total


def _dispatch_batch(key: str, rows: list, reg: dict, defer_close: bool = False,
                    batch_closing=None) -> str:
    """Returns the verb used (for the log/report)."""
    entry = _resolve_entry(key, reg)
    if STREAM_CMD and entry and entry.get("holder") not in ("stream", "oneshot"):
        raise RuntimeError("native stream requires explicit migration of this existing holder; no input sent")
    if not RUNTIME_STREAMS and rows and all(_informational_status(r) for r in rows):
        closing = close_signal(key, rows)
        if closing is None and entry and _thread_done(key):
            closing = ("", _thread_done(key))
        return _adapter_no_turn(key, entry, rows, reg, closing)
    via = entry["key"] if entry and entry.get("key") != key else None   # held via a binding
    now = int(time.time())
    state = (entry or {}).get("state", "open")
    fresh = rows                     # the registry already withheld delivered rows

    # The sweep's rule, applied here too: a process that ended between the
    # sweep and this dispatch (the settle window is exactly that long) is a
    # closed thread NOW, not a dead holder to resume. Without this a peer's
    # reply landing 47 s after the wrap-up found the holder gone on a
    # `closing` entry and RESUMED it as a stream on a resolved ticket, where
    # it then sat open with nothing left to close it (staging #110).
    if entry and not via and state != "closed" and not holder_alive(entry):
        entry["state"], entry["closed_at"] = "closed", now
        state = "closed"
        log(f"{key}: closed ({'wrapped up' if entry.get('closed_by') else 'process ended'} during this pass)")
        if not DRY:
            plane = _plane_binding(entry.get("key") or key)
            if not plane or plane.get("state") != "closed":
                post_binding(entry["key"], entry, "closed")

    # A mention (or assignment) on a RESOLVED object with no instance: the
    # rule delivers it, but a resident born now would hold a dead thread with
    # nothing left to close it — the un-dry backlog held five such mentions on
    # since-resolved tickets (doc 179 §4). After close a late message is a
    # one-off: a fresh conversation answers once and exits (3.9.14).
    if entry is None and _key_parts(key)[0] and not _object_open(key):
        entry = {"key": key, "sid": str(uuid.uuid4()), "holder": HOLDER, "born": now,
                 "contract_v": RAIL_VERSION, "state": "closed", "oneshots": 0, "fresh": True}
        reg[key] = entry
        state = "closed"
        log(f"{key}: a late message on a resolved object this seat never held — one-off, fresh session")

    # A closed thread's late messages → one-shot, bounded by a counter.
    if entry and state == "closed":
        if _key_parts(key)[0] and _object_open(key):
            # the object is open (reopened, or the process simply ended): the
            # thread streams again, on the SAME conversation — a native resume
            # reconnects the client to its own log (resume whether closed
            # or not; the rail never re-sends rows).
            entry["state"] = "open"
            state = "open"
        else:
            if entry.get("oneshots", 0) >= ONESHOT_MAX and not _doc_rooted(key):
                # A doc answers every question once (3.11.2, doc 250 D10): a
                # doc-rooted thread has no open work, so every message on it
                # is a one-off by construction — the cap that bounds a resolved
                # ticket's afterlife would leave a doc's fourth question
                # silently unanswered. Ping-pong is stopped by the former-
                # holder rule, not by the cap.
                log(f"{key}: one-shot attempts exhausted ({ONESHOT_MAX}) — "
                    f"{len(fresh)} row(s) left unread for a human")
                dispatch_counts[key] = 0
                return "idle"
            if not DRY:
                # One-shot launch builds its prompt later. Validate before the
                # existing receipt point; overflow must not consume its rows.
                _digest(fresh)
                # open (oneshot) while it runs; pid None — the entry's pid is
                # the dead resident's. The wrapper closes the binding at exit.
                try:
                    tid = post_binding(key, entry, "open", holder="oneshot", pid=None)
                except ThreadHeld as e:
                    log(f"{key}: no one-off — {e}")
                    dispatch_counts[key] = 0
                    return "idle"
                mark_delivered([r["id"] for r in fresh], entry.get("sid", ""))
                start_oneshot(key, entry, fresh, tid)
                entry["oneshots"] = entry.get("oneshots", 0) + 1
            dispatch_counts[key] = len(fresh)
            return "oneshot"

    alive = bool(entry and holder_alive(entry))
    closing = batch_closing or (close_signal(key, rows) if _key_parts(key)[0] else None)
    if closing is None and alive and not via and state == "open" and _thread_done(key):
        closing = ("", _thread_done(key))          # no row carried the close (3.11.0, doc 246 D3)
    if defer_close:
        closing = None
    if via and not alive:
        # the instance that FILED this object has ended: its own thread closes
        # on the next pass (or already has); resuming it under the child's
        # key would give one conversation two entries. The rows stay in the
        # inbox for whoever looks.
        log(f"{key}: not delivered — the filer's instance ({via}) has ended")
        dispatch_counts[key] = 0
        return "idle"

    if not RUNTIME_STREAMS:
        # Nothing below applies: the FIFO, the resident, the reap-then-respawn
        # are all the streaming holder's machinery. A turn already running is
        # not interrupted — the new turn queues on the seat's runtime lock.
        return dispatch_adapter(key, entry, fresh, reg, closing, now, alive)

    if alive:
        verb = "continue"
        if not DRY:
            if STREAM_CMD:
                _wait_stream_ready(STATE_DIR / "threads" / (via or key) / entry["out"],
                                   entry["pid"], entry["sid"])
            wrap = None
            if closing and not via:
                by, st = closing
                wrap = (by, {"resolved": "resolved", "closed": "closed", "done": "marked done", "cancelled": "cancelled"}.get(st, st))
                entry["closed_by"], entry["close_why"] = wrap
                entry["close_plane"] = not by          # the plane's word, not a row's
            elif not defer_close and not via and entry.get("state") == "closing" and (entry.get("closed_by") or entry.get("close_plane")):
                # Still closing: EVERY delivery carries the wrap-up again. On a
                # multi-party ticket each peer's closing notes reach the others
                # as a plain message, and a holder told once to end takes the
                # new message as a normal turn and forgets the exit — two
                # holders sat in `closing` with live pids for 18 min until a
                # human nudged them (staging #108). No timer: the reminder
                # rides the traffic that caused the forgetting.
                wrap = (entry.get("closed_by") or "", entry.get("close_why") or "closed")
            pending_wake = STREAM_CMD and entry.get("contract_v") == ""
            if pending_wake:
                text = wake_prompt(key, fresh, resumed=entry.get("wake_resumed", False),
                                   home_key=via or key, closing=wrap)
            else:
                text = continue_prompt(key, fresh, closing=wrap, home_key=via or key)
            if not pending_wake and entry.get("contract_v") != RAIL_VERSION:
                text = contract_refresh(key) + text
                entry["contract_v"] = RAIL_VERSION
            # a thread held via a binding is delivered into the holder's own
            # FIFO, under the object's key (the message names the object)
            (deliver_tmux if entry["holder"] == "tmux" else deliver_stream)(via or key, text)
            if pending_wake:
                entry["contract_v"] = RAIL_VERSION
                entry.pop("wake_resumed", None)
            if closing and via:
                closing = None                 # the filer's instance is not closed by the ticket it filed
            if closing:
                entry["state"] = "closing"
                post_binding(key, entry, "closing")
                release_dead_claims(key)
                verb = "closing"
    else:
        if closing and not entry:
            # a close for a thread with no instance opens nothing
            dispatch_counts[key] = 0
            return "idle"
        if entry and not DRY:
            _reap_holder(entry)            # zombie must not race its successor on the FIFO
            if entry.get("sid"):
                release_dead_claims(key)
        resumed = bool(entry and entry.get("sid")
                       and (STREAM_CMD or session_file_exists(entry["sid"])))
        sid = entry["sid"] if resumed else str(uuid.uuid4())
        verb = "resume" if resumed else "wake"
        if not DRY:
            prompt = wake_prompt(key, fresh, resumed)
            hclass = (entry or {}).get("holder") or HOLDER
            entry = {"key": key, "sid": sid, "holder": hclass,
                     "born": (entry or {}).get("born", now),
                     "oneshots": (entry or {}).get("oneshots", 0),   # the cap survives a reopen (#103 f4)
                     "thread_id": (entry or {}).get("thread_id"),
                     "contract_v": "" if STREAM_CMD else RAIL_VERSION, "state": "open"}
            if STREAM_CMD:
                entry["wake_resumed"] = resumed
            if entry["born"] != now:
                entry["respawned_at"] = now
            # Ask before birthing. The old order was birth-then-record, so a
            # thread already held by a live conversation of mine got a second
            # process FIRST and the plane heard about it after the fact — by
            # which time the person's binding had been overwritten and there
            # was nothing left to refuse. `dispatch_adapter` already posted
            # first; this is the stream path catching up.
            try:
                post_binding(key, entry, "open", pid=None)
            except ThreadHeld as e:
                log(f"{key}: not born — {e}")
                dispatch_counts[key] = 0
                return "idle"
            if hclass == "tmux":
                start_tmux_holder(key, sid, prompt, resumed)
            else:
                entry["pid"], entry["out"] = start_stream_holder(
                    key, sid, prompt, resumed)
            reg[key] = entry
            post_binding(key, entry, "open")
            if STREAM_CMD:
                # Preserve this process/id even if readiness fails; the next
                # dispatch must not invent another conversation or birth a twin.
                _wait_stream_ready(STATE_DIR / "threads" / key / entry["out"],
                                   entry["pid"], sid)
                deliver_stream(key, prompt)
                entry["contract_v"] = RAIL_VERSION
                entry.pop("wake_resumed", None)
            if closing:
                # born straight into a close (rare): wrap up immediately
                by, st = closing
                why = {"resolved": "resolved", "closed": "closed", "done": "marked done", "cancelled": "cancelled"}.get(st, st)
                (deliver_tmux if hclass == "tmux" else deliver_stream)(key, close_prompt(key, by, why))
                entry["state"] = "closing"
                entry["closed_by"], entry["close_why"] = by, why
                post_binding(key, entry, "closing")

    dispatch_counts[key] = len(fresh)
    if not DRY and entry is not None:
        entry["last_delivery"] = now
        ids = [r["id"] for r in fresh]
        mark_delivered(ids, entry.get("sid", ""))
        mark_seen(ids)
    return verb


def sweep(reg: dict) -> list:
    """Observe endings (doc 174). Runs every pass, no clocks: a thread is
    held while its process lives; when the process is gone the thread is
    closed — wrap-up, rage-quit, crash or reboot, the rail does not ask why.
    Nothing here kills anything. What happens next is decided by the OBJECT,
    and either way the client reconnects to its own log (native resume, same
    conversation id): a message on a still-open ticket streams again; a
    message on a resolved one is a one-shot."""
    now = int(time.time())
    notes = []
    for key, entry in list(reg.items()):
        st = entry.get("state", "open")
        if key == "lobby" or st == "closed":
            continue
        # Liveness by holder class: a tmux holder has no pid (its window is
        # the process — launch.sh execs claude), so a pid-only check skipped
        # it forever and a tmux thread never reached `closed` (#633 item 17:
        # its late messages resumed as a stream on a resolved object).
        if entry.get("holder") == "tmux":
            if tmux_alive(key):
                continue
        elif entry.get("holder") == "adapter":
            if not _adapter_pids(entry) or holder_alive(entry):
                continue
        elif not entry.get("pid") or _pid_alive(entry.get("pid")):
            continue
        entry["state"], entry["closed_at"] = "closed", now
        if not DRY:
            # the holder's launcher usually reported the exit already (3.9.10);
            # a second `closed` write only moved updated_at (staging #106)
            plane = _plane_binding(key)
            if not plane or plane.get("state") != "closed":
                post_binding(key, entry, "closed")
            if st == "open":
                release_dead_claims(key)
        notes.append(f"{key}: closed ({'wrapped up' if st == 'closing' else 'process ended'})")
    return notes


def _doc_rooted(key: str) -> bool:
    """A universal thread whose first member is a doc: never 'done' for want
    of open work, never held — each message answers once."""
    return key.startswith("thread-") and str(_thread_info(key).get("root") or "").startswith("doc:")


def _thread_done(key: str):
    """`closed` when a person closed the thread, `resolved`/`done` when its
    last open work ended, None while it is live. Only for thread keys whose
    root is work (a doc-rooted thread has no open members by definition and
    is not "done" for want of them)."""
    if not key.startswith("thread-"):
        return None
    info = _thread_info(key)
    if not info:
        return None
    if info.get("state") == "closed":
        return "closed"
    root = str(info.get("root") or "")
    if root.startswith("doc:") or info.get("open_members"):
        return None
    return "done" if root.startswith("task:") else "resolved"


def sweep_done_threads(reg: dict, skip: set = frozenset()) -> list:
    """A live holder whose thread the plane says is done, with no row to say
    so (3.11.0, doc 246 D3): the close was its own identity's write from
    another conversation, or was received by another surface of the
    identity. Deliver the wrap-up now, from the plane's word. Runs every
    pass; one cached thread read per open entry; nothing here kills anything."""
    notes = []
    if DRY:
        return notes
    for key, entry in list(reg.items()):
        if key in skip or entry.get("state", "open") != "open" or not holder_alive(entry):
            continue
        why = _thread_done(key)
        if not why:
            continue
        if entry.get("holder") == "adapter":
            _adapter_no_turn(key, entry, [], reg, ("", why))
            notes.append(f"{key}: closing (adapter controller recorded {why}, no turn)")
            continue
        try:
            (deliver_tmux if entry.get("holder") == "tmux" else deliver_stream)(key, plane_close_prompt(key, why))
        except Exception as e:
            log(f"{key}: wrap-up from the plane's word failed ({e})")
            continue
        entry["state"], entry["closed_by"], entry["close_why"], entry["close_plane"] = "closing", "", why, True
        post_binding(key, entry, "closing")
        release_dead_claims(key)
        notes.append(f"{key}: closing (the plane says {why}; no row carried it — wrap-up delivered)")
    return notes


def _plane_binding(key: str):
    """My identity's binding row on the plane for this key (by thread id when
    the plane's rows carry one, else by object)."""
    tid = _thread_id_of(key)
    plane, oid = _key_parts(key)
    for t in (_threads_all() or []):
        if tid and t.get("thread_id"):
            if t.get("thread_id") == tid:
                return t
        elif t.get("kind") == plane and t.get("object_id") == oid:
            return t
    return None


def sweep_bindings(reg: dict) -> list:
    """Close the plane's `conversation` bindings of my identity that nothing
    can deliver into (3.9.20): a binding whose conversation is neither one of
    this seat's live entries nor a CONNECTED surface is a dead nonce — an
    interactive session that ended, or a holder long gone. Until now these
    rows lived forever (pid-less, so the process sweep never saw them; the
    gap 3.9.7 closed for tmux, closed here for bindings): the admin's old
    session nonce held 46 objects on 2026-09-07 (doc 221). Runs every pass
    after `sweep()`; nothing here kills anything."""
    notes = []
    if DRY:
        return notes
    live_sids = {e.get("sid") for e in reg.values()
                 if e.get("sid") and e.get("state", "open") in ("open", "closing")}
    # A binding is an ADDRESS, not a claim that someone is at home (3.11.5).
    # Mail goes to an address; nobody delivers a second copy to a stranger
    # because you were slow to reply, and nobody posts you a replacement
    # correspondent. An address stops existing when its owner gives it up —
    # `ended: true` — and not before.
    #
    # This used to be decided by `connected`: true only while a
    # `?for_conversation` long-poll was open. That is a proof-of-life test, and
    # it failed the two ways such tests always fail. It lives in the registry's
    # process, so ANY deploy wiped it; and a subscriber busy printing a backlog
    # is not polling, so it read as gone while working. Both happened on
    # 2026-09-20 — 09:23:03 and 20:16:49 — and each time the sweep closed the
    # person's bindings and a headless twin was born on a thread they held.
    live_sids |= {s.get("conversation_id") for s in _surfaces_self()
                  if not s.get("ended") and s.get("conversation_id")}
    host = os.uname().nodename.split(".")[0]
    for t in (_threads_all() or []):
        if t.get("state") not in ("open", "closing") or not t.get("id"):
            continue
        conv = t.get("conversation_id")
        # A `conversation` binding is NOT this seat's to close (3.13.0). The
        # rule here was "close it unless I can see a live surface for it" —
        # absence of evidence read as evidence of absence, by a client, off a
        # list it may have mis-read. It closed a live person's bindings on
        # three tickets at 2026-09-20 22:37:35 and a headless twin was born on
        # one of them four minutes later; the harness reproduces it. The plane
        # closes these itself now, on `{"ended": true}` — the one event that
        # means the conversation is over.
        if t.get("holder") == "conversation":
            continue
        if t.get("holder") in ("stream", "tmux") and t.get("host") == host:
            # A holder row of mine on this host whose process is gone and which
            # no live entry here holds (3.11.2, doc 250): the seat's cache lost
            # the entry (a park, a re-key, a wiped state dir) and nothing else
            # ever closed the row — sixty such rows sat open for the dry seats.
            if conv and conv in live_sids:
                continue
            if t.get("pid") and _pid_alive(t.get("pid")):
                continue
            why = f"holder pid {t.get('pid')} is gone and nothing here holds it"
        else:
            continue
        try:
            _api("PATCH", f"/wake/threads/{t['id']}", {"state": "closed"})
            notes.append(f"{t.get('kind')}-{t.get('object_id')}: binding closed ({why})")
        except Exception as e:
            log(f"{t.get('kind')}-{t.get('object_id')}: could not close dead binding ({e})")
    return notes


_LIMIT_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.I)


def _last_result(entry: dict):
    """The holder's most recent `result` line (stream-json), from its out file."""
    out = entry.get("out")
    if not out or not entry.get("key"):
        return None, None
    path = STATE_DIR / "threads" / entry["key"] / out
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "replace")
        mtime = path.stat().st_mtime
    except OSError:
        return None, None
    for line in reversed(tail.splitlines()):
        if '"type": "result"' in line or '"type":"result"' in line:
            try:
                return json.loads(line), mtime
            except ValueError:
                continue
    return None, None


def _limit_reset_at(res: dict, when: float):
    """If `res` is a usage-limit 429, the instant it says the limit resets, as an
    aware datetime — else None. `when` (the out file's mtime) fixes the day:
    'resets 12am' written at 23:10 means the coming midnight, not the one that
    passed."""
    if not res or not res.get("is_error"):
        return None
    text = str(res.get("result") or "")
    if res.get("api_error_status") != 429 and "session limit" not in text and "usage limit" not in text.lower():
        return None
    m = _LIMIT_RESET_RE.search(text)
    if not m:
        return None
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(m.group(4).strip())
    except Exception:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    minute = int(m.group(2) or 0)
    at = datetime.fromtimestamp(when, tz)
    reset = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= at:
        reset += timedelta(days=1)
    return reset


def rekick_after_limit(reg: dict) -> list:
    """One CONTINUE for a live holder whose last turn ended in a usage-limit 429
    once the reset time that 429 named has passed (3.9.21). #623 made the 429
    "not death" — right — but nothing then re-woke the holder: on 2026-09-07
    every resumed holder 429'd a minute before the reset and the whole fleet
    sat parked, alive, for an hour (doc 221 M5). This runs on the identity's
    next event like everything else here — no clock; the reset time is read
    from the result text the CLI wrote. One kick per reset: the entry records
    which reset it was kicked for."""
    if STREAM_CMD:
        return []                         # runtime recovery belongs to its client
    notes = []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for key, entry in list(reg.items()):
        if key == "lobby" or entry.get("state", "open") not in ("open", "closing"):
            continue
        if entry.get("holder") != "stream" or not holder_alive(entry):
            continue
        res, when = _last_result(entry)
        reset = _limit_reset_at(res, when) if res else None
        if not reset:
            continue
        stamp = reset.isoformat()
        if entry.get("rekicked_for") == stamp or now < reset:
            continue
        if not DRY:
            try:
                deliver_stream(key, f"The usage limit your last turn hit has reset ({reset.strftime('%H:%M %Z')}). "
                                    f"The messages above are still yours — carry on.\n\n{_footer(key)}")
            except Exception as e:
                log(f"{key}: re-kick after the limit failed ({e})")
                continue
        entry["rekicked_for"] = stamp
        notes.append(f"{key}: re-kicked after the usage limit (reset {reset.strftime('%H:%M %Z')})")
    return notes


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def main() -> None:
    trigger = "manual"
    if not sys.stdin.isatty():
        try:
            payload = json.load(sys.stdin)
            r = payload.get("reason", "manual")
            e = (payload.get("event") or {}).get("type", "")
            trigger = f"listener event {e}" if r == "event" else f"listener {r}"
        except Exception:
            pass

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 0700 on the ROOT every dispatch (#569 follow-up): per-file chmods only
    # fire at holder birth, so pre-fix thread dirs kept 0755 — clamping the
    # state root makes everything under it same-uid-only regardless.
    STATE_DIR.chmod(0o700)
    # Standing auth header file (#582): holders authenticate with
    # `curl -H @auth.hdr` so the expanded bearer never enters argv.
    try:
        _write_0600(STATE_DIR / "auth.hdr", f"Authorization: Bearer {_token()}\n")
    except Exception as e:
        log(f"auth.hdr refresh failed (non-fatal): {e}")
    lockf = open(STATE_DIR / "dispatch.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX)
    except OSError as e:
        log(f"dispatch lock unavailable: {e}")
        report("failed", "rail3 dispatch-lock-unavailable", 0)
        sys.exit(1)

    reg = load_registry()
    for note in migrate_keys(reg):
        log(f"re-keyed {note} (doc 239)")

    # Lifecycle sweep first (doc 174): a holder that has ended becomes
    # `closed` — locally, so this pass routes on the truth, and on the plane.
    # The handler runs on events for this identity (any thread), so the
    # plane learns of an ending on the identity's next event, not at the
    # instant the process exits: an observation, not a deadline (staging
    # #100 measured 2m21s on a busy identity). No timer fills that gap.
    for note in sweep(reg):
        log(note)
    for note in sweep_bindings(reg):
        log(note)
    for note in rekick_after_limit(reg):
        log(note)

    quiet_marks = load_quiet()
    rows = [r for r in fetch_rows() if (r.get("id") or 0) > quiet_marks.get(thread_key(r), 0)]

    if not rows:
        # A close the plane knows and no row carried (3.11.0, doc 246 D3):
        # with nothing to dispatch, the sweep delivers the wrap-up itself.
        for note in sweep_done_threads(reg):
            log(note)
        if not DRY:
            save_registry(reg)
        sys.exit(0)

    # Settle window (#87 c585): traffic exists, so wait a beat and
    # re-poll — messages posted within a few seconds of each other land in
    # ONE batch per thread instead of racing in as parallel turns.
    if SETTLE_SEC > 0 and not FAKE_INBOX and not DRY:
        time.sleep(SETTLE_SEC)
        rows = [r for r in fetch_rows() if (r.get("id") or 0) > quiet_marks.get(thread_key(r), 0)] or rows

    me = _whoami()
    parked = [r for r in rows if str(r.get("event_type")) in PARKED_EVENTS]
    if parked:
        log(f"parked (inbox only): {' '.join(str(r.get('event_type')) + '@' + thread_key(r) for r in parked)}")
        for r in parked:
            if r.get("event_type") != "doc.published":
                quiet_marks[thread_key(r)] = max(quiet_marks.get(thread_key(r), 0), int(r.get("id") or 0))
        rows = [r for r in rows if r not in parked]
    groups: dict = {}
    for r in rows:
        groups.setdefault(thread_key(r), []).append(r)

    # The delivery rule (doc 174) replaces birth-events + lobby routing + the
    # claims interlock: a batch is delivered to a holder, or it is not —
    # undelivered rows stay in the inbox for whoever looks; no holder wakes.
    quiet = []
    for k in sorted(groups):
        if not deliverable(k, groups[k], reg, me):
            quiet.append(f"{k}({len(groups[k])})")
            if k not in _SURFACE_HELD:         # judged once (3.11.0); a surface's rows wait for the surface
                quiet_marks[k] = max(quiet_marks.get(k, 0), max(int(r.get("id") or 0) for r in groups[k]))
            groups.pop(k)
        else:
            groups[k] = _receiver_rows(groups[k], _resolve_entry(k, reg), me)
    if quiet:
        log(f"not delivered (inbox only): {' '.join(quiet)}")
    if not DRY:
        save_quiet(quiet_marks)
    # keys with rows this pass wrap up inside dispatch (the row, or the plane's word)
    for note in sweep_done_threads(reg, skip=set(groups)):
        log(note)
    # A capability probe (#521) asks only to be acked and answered with a
    # session report; this pass IS the session attempt, so the rail acks it
    # itself instead of spending a turn to have a holder say "noted".
    probes = [r["id"] for r in rows if r.get("event_type") == "wake.probe"]
    if probes and not DRY:
        ack_rows(probes)
        log(f"probe acked without a turn: {probes}")
    if not groups:
        if not DRY:
            save_registry(reg)
        report("ok", f"rail3 nothing to deliver: {' '.join(quiet)[:120]}", len(rows))
        sys.exit(0)

    log(f"dispatching {sum(len(g) for g in groups.values())} row(s) across "
        f"{len(groups)} thread(s) (trigger: {trigger}, holder={HOLDER}"
        f"{', DRY' if DRY else ''})")
    outcomes = []
    for key in sorted(groups):
        try:
            verb = dispatch(key, groups[key], reg)
            outcomes.append(f"{key}:{verb}")
            detail = {
                "idle": "nothing new — 0 delivered",
                "oneshot": f"{dispatch_counts.get(key, 0)} late message(s) → one-shot resume",
                "closing": f"{dispatch_counts.get(key, 0)} message(s) + wrap-up delivered",
            }.get(verb, f"{dispatch_counts.get(key, 0)} of "
                        f"{len(groups[key])} message(s) delivered")
            if DRY:     # parked: nothing was delivered, whatever the verb would have done
                detail = detail.replace("delivered", "would be delivered (DRY: delivered nothing)")
                verb = f"would {verb}"
            log(f"{key}: {verb} ({detail})")
        except Exception as e:
            outcomes.append(f"{key}:ERROR")
            log(f"{key}: dispatch failed: {e}")
    if not DRY:
        save_registry(reg)   # under FAKE_INBOX the state dir is test-scoped
    failed = any(o.endswith(":ERROR") for o in outcomes)
    report("failed" if failed else "ok",
           f"rail3 {' '.join(outcomes)}", len(rows))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
