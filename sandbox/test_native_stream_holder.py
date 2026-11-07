#!/usr/bin/env python3
"""Real rail dispatch + stream client subprocesses, fake native RPC and plane.

The only HTTP listener is a loopback fixture for the existing launcher's exit
and ack calls. No inference, credentials or live Samcloud API are used.
RAIL_UNDER_TEST selects a baseline helper for the red check.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import Mock, patch
import uuid

REPO = Path(__file__).resolve().parents[1]
NATIVE = r'''
import json, os, signal, sys, uuid
from pathlib import Path
path=Path(os.environ["HERMES_HOME"])/"fixture-native-history.json"
def send(**frame):
    print(json.dumps(dict(jsonrpc="2.0",**frame)),flush=True)
for line in sys.stdin:
    r=json.loads(line); p=r["params"]
    if r["method"] == "session.create":
        stored="native-"+str(uuid.uuid4()); history=[]
        result={"session_id":"live","stored_session_id":stored}
    elif r["method"] == "session.resume":
        old=json.loads(path.read_text())
        assert old["stored"]==p["session_id"]
        stored,history=old["stored"],old["history"]
        result={"session_id":"live","session_key":stored}
    else:
        history.append(p["text"])
        path.write_text(json.dumps({"stored":stored,"history":history,
                                  "plane_id":os.environ["SAMCLOUD_SESSION_ID"]}))
        row=len(history)*2
        result={"status":"streaming","user_row_id":row}
        send(id=r["id"],result=result)
        send(method="event",params={"type":"message.complete","session_id":"live",
             "payload":{"status":"complete","persisted_turn":{"complete":True,
                        "user_row_id":row,"final_assistant_row_id":row+1}}})
        if "ENDCLIENT" in p["text"]:
            os.kill(int(os.environ["SAMCLOUD_CLIENT_PID"]),signal.SIGTERM)
        continue
    send(id=r["id"],result=result)
'''


class NativeHolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="native-holder-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "profile"
        self.home.mkdir()
        self.calls, self.processes = [], []
        calls = self.calls

        class Plane(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.command, self.path, data))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
            do_PATCH = do_POST
            def log_message(self, *_):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Plane)
        self.server = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.server.start()
        self.addCleanup(self.cleanup)
        command = shlex.join([sys.executable, str(REPO / "clients/hermes/stream.py"),
                              "--home", str(self.home), "--gateway-command",
                              shlex.join([sys.executable, "-u", "-c", NATIVE])])
        spec = importlib.util.spec_from_file_location("native_holder_rail",
            os.environ.get("RAIL_UNDER_TEST") or REPO / "registry/helpers/samcloud-rail.py")
        self.rail = rail = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"RAIL_RUNTIME":"hermes", "RAIL_STREAM_CMD":command,
                                     "RAIL_HOLDER":"stream", "SAMCLOUD_WAKE_CONF":str(self.root / "absent.env")}):
            with patch.object(Path, "home", return_value=self.root):
                spec.loader.exec_module(rail)
        self.assertTrue(rail.RUNTIME_STREAMS, "declared native stream did not select the streaming holder")
        rail.STATE_DIR = self.root / "rail"
        rail.STATE_DIR.mkdir()
        (rail.STATE_DIR / "auth.hdr").write_text("Authorization: Bearer fixture-only\n")
        rail.HOLDER_CWD = str(self.root)
        rail.API = f"http://127.0.0.1:{self.http.server_port}/api/v1"
        rail.DRY = False
        rail._resolve_entry = lambda key, reg: reg.get(key)
        rail._key_parts = lambda key: ("task", 42)
        rail._thread_id_of = lambda key: 88
        rail._object_open = Mock(return_value=True)
        rail._thread_done = Mock(return_value=None)
        rail._plane_binding = Mock(return_value={"state":"closed"})
        rail.close_signal = Mock(return_value=None)
        rail.release_dead_claims = Mock()
        rail._whoami = Mock(return_value="fixture")
        rail.log = Mock()
        rail.claude_exe = Mock(side_effect=AssertionError("must not launch Claude"))
        rail.start_adapter_turn = Mock(side_effect=AssertionError("must not launch batch adapter"))
        self.real_wake_prompt = rail.wake_prompt
        self.real_continue_prompt = rail.continue_prompt
        rail.wake_prompt = lambda key, rows, resumed, **kw: f"{'RESUME' if resumed else 'WAKE'} {rows[0]['id']}"
        rail.continue_prompt = lambda key, rows, closing=None, **kw: "ENDCLIENT" if closing else f"CONTINUE {rows[0]['id']}"
        rail.oneshot_prompt = lambda key, rows, fresh=False: f"ONESHOT {rows[0]['id']}"

        def api(method, path, body=None, *args, **kw):
            self.calls.append((method, path, body))
            if method == "POST" and path == "/wake/threads":
                return {"thread":{"id":88}}
            return {}
        rail._api = api
        rail._api_quiet = api
        original_popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            # ps/check_output also uses Popen; only track session-owning launchers.
            if kwargs.get("start_new_session"):
                self.processes.append(process)
            return process
        self.popen_patch = patch.object(rail.subprocess, "Popen", side_effect=launch)
        self.popen_patch.start()
        self.key, self.reg = "thread-88", {}

    def cleanup(self):
        if hasattr(self, "popen_patch"):
            self.popen_patch.stop()
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(7)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(5)
        self.http.shutdown()
        self.http.server_close()
        self.server.join(3)
        self.tmp.cleanup()

    def wait_for(self, predicate):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.02)
        self.fail("fixture condition not observed")

    def history(self):
        try:
            return json.loads((self.home / "fixture-native-history.json").read_text())
        except (FileNotFoundError, ValueError):
            return {"history":[]}

    def row(self, row_id):
        return {"id":row_id, "event_type":"task.commented", "task_id":42,
                "actor":"peer", "payload":{"body":"work"}}

    def dispatch(self, row_id):
        return self.rail.dispatch(self.key, [self.row(row_id)], self.reg)

    def test_wake_continue_same_client_and_no_automatic_post(self):
        self.assertEqual(self.dispatch(1), "wake")
        first = dict(self.reg[self.key])
        self.assertTrue(self.rail.holder_alive(first))
        self.assertEqual(self.dispatch(2), "continue")
        self.wait_for(lambda: len(self.history()["history"]) == 2)
        self.assertEqual(self.reg[self.key]["pid"], first["pid"])
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.history()["history"], ["WAKE 1", "CONTINUE 2"])
        self.assertEqual(self.history()["plane_id"], first["sid"])
        self.assertFalse(any(path.endswith("/comments") for _,path,_ in self.calls))
        self.assertEqual(self.rail._reply_sentence(), "")
        self.assertIn("sc-say", self.rail._footer(self.key))
        self.assertIn("SAMCLOUD_CLIENT_PID", self.rail._end_sentence())

    def test_real_capped_birth_delivers_all_batches_on_one_native_client(self):
        self.rail.wake_prompt = self.real_wake_prompt
        self.rail.continue_prompt = self.real_continue_prompt
        self.rail.PAYLOAD_CAP = 4096
        rows = [self.row(1), self.row(2)]
        for row in rows:
            row['payload']['body'] = f"CAP-MARKER-{row['id']} " + 'x' * 2700
        self.assertEqual(self.rail.dispatch(self.key, rows, self.reg), 'wake')
        self.wait_for(lambda: len(self.history()['history']) == 2)
        self.assertEqual(len(self.processes), 1)
        first, second = self.history()['history']
        self.assertIn('New thread', first)
        self.assertNotIn('New thread', second)
        self.assertIn('CAP-MARKER-1', first)
        self.assertIn('CAP-MARKER-2', second)
        receipts = [body['ids'] for method, path, body in self.calls
                    if method == 'POST' and path == '/notifications/delivered']
        self.assertEqual(receipts, [[1], [2]])

    def test_dead_client_resumes_same_plane_and_native_id_without_claude_log(self):
        self.dispatch(1)
        self.wait_for(lambda: bool(self.history()["history"]))
        first, native = dict(self.reg[self.key]), self.history()["stored"]
        os.kill(first["pid"], signal.SIGTERM)
        self.processes[0].wait(8)
        self.assertFalse(self.rail.session_file_exists(first["sid"]))
        self.assertEqual(self.dispatch(2), "resume")
        self.wait_for(lambda: len(self.history()["history"]) == 2)
        self.assertEqual(self.reg[self.key]["sid"], first["sid"])
        self.assertNotEqual(self.reg[self.key]["pid"], first["pid"])
        self.assertNotEqual(self.reg[self.key]["out"], first["out"])
        self.assertEqual(self.history()["stored"], native)
        self.assertEqual(self.history()["history"][-1], "RESUME 2")

    def test_wrap_up_closes_through_existing_launcher(self):
        self.dispatch(1)
        self.rail.close_signal.return_value = ("peer", "done")
        self.assertEqual(self.dispatch(2), "closing")
        self.processes[0].wait(8)
        self.assertTrue(any(method == "PATCH" and body == {"state":"closed"}
                            for method,_,body in self.calls))
        self.rail.sweep(self.reg)
        self.assertEqual(self.reg[self.key]["state"], "closed")

    def test_two_late_oneshots_create_then_resume_not_two_conversations(self):
        self.rail._object_open.return_value = False
        self.assertEqual(self.dispatch(1), "oneshot")
        self.processes[0].wait(8)
        original = self.history()["stored"]
        self.assertFalse(self.reg[self.key]["fresh"])
        self.assertEqual(self.dispatch(2), "oneshot")
        self.processes[1].wait(8)
        self.assertEqual(self.history()["stored"], original)
        self.assertEqual(self.history()["history"], ["ONESHOT 1", "ONESHOT 2"])
        self.assertEqual(sum(path.endswith("/notifications/ack") for _,path,_ in self.calls), 2)

    def test_missing_native_mapping_never_becomes_fresh_or_receipted(self):
        sid = str(uuid.uuid4())
        self.reg[self.key] = {"key":self.key,"sid":sid,"holder":"stream","state":"closed"}
        with self.assertRaisesRegex(RuntimeError, "before accepting"):
            self.dispatch(1)
        self.assertEqual(self.reg[self.key]["sid"], sid)
        self.assertIn("pid", self.reg[self.key])  # failed startup remains inspectable
        self.assertFalse(any(path.endswith("/notifications/delivered") for _,path,_ in self.calls))
        self.assertFalse((self.home / "fixture-native-history.json").exists())

    def test_claude_health_and_rekick_rules_do_not_apply(self):
        self.dispatch(1)
        entry = self.reg[self.key]
        out = self.rail.STATE_DIR / "threads" / self.key / entry["out"]
        with out.open("a") as stream:
            stream.write(json.dumps({"type":"result","result":"Not logged in · Please run /login"})+"\n")
        self.assertTrue(self.rail.stream_alive(entry))
        self.rail._last_result = Mock(side_effect=AssertionError("client owns runtime recovery"))
        self.assertEqual(self.rail.rekick_after_limit(self.reg), [])
        with patch.object(self.rail.os, "kill") as kill:
            self.rail._reap_holder(entry)
            kill.assert_not_called()
        self.rail.MINT_URL = "https://must-not-contact.invalid"
        self.assertEqual(self.rail._mint_oauth(), "")

    def test_ready_must_name_this_conversation(self):
        output = self.root / "ready.jsonl"
        output.write_text(json.dumps({"type":"ready","plane_id":"another"})+"\n")
        with patch.object(self.rail, "_pid_alive", return_value=False):
            with self.assertRaises(RuntimeError):
                self.rail._wait_stream_ready(output, 123, "expected")

    def test_existing_batch_binding_requires_deliberate_migration(self):
        for kind in ("adapter", "tmux", "conversation"):
            with self.subTest(kind=kind):
                original = {"key":self.key, "sid":str(uuid.uuid4()), "holder":kind, "state":"open"}
                self.reg[self.key] = dict(original)
                with self.assertRaisesRegex(RuntimeError, "explicit migration"):
                    self.dispatch(1)
                self.assertEqual(self.reg[self.key], original)
        self.assertFalse(self.processes)
        self.assertFalse(self.calls)

    def test_readiness_timeout_keeps_process_for_next_delivery(self):
        with patch.object(self.rail, "_wait_stream_ready", side_effect=RuntimeError("fixture startup timeout")):
            with self.assertRaisesRegex(RuntimeError, "startup timeout"):
                self.dispatch(1)
        first = dict(self.reg[self.key])
        self.assertFalse(any(path.endswith("/notifications/delivered") for _,path,_ in self.calls))
        self.assertFalse(self.history()["history"])
        self.rail.contract_refresh = lambda key: ""
        with patch.object(self.rail, "deliver_stream", side_effect=OSError("FIFO unavailable")):
            with self.assertRaisesRegex(OSError, "FIFO unavailable"):
                self.dispatch(1)
        self.assertEqual(self.reg[self.key]["contract_v"], "")
        self.assertFalse(any(path.endswith("/notifications/delivered") for _,path,_ in self.calls))
        self.assertEqual(self.dispatch(1), "continue")
        self.wait_for(lambda: bool(self.history()["history"]))
        self.assertEqual(self.reg[self.key]["pid"], first["pid"])
        self.assertEqual(self.reg[self.key]["sid"], first["sid"])
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.history()["history"], ["WAKE 1"])
        self.assertEqual(self.reg[self.key]["contract_v"], self.rail.RAIL_VERSION)
        self.assertEqual(self.dispatch(2), "continue")
        self.wait_for(lambda: len(self.history()["history"]) == 2)
        self.assertEqual(self.history()["history"][-1], "CONTINUE 2")

    def test_resumed_startup_timeout_keeps_resume_wake(self):
        self.dispatch(1)
        self.wait_for(lambda: bool(self.history()["history"]))
        first = dict(self.reg[self.key])
        os.kill(first["pid"], signal.SIGTERM)
        self.processes[0].wait(8)
        with patch.object(self.rail, "_wait_stream_ready", side_effect=RuntimeError("startup timeout")):
            with self.assertRaisesRegex(RuntimeError, "startup timeout"):
                self.dispatch(2)
        self.rail.contract_refresh = lambda key: ""
        self.dispatch(2)
        self.wait_for(lambda: len(self.history()["history"]) == 2)
        self.assertEqual(self.history()["history"], ["WAKE 1", "RESUME 2"])

    def test_ready_record_waits_for_newline(self):
        output = self.root / "fragmented-ready.jsonl"
        record = json.dumps({"type": "ready", "plane_id": "expected"})
        for initial in (record[:12], record):
            with self.subTest(initial=initial):
                output.write_text(initial)
                def finish_record(_delay):
                    with output.open("a") as stream:
                        stream.write(record[len(initial):] + "\n")
                with patch.object(self.rail.time, "sleep", side_effect=finish_record) as sleep:
                    with patch.object(self.rail, "_pid_alive", side_effect=[True, False]):
                        self.rail._wait_stream_ready(output, 123, "expected")
                    sleep.assert_called_once()

    def test_delayed_wake_with_close_has_one_footer_after_wrap(self):
        with patch.object(self.rail, "_wait_stream_ready", side_effect=RuntimeError("startup timeout")):
            with self.assertRaisesRegex(RuntimeError, "startup timeout"):
                self.dispatch(1)
        self.rail.wake_prompt = self.real_wake_prompt
        self.rail._note_reply_target = Mock()
        self.rail._digest = Mock(return_value=("TASK CONTEXT", []))
        self.rail._thread_title = Mock(return_value="task 42")
        self.rail._birth_kind = Mock(return_value="assignment")
        self.rail._conversation_sentence = Mock(return_value="CONVERSATION")
        self.rail._footer = Mock(return_value="ONLY FOOTER")
        self.rail._close_text = Mock(return_value="CLOSE INSTRUCTION")
        self.rail.close_signal.return_value = ("peer", "done")
        self.assertEqual(self.dispatch(1), "closing")
        self.wait_for(lambda: bool(self.history()["history"]))
        text = self.history()["history"][0]
        self.assertIn("New thread", text)
        self.assertIn("TASK CONTEXT", text)
        self.assertEqual(text.count("ONLY FOOTER"), 1)
        self.assertLess(text.index("TASK CONTEXT"), text.index("CLOSE INSTRUCTION"))
        self.assertTrue(text.endswith("CLOSE INSTRUCTION\n\nONLY FOOTER"))
        self.assertEqual(self.reg[self.key]["contract_v"], self.rail.RAIL_VERSION)

    def test_default_claude_command_is_preserved(self):
        self.rail.STREAM_CMD = ""
        self.rail.claude_exe = lambda: "claude-fixture"
        stream = self.rail._stream_command("same-id", True)
        self.assertEqual(stream[:2], ["claude-fixture", "-p"])
        self.assertEqual(stream[stream.index("--resume")+1], "same-id")
        self.assertEqual(stream[stream.index("--input-format")+1], "stream-json")
        self.assertNotIn("--oneshot", stream)
        once = self.rail._stream_command("new-id", False, oneshot=True)
        self.assertEqual(once[once.index("--session-id")+1], "new-id")
        self.assertEqual(once[once.index("--output-format")+1], "json")
        self.assertNotIn("--input-format", once)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeHolder))
    if result.wasSuccessful():
        print("ALL PASSED: native streaming holder")
    sys.exit(0 if result.wasSuccessful() else 1)
