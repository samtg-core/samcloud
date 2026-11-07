#!/usr/bin/env python3
# CHANNEL_VERSION: 1.1.0
# samcloud-channel — the plane, pushed into an interactive Claude Code session.
#
# A Claude Code *channel* is an MCP server that pushes events into a running
# session; an idle session wakes on one. This is the interactive session's
# inbound edge without a watch to re-arm: the Monitor tool ends every watch at
# a deadline (30 minutes at most), so a session hearing the plane through it
# spent its idle hours re-subscribing (#886). A channel has no deadline.
#
# It runs `samcloud-subscribe` (fetched from the plane, so it is always the
# served build) in its JSON-lines mode and forwards each message as one
# `notifications/claude/channel` event. Registration, scoping to this
# conversation, and receipts are the subscriber's, unchanged: delivery is
# receipt.
#
# Install (once per seat), then launch interactive sessions through it:
#   curl -s -H @"$HOME/.samcloud/token.hdr" __API__/helpers/channel > ~/.local/bin/samcloud-channel && chmod +x ~/.local/bin/samcloud-channel
#   claude mcp add --scope user samcloud -- ~/.local/bin/samcloud-channel
#   sid=$(uuidgen | tr A-Z a-z)
#   claude --session-id $sid \
#       --dangerously-load-development-channels server:samcloud          (research preview)
# Resume the same way with `--resume <id>`.
#
# The server does nothing unless the session that started it loaded it as a
# channel (its command line names server:samcloud; or SAMCLOUD_CHANNEL=1): it
# is registered for every session on the account, and only one launched this
# way is listening. The conversation is SAMCLOUD_SESSION_ID when the seat sets one
# for all its writes (claude-run does), else CLAUDE_CODE_SESSION_ID, which Claude
# Code passes to the server. Don't export SAMCLOUD_SESSION_ID by hand to launch:
# the repo's pre-commit hook reads it as "this is a rail holder".
#
# Env: SAMCLOUD_API, SAMCLOUD_TOKEN_FILE (as the subscriber), SAMCLOUD_SUBSCRIBE_PATH (run a local subscriber instead of
# the served one — for testing a change before it ships).
#
# No dependencies: the MCP stdio transport is newline-delimited JSON-RPC.
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

CHANNEL_VERSION = "1.1.0"
API = (os.environ.get("SAMCLOUD_API") or "__API__").rstrip("/")
if not API.endswith("/api/v1"):
    API += "/api/v1"
HDR = os.environ.get("SAMCLOUD_AUTH_HDR") or os.path.expanduser(
    (os.environ.get("SAMCLOUD_TOKEN_FILE") or "~/.samcloud/token") + ".hdr")

INSTRUCTIONS = (
    'Messages from the samcloud plane arrive as <channel source="samcloud" thread="...">, for the '
    "threads this conversation is on. The footer names the verbs; this channel has no reply tool. "
    "kind=\"status\" events are the feed reporting on itself.")

_out = threading.Lock()
_child = None
_stopping = False


def log(msg):
    print(f"samcloud-channel: {msg}", file=sys.stderr, flush=True)


def send(obj):
    with _out:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def push(content, **meta):
    send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
          "params": {"content": content, "meta": {k: str(v) for k, v in meta.items() if v}}})


def loaded_as_channel():
    """True when the session that started this server loaded it as a channel:
    its command line carries `--dangerously-load-development-channels` or
    `--channels` naming server:samcloud. SAMCLOUD_CHANNEL=1 says so explicitly
    for a harness whose command line cannot be read."""
    if os.environ.get("SAMCLOUD_CHANNEL") == "1":
        return True
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(os.getppid())],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return "channels" in cmd and "server:samcloud" in cmd


def conversation():
    return (os.environ.get("SAMCLOUD_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()


def subscriber_source():
    local = os.environ.get("SAMCLOUD_SUBSCRIBE_PATH")
    if local:
        return open(os.path.expanduser(local)).read()
    with open(HDR) as f:
        name, _, value = f.read().strip().partition(":")
    req = urllib.request.Request(f"{API}/helpers/subscribe", headers={name.strip(): value.strip()})
    return urllib.request.urlopen(req, timeout=20).read().decode()


def run_feed():
    """The subscriber, kept running. It reconnects on its own; this restarts
    it only if the process itself ends, with a growing pause."""
    global _child
    pause = 2
    # Opt-in, never ambient: a user-scope MCP server starts in EVERY session of
    # the account — headless rail holders included — and a feed running in one
    # would register a surface and receipt messages that are the holder's.
    # Only a launch that loads the channel says SAMCLOUD_CHANNEL=1.
    if os.environ.get("SAMCLOUD_HOLDER") or not loaded_as_channel():
        log("idle: this session did not load server:samcloud as a channel (or it is a rail holder)")
        return
    conv = conversation()
    if not conv:
        push("samcloud-channel has no conversation id (neither CLAUDE_CODE_SESSION_ID nor "
             "SAMCLOUD_SESSION_ID reached it), so it cannot scope a feed to this session. "
             "Nothing will arrive here.", kind="status")
        return
    while not _stopping:
        try:
            src = subscriber_source()
        except Exception as e:
            log(f"cannot fetch the subscriber ({e}); retrying in {pause}s")
            time.sleep(pause); pause = min(pause * 2, 300)
            continue
        env = dict(os.environ, SAMCLOUD_SUB_SINK="jsonl", SAMCLOUD_SESSION_ID=conv, SAMCLOUD_API=API)
        _child = subprocess.Popen([sys.executable, "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=sys.stderr, env=env, text=True, bufsize=1)
        _child.stdin.write(src); _child.stdin.close()
        started = time.time()
        for line in _child.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                m = None
            if isinstance(m, dict) and "text" in m:
                key = str(m.get("key") or "")
                push(m["text"], thread=key[7:] if key.startswith("thread-") else key)
            elif line.startswith("backlog:") or "registered" in line and "but this" in line:
                push(line, kind="status")
            else:
                log(line)
        _child.wait()
        if _stopping:
            return
        if time.time() - started > 60:
            pause = 2
        log(f"subscriber exited ({_child.returncode}); restarting in {pause}s")
        time.sleep(pause); pause = min(pause * 2, 300)


def stop(*_a):
    global _stopping
    _stopping = True
    if _child and _child.poll() is None:
        _child.send_signal(signal.SIGTERM)      # the subscriber says goodbye: the surface ends
        try:
            _child.wait(timeout=5)
        except Exception:
            _child.kill()
    os._exit(0)


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = False
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            # The 2026-07-28 runtime cannot deliver channel messages: answer an
            # earlier revision so the session keeps the channel path.
            version = asked if asked < "2026-07-28" else "2025-11-25"
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version,
                "capabilities": {"experimental": {"claude/channel": {}}},
                "serverInfo": {"name": "samcloud", "version": CHANNEL_VERSION},
                "instructions": INSTRUCTIONS}})
        elif method == "notifications/initialized":
            if not started:
                started = True
                threading.Thread(target=run_feed, daemon=True).start()
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"samcloud-channel has no {method}"}})
    stop()


if __name__ == "__main__":
    main()
