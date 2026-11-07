#!/usr/bin/env python3
"""Hermes protocol ownership: real subprocess, isolated state, no network."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

ADAPTER = Path(os.environ.get("ADAPTER_UNDER_TEST") or
               Path(__file__).resolve().parents[1] / "registry/helpers/samcloud-adapter.py")
INIT = {"type": "system", "subtype": "init", "session_id": "owned-turn"}
RESULT = {"type": "result", "session_id": "owned-turn", "exit_code": 0, "text": "323"}


class HermesOutput(unittest.TestCase):
    def run_case(self, events, *, resume="", rc=0, timeout=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = importlib.util.spec_from_file_location("adapter_hermes", ADAPTER)
            mod = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {"HOME": tmp}):
                spec.loader.exec_module(mod)
            mod.OUTPUT_MODE = "hermes-stream-json"
            mod.RUNTIME = "hermes"
            mod.CONV_CMD = "must-not-probe-newest-session"
            mod.CONV_RE = "(operator-session)"
            mod.RUNTIME_RESUME = mod.FAIL_RE = ""
            mod.TURN_TIMEOUT = .15 if timeout else 5
            lines = "\n".join(json.dumps(e) for e in events)
            script = (f"import sys,time; print({lines!r},flush=True); "
                      "print('session_id: operator-session',file=sys.stderr); "
                      + ("time.sleep(2)" if timeout else f"sys.exit({rc})"))
            mod.RUNTIME_CMD = shlex.join([sys.executable, "-c", script])
            prompt = root / "prompt"
            prompt.write_text("test")
            if resume:
                (root / "conv").write_text(resume)
            with patch.object(mod, "_extract_conv", side_effect=AssertionError("global probe")):
                result = mod.run_turn("test", "plane-conv", "test", prompt, root)
            saved = (root / "conv").read_text() if (root / "conv").exists() else ""
            return result, saved

    def test_answer_not_protocol_and_owned_handle(self):
        result, saved = self.run_case([INIT, {"type": "text", "text": "partial"}, RESULT])
        self.assertEqual((result[0], result[1], saved), ("accepted", "323", "owned-turn"))

    def test_resume_exact_handle(self):
        result, saved = self.run_case([INIT, RESULT], resume="owned-turn")
        self.assertEqual((result[0], saved), ("accepted", "owned-turn"))

    def test_wrong_resume_does_not_replace_handle(self):
        result, saved = self.run_case([INIT, RESULT], resume="original-turn")
        self.assertEqual((result[0], saved), ("failed", "original-turn"))

    def test_missing_result_preserves_work_but_fails(self):
        result, saved = self.run_case([INIT])
        self.assertEqual((result[0], result[1], saved), ("failed", "", "owned-turn"))

    def test_timeout_retains_only_this_turn_init(self):
        result, saved = self.run_case([INIT], timeout=True)
        self.assertEqual((result[0], saved), ("uncertain", "owned-turn"))

    def test_timeout_before_init_never_adopts_operator(self):
        result, saved = self.run_case([], timeout=True)
        self.assertEqual((result[0], saved), ("uncertain", ""))

    def test_invalid_results_fail_closed(self):
        bad = [dict(RESULT, exit_code=1), dict(RESULT, exit_code=False),
               dict(RESULT, text=""), dict(RESULT, text={"answer": "323"}),
               dict(RESULT, session_id="operator-session")]
        for event in bad:
            with self.subTest(event=event):
                result, _ = self.run_case([INIT, event])
                self.assertEqual(result[0], "failed")

    def test_duplicate_result_fails(self):
        result, _ = self.run_case([INIT, RESULT, RESULT])
        self.assertEqual(result[0], "failed")

    def test_no_init_fails(self):
        result, saved = self.run_case([RESULT])
        self.assertEqual((result[0], saved), ("failed", ""))

    def test_event_order_and_top_level_errors_fail(self):
        sequences = [
            [RESULT, INIT],
            [INIT, RESULT, {"type": "error", "message": "late failure"}],
            [INIT, {"type": "error", "message": "early failure"}, RESULT],
            [INIT, INIT, RESULT],
            [INIT, RESULT, {"type": "text", "text": "more work"}],
        ]
        for events in sequences:
            with self.subTest(events=events):
                result, _ = self.run_case(events)
                self.assertEqual(result[0], "failed")

    def test_nonzero_process_never_accepts_success_event(self):
        result, _ = self.run_case([INIT, RESULT], rc=7)
        self.assertEqual(result[0], "failed")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(HermesOutput))
    if result.wasSuccessful():
        print("ALL PASSED: Hermes turn-owned output")
    raise SystemExit(0 if result.wasSuccessful() else 1)
