#!/usr/bin/env python3
"""Runtime process failures never become answers, even with useful stdout.

Real adapter main + real subprocess; fake plane; temporary state only.
ADAPTER_UNDER_TEST can point at a baseline helper for a negative check.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
ADAPTER = Path(os.environ.get("ADAPTER_UNDER_TEST") or REPO / "registry/helpers/samcloud-adapter.py")


class ExitStatus(unittest.TestCase):
    def run_case(self, exit_code, text="Partial answer", failure_pattern="", signal=False):
        with tempfile.TemporaryDirectory(prefix="adapter-exit-") as tmp:
            root = Path(tmp)
            spec = importlib.util.spec_from_file_location("adapter_exit", ADAPTER)
            mod = importlib.util.module_from_spec(spec)
            with patch("os.path.expanduser", side_effect=lambda p: str(root) + p[1:] if p.startswith("~") else p):
                spec.loader.exec_module(mod)
            mod.STATE_DIR = root
            mod.RUNTIME = "fixture"
            script = ("import os,signal,sys; "
                      f"print({text!r}, flush=True); "
                      "print('session_id: turn-owned-id', file=sys.stderr, flush=True); "
                      + ("os.kill(os.getpid(), signal.SIGTERM)" if signal else f"sys.exit({exit_code})"))
            mod.RUNTIME_CMD = shlex.join([sys.executable, "-c", script])
            mod.RUNTIME_RESUME = mod.CONV_CMD = mod.LEASE_RESOURCE = ""
            mod.CONV_RE = r"session_id: ([a-z-]+)"
            mod.FAIL_RE = failure_pattern
            mod.TURN_TIMEOUT = mod.LOCK_WAIT = 10
            calls = []
            def api(method, path, body=None, **kwargs):
                calls.append((method, path, body))
                return {"comment_id": 101} if path.endswith("/comments") else {}
            mod._api = api
            mod.log = lambda *args: None
            prompt = root / "prompt.txt"
            prompt.write_text("Do the work")
            argv = ["adapter", "--key", "thread-test", "--sid", "test-conversation",
                    "--prompt-file", str(prompt), "--reply-to", "task 42", "--ids", "[17]"]
            with patch.object(sys, "argv", argv):
                rc = mod.main()
            tdir = root / "threads/thread-test"
            record = json.loads((tdir / "adapter.jsonl").read_text().splitlines()[-1])
            return rc, record, calls, (tdir / "conv").read_text()

    def assert_failed_without_reply(self, result, text):
        rc, record, calls, conv = result
        self.assertEqual(rc, 1)
        self.assertEqual(record["outcome"], "failed")
        self.assertIsNone(record["comment"])
        self.assertFalse(any(path.endswith("/comments") or path.endswith("/ack") for _, path, _ in calls))
        if text:
            self.assertEqual(record["unposted"], text)
        self.assertEqual(conv, "turn-owned-id")

    def test_nonzero_stdout_is_not_a_success(self):
        self.assert_failed_without_reply(self.run_case(7), "Partial answer")

    def test_signal_after_stdout_is_not_a_success(self):
        self.assert_failed_without_reply(self.run_case(0, signal=True), "Partial answer")

    def test_nonzero_without_stdout_remains_failed(self):
        self.assert_failed_without_reply(self.run_case(1, text=""), "")

    def test_zero_with_answer_posts_then_acks(self):
        rc, record, calls, _ = self.run_case(0, text="Finished")
        self.assertEqual(rc, 0)
        self.assertEqual(record["outcome"], "accepted")
        paths = [p for _, p, _ in calls]
        self.assertLess(paths.index("/tasks/42/comments"), paths.index("/notifications/ack"))

    def test_declared_failure_stays_unavailable_for_either_exit(self):
        for exit_code in (0, 2):
            with self.subTest(exit_code=exit_code):
                rc, record, calls, _ = self.run_case(exit_code, "pool_unavailable", "pool_unavailable")
                self.assertEqual(rc, 1)
                self.assertEqual(record["outcome"], "unavailable")
                self.assertEqual(record["unposted"], "pool_unavailable")
                self.assertFalse(any(p.endswith("/comments") or p.endswith("/ack") for _, p, _ in calls))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ExitStatus)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("ALL PASSED: adapter exit status")
    raise SystemExit(0 if result.wasSuccessful() else 1)
