"""Real stream-client subprocesses; fake native transport, no network/inference."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from binding import Binding
from native import ProtocolError
from stream import message_text


FAKE = r'''
import json, os, sys, threading
def send(**frame):
    print(json.dumps(dict(jsonrpc="2.0", **frame)), flush=True)
for line in sys.stdin:
    req=json.loads(line)
    method,p=req["method"],req["params"]
    if method == "session.create":
        stored="native-first"
        result={"session_id":"live","stored_session_id":stored}
    elif method == "session.resume":
        stored=p["session_id"]
        if stored == "missing-native-draft":
            send(id=req["id"],error={"code":4007})
            continue
        result={"session_id":"resumed","session_key":stored}
    else:
        text=p["text"]
        if text == "exit": sys.exit(1)
        if text == "malformed":
            print("not json",flush=True)
            continue
        path=os.path.join(os.environ["HERMES_HOME"],".samcloud-client","conversations",os.environ["SAMCLOUD_SESSION_ID"]+".json")
        result={"status":"queued" if text == "busy" else "streaming", "user_row_id":41,
                "target":p["session_id"],"queued":p["queued"],"stored":stored,
                "map_before_input":json.load(open(path))["stored_id"],
                "stamp":os.environ["SAMCLOUD_SESSION_ID"],
                "client_pid":int(os.environ["SAMCLOUD_CLIENT_PID"])}
        # Terminal may precede the prompt.submit response; the client must not lose it.
        if text in ("complete","error","interrupted","unknown", "wrong-turn", "partial", "no-receipt"):
            status="complete" if text in ("wrong-turn","partial","no-receipt") else text
            persisted={"user_row_id":999 if text == "wrong-turn" else 41,
                       "complete":text != "partial", "final_assistant_row_id":42}
            payload={"status":status}
            if text != "no-receipt": payload["persisted_turn"]=persisted
            send(method="event",params={"type":"message.complete","session_id":p["session_id"],
                                         "payload":payload})
    send(id=req["id"],result=result)
    if method == "prompt.submit" and p["text"] == "die-later":
        threading.Timer(.15, lambda: os._exit(1)).start()
'''


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sid = str(uuid.uuid4())
        self.home = self.tmp.name

    def launch(self, *, resume=False, oneshot=False, inherited=None):
        env = dict(os.environ)
        env.pop("SAMCLOUD_SESSION_ID", None)
        if inherited:
            env["SAMCLOUD_SESSION_ID"] = inherited
        command = [sys.executable, str(Path(__file__).with_name("stream.py")),
                   "--home", self.home, "--resume" if resume else "--session-id", self.sid]
        if oneshot:
            command.append("--oneshot")
        command += ["--", sys.executable, "-u", "-c", FAKE]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env)
        frames = queue.Queue()
        def read():
            for line in process.stdout:
                frames.put(json.loads(line))
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def cleanup():
            if process.poll() is None:
                process.terminate()
                process.wait(12)
            reader.join(2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.addCleanup(cleanup)
        return process, frames

    def send(self, process, text):
        process.stdin.write(json.dumps({"type":"user", "message":{"role":"user","content":text}})+"\n")
        process.stdin.flush()

    def test_mapping_before_acceptance_exact_resume_and_stamp(self):
        first, frames = self.launch()
        self.assertEqual(frames.get(timeout=5)["type"], "ready")
        self.send(first, "busy")
        received = frames.get(timeout=5)
        self.assertEqual(received["type"], "accepted")
        receipt = received["receipt"]
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(receipt["map_before_input"], "native-first")
        self.assertEqual(receipt["stamp"], self.sid)
        self.assertEqual(receipt["client_pid"], first.pid)
        self.assertTrue(receipt["queued"])
        self.assertTrue(frames.empty())  # No fake answer or completed-work record.
        first.terminate()
        self.assertEqual(first.wait(12), 0)
        second, frames = self.launch(resume=True)
        self.assertEqual(frames.get(timeout=5)["stored_id"], "native-first")
        self.send(second, "continue")
        receipt = frames.get(timeout=5)["receipt"]
        self.assertEqual(receipt["target"], "resumed")
        self.assertEqual(receipt["stored"], "native-first")

    def test_native_missing_draft_refused_not_recreated(self):
        with Binding(self.home, self.sid, resume=False) as binding:
            binding.save("missing-native-draft")
        process, frames = self.launch(resume=True)
        self.assertEqual(process.wait(5), 1)
        self.assertTrue(frames.empty())
        with Binding(self.home, self.sid, resume=True) as binding:
            self.assertEqual(binding.stored_id, "missing-native-draft")

    def test_wrong_inherited_conversation_refused(self):
        process, frames = self.launch(inherited=str(uuid.uuid4()))
        self.assertEqual(process.wait(5), 1)
        self.assertTrue(frames.empty())
        self.assertFalse(list(Path(self.home).rglob("*.json")))

    def test_quiet_input_does_not_hide_dead_runtime(self):
        process, frames = self.launch()
        frames.get(timeout=5)
        self.send(process, "die-later")
        self.assertEqual(frames.get(timeout=5)["type"], "accepted")
        self.assertEqual(process.wait(5), 1)
        self.assertTrue(frames.empty())

    def test_malformed_response_not_accepted(self):
        process, frames = self.launch()
        frames.get(timeout=5)
        self.send(process, "malformed")
        self.assertEqual(process.wait(5), 1)
        self.assertTrue(frames.empty())

    def test_multiple_inputs_in_one_os_read(self):
        process, frames = self.launch()
        frames.get(timeout=5)
        body = lambda s: json.dumps({"type":"user","message":{"role":"user","content":s}})+"\n"
        process.stdin.write(body("first") + body("busy"))
        process.stdin.flush()
        self.assertEqual(frames.get(timeout=5)["receipt"]["status"], "streaming")
        self.assertEqual(frames.get(timeout=5)["receipt"]["status"], "queued")

    def test_oneshot_uses_terminal_status_not_acceptance(self):
        for status, expected in (("complete",0),("error",1),("interrupted",1),("unknown",1)):
            with self.subTest(status=status):
                self.sid = str(uuid.uuid4())
                process, frames = self.launch(oneshot=True)
                frames.get(timeout=5)
                process.stdin.write(status)
                process.stdin.close()
                self.assertEqual(process.wait(5), expected)

    def test_partial_envelope_and_context_only_request_refused(self):
        for data in ('{"type":', json.dumps({"type":"user","message":{"role":"user","content":"fact"},"infer":False})+"\n"):
            with self.subTest(data=data):
                self.sid = str(uuid.uuid4())
                process, frames = self.launch()
                frames.get(timeout=5)
                process.stdin.write(data)
                process.stdin.close()
                self.assertEqual(process.wait(5), 1)
                self.assertTrue(frames.empty())

    def test_oneshot_rejects_unowned_or_partial_terminal_receipt(self):
        for text in ("wrong-turn", "partial", "no-receipt", "busy"):
            with self.subTest(text=text):
                self.sid = str(uuid.uuid4())
                process, frames = self.launch(oneshot=True)
                frames.get(timeout=5)
                process.stdin.write(text)
                process.stdin.close()
                self.assertEqual(process.wait(5), 1)

    def test_no_coercion_of_wrong_envelope(self):
        for frame in (None, [], {"type":"assistant","message":{"role":"user","content":"hi"}},
                      {"type":"user","message":{"role":"user","content":[]}}):
            with self.subTest(frame=frame), self.assertRaises(ProtocolError):
                message_text(frame)


if __name__ == "__main__":
    unittest.main()
