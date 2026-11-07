"""Client-side Hermes stdio RPC transport; no Samcloud API or turn scheduler.

The caller supplies a Hermes profile home and a saved *stored* conversation ID.
Hermes owns the conversation, compression and in-session input queue. A submit
response is input acceptance, not inference completion or a posted answer.
"""
from concurrent.futures import Future, TimeoutError
import json
import os
from pathlib import Path
import subprocess
import threading


class ProtocolError(RuntimeError):
    pass


class RpcError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(f"Hermes rejected RPC: {error.get('code')}")


class Gateway:
    """One native gateway process, with responses independent of turn events.

    ``on_event`` runs on the reader thread: it must not block or call ``request``.
    RPC timeouts are ambiguous; this transport never retries them. stderr goes
    to the caller's log, not into the RPC stream. No credential is passed in argv.
    """

    def __init__(self, command, home, on_event=lambda event: None, *, stderr=None):
        self.home = str(Path(home).resolve(strict=True))
        self.on_event = on_event
        self._pending = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._failure = None
        env = dict(os.environ, HERMES_HOME=self.home)
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=stderr,
                                        text=True, encoding="utf-8", env=env)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _write(self, frame):
        with self._write_lock:
            self.process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _read(self):
        try:
            for line in self.process.stdout:
                frame = json.loads(line)
                if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                    raise ProtocolError("Invalid Hermes RPC envelope")
                if "method" in frame:
                    if "id" in frame:
                        # Headless client cannot invent an operator approval,
                        # secret, or clarification. Tell Hermes explicitly.
                        self._write({"jsonrpc": "2.0", "id": frame["id"],
                                     "error": {"code": -32601,
                                               "message": "No interactive request handler on this client"}})
                    else:
                        self.on_event(frame)
                    continue
                rid = frame.get("id")
                if type(rid) is not int or (("result" in frame) == ("error" in frame)):
                    raise ProtocolError("Invalid Hermes RPC response")
                with self._lock:
                    future = self._pending.pop(rid, None)
                if future is None:
                    raise ProtocolError("Unmatched Hermes RPC response")
                if "error" in frame:
                    error = frame["error"]
                    future.set_exception(RpcError(error) if isinstance(error, dict)
                                         else ProtocolError("Invalid Hermes RPC error"))
                elif not isinstance(frame["result"], dict):
                    future.set_exception(ProtocolError("Invalid Hermes RPC result"))
                else:
                    future.set_result(frame["result"])
            raise EOFError("Hermes gateway closed its output")
        except Exception as error:
            with self._lock:
                self._failure = error
                pending, self._pending = self._pending, {}
            for future in pending.values():
                future.set_exception(error)

    def request(self, method, params, *, timeout=30):
        with self._lock:
            if self._failure is not None:
                raise self._failure
            self._next_id += 1
            rid = self._next_id
            future = self._pending[rid] = Future()
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except Exception:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        try:
            return future.result(timeout)
        except TimeoutError as error:
            # Keep the pending entry: a late response is still this request's,
            # never the next one's. Neither a timeout nor EOF means rejected.
            raise TimeoutError("Hermes acceptance unknown; do not replay automatically") from error

    def check_health(self):
        """Notice a dead/broken child even when no RPC is currently pending."""
        with self._lock:
            if self._failure is not None:
                raise self._failure
        if self.process.poll() is not None:
            raise EOFError("Hermes gateway exited")

    def close(self):
        # Stop only the gateway this client launched, not any seat or sibling.
        with self._write_lock:
            if not self.process.stdin.closed:
                self.process.stdin.close()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._reader.join(5)
        self.process.stdout.close()


class Conversation:
    """A native live handle plus its durable resume key, not a second history."""

    def __init__(self, gateway, stored_id=None):
        self.gateway = gateway
        if stored_id is None:
            result = gateway.request("session.create", {"source": "tui"})
            stored = result.get("stored_session_id")
        else:
            if not isinstance(stored_id, str) or not stored_id:
                raise ValueError("A stored conversation ID is required to resume")
            result = gateway.request("session.resume", {"session_id": stored_id})
            # Hermes resolves compression continuations itself. Do not infer a
            # different conversation from latest-session lists or timestamps.
            stored = result.get("session_key") or result.get("stored_session_id")
        live = result.get("session_id")
        if not isinstance(live, str) or not live or not isinstance(stored, str) or not stored:
            raise ProtocolError("Hermes did not identify the conversation")
        self.live_id, self.stored_id = live, stored

    def submit(self, text, *, timeout=30):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A nonempty message is required")
        result = self.gateway.request("prompt.submit", {
            "session_id": self.live_id, "text": text, "queued": True,
        }, timeout=timeout)
        # Force native follow-up queueing, never redirect/cancel an active turn.
        if result.get("status") not in ("streaming", "queued"):
            raise ProtocolError("Hermes did not confirm message acceptance")
        return result
