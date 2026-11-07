"""Transport tests with a real subprocess and no inference/network."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from native import Conversation, Gateway, ProtocolError, RpcError


FAKE = r'''
import json, sys, time
def send(frame):
    print(json.dumps(dict(jsonrpc="2.0", **frame)), flush=True)
for line in sys.stdin:
    r = json.loads(line)
    method, p = r["method"], r["params"]
    if method == "session.create":
        result = {"session_id":"live-a", "stored_session_id":"stored-a"}
    elif method == "session.resume":
        if p["session_id"] == "missing":
            send({"id":r["id"], "error":{"code":4007,"message":"session not found"}})
            continue
        result = {"session_id":"live-r", "session_key":p["session_id"]}
    elif method == "prompt.submit":
        if p["text"] == "exit":
            sys.exit(0)
        if p["text"] == "slow":
            time.sleep(.15)
        if p["text"] == "malformed":
            print("[]", flush=True)
            continue
        send({"method":"event", "params":{"type":"tool.start", "session_id":p["session_id"]}})
        result = {"status": "queued" if p["text"] == "busy" else "streaming",
                  "queued_parameter":p["queued"], "target":p["session_id"]}
        if p["text"] == "bad-status":
            result = {"status":"redirected"}
    elif method == "approval-test":
        send({"method":"approval","id":"srq-1","params":{}})
        answer=json.loads(sys.stdin.readline())
        result={"answer":answer}
    elif method == "home":
        import os
        result={"home":os.environ["HERMES_HOME"]}
    else:
        result={}
    send({"id":r["id"], "result":result})
'''


class NativeTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.events = []
        self.gateway = Gateway([sys.executable, "-u", "-c", FAKE], self.home.name, self.events.append)
        self.addCleanup(self.home.cleanup)
        self.addCleanup(self.gateway.close)

    def test_create_and_accept_without_answer(self):
        c = Conversation(self.gateway)
        self.assertEqual(c.stored_id, "stored-a")
        result = c.submit("hello")
        self.assertEqual(result["status"], "streaming")
        self.assertEqual(result["target"], "live-a")
        self.assertTrue(result["queued_parameter"])
        self.assertEqual([e["params"]["type"] for e in self.events], ["tool.start"])
        # No result/answer event exists: transport acceptance did not wait for one.

    def test_resume_exact_id_not_latest(self):
        c = Conversation(self.gateway, "older-stored-session")
        self.assertEqual(c.stored_id, "older-stored-session")
        self.assertEqual(c.submit("hello")["target"], "live-r")

    def test_missing_resume_refused_without_new_conversation(self):
        with self.assertRaises(RpcError) as raised:
            Conversation(self.gateway, "missing")
        self.assertEqual(raised.exception.error["code"], 4007)

    def test_busy_is_native_queue_not_cancel_or_answer(self):
        self.assertEqual(Conversation(self.gateway).submit("busy")["status"], "queued")

    def test_late_acceptance_is_not_retried_or_adopted_by_next_request(self):
        c = Conversation(self.gateway)
        with self.assertRaisesRegex(TimeoutError, "unknown"):
            c.submit("slow", timeout=.01)
        self.assertEqual(c.submit("busy")["status"], "queued")
        self.assertEqual(len(self.events), 2)  # once per send, no retry

    def test_eof_is_not_receipt(self):
        with self.assertRaises(EOFError):
            Conversation(self.gateway).submit("exit")

    def test_malformed_response_is_not_receipt(self):
        with self.assertRaises(ProtocolError):
            Conversation(self.gateway).submit("malformed")

    def test_unexpected_acceptance_kind_refused(self):
        with self.assertRaises(ProtocolError):
            Conversation(self.gateway).submit("bad-status")

    def test_no_automatic_approval(self):
        result = self.gateway.request("approval-test", {})
        self.assertEqual(result["answer"]["id"], "srq-1")
        self.assertEqual(result["answer"]["error"]["code"], -32601)

    def test_explicit_profile_home(self):
        self.assertEqual(self.gateway.request("home", {})["home"], str(Path(self.home.name).resolve()))

    def test_concurrent_requests_keep_their_responses(self):
        results = {}
        c = Conversation(self.gateway)
        threads = [threading.Thread(target=lambda text=text: results.update({text:c.submit(text)}))
                   for text in ("slow", "busy")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(results["slow"]["status"], "streaming")
        self.assertEqual(results["busy"]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
