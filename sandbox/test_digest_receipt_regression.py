#!/usr/bin/env python3
"""Receipt must never cover an entirely omitted notification (task 828).

Real dispatch, continue_prompt and _digest; only process/FIFO/API boundaries
are replaced. No runtime, inference, credentials or network access.
RAIL_UNDER_TEST selects a baseline for the red check.
"""
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parents[1]


class DigestReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rail-digest-receipt-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location(
            "receipt_cap_rail", os.environ.get("RAIL_UNDER_TEST")
            or REPO / "registry/helpers/samcloud-rail.py")
        self.rail = rail = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            "HOME": str(root), "RAIL_RUNTIME": "hermes",
            "RAIL_STREAM_CMD": "fixture-native-client", "RAIL_HOLDER": "stream",
            "RAIL_STATE_DIR": str(root / "state"),
            "SAMCLOUD_WAKE_CONF": str(root / "absent.env"),
        }), patch.object(Path, "home", return_value=root):
            spec.loader.exec_module(rail)
        rail.DRY = False
        rail.PAYLOAD_CAP = 4096
        rail.DIGEST_BODY_CAP = 16000
        self.sent, self.receipts = [], []

        def api(method, path, body=None, *args, **kwargs):
            if method == "GET" and path == "/threads/88":
                return {"root": "task:42", "state": "open", "open_members": 1}
            if method == "GET" and path == "/auth/verify":
                return {"username": "fixture"}
            if method == "POST" and path == "/notifications/delivered":
                self.receipts.extend(body["ids"])
                return {}
            if method == "POST" and path == "/wake/threads":
                return {"thread": {"id": 88}}
            if method == "GET":
                return {}
            raise AssertionError(f"unexpected API write: {method} {path}")

        rail._api = api
        rail._api_quiet = api
        rail.holder_alive = Mock(return_value=True)
        rail._wait_stream_ready = Mock()
        rail.deliver_stream = lambda key, text: self.sent.append(text)
        rail.release_dead_claims = Mock()
        rail.log = Mock()
        self.key = "thread-88"
        self.reg = {self.key: {
            "key": self.key, "sid": "fixture-conversation", "holder": "stream",
            "state": "open", "pid": 123456789, "out": "fixture.jsonl",
            "contract_v": rail.RAIL_VERSION, "thread_id": 88,
        }}

    def row(self, number, chars):
        return {
            "id": number, "thread_id": 88, "task_id": 42,
            "event_type": "task.commented", "actor": "peer",
            "created_at": "2026-10-11T00:00:00Z",
            "payload": {"body": f"UNIQUE-ROW-{number}: " + "x" * chars},
        }

    def assert_receipts_visible(self):
        text = "\n".join(self.sent)
        for number in self.receipts:
            self.assertIn(f"UNIQUE-ROW-{number}:", text,
                          f"notification {number} receipted but absent from delivered text")

    def test_fitting_batch_control(self):
        rows = [self.row(1, 100), self.row(2, 100)]
        self.rail.dispatch(self.key, rows, self.reg)
        self.assertEqual(self.receipts, [1, 2])
        self.assert_receipts_visible()

    def test_capped_batch_never_receipts_omitted_row(self):
        rows = [self.row(1, 2700), self.row(2, 2700)]
        self.rail.dispatch(self.key, rows, self.reg)
        self.assertTrue(self.sent, "fixture never exercised a delivery")
        self.assertTrue(self.receipts, "fixture never exercised a receipt")
        self.assert_receipts_visible()
        self.assertEqual(self.receipts, [1, 2], "overflow must drain in this dispatch")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.rail.dispatch_counts[self.key], 2)

    def test_failed_second_send_receipts_only_first_batch(self):
        rows = [self.row(1, 2700), self.row(2, 2700)]
        def send(key, text):
            if self.sent:
                raise OSError("fixture second FIFO write failed")
            self.sent.append(text)
        self.rail.deliver_stream = send
        with self.assertRaisesRegex(OSError, "second FIFO"):
            self.rail.dispatch(self.key, rows, self.reg)
        self.assertEqual(self.receipts, [1])
        self.assert_receipts_visible()
        self.assertEqual(self.rail.dispatch_counts[self.key], 1)

    def test_single_oversized_row_is_shortened_sent_and_receipted(self):
        row = self.row(1, 5000)
        self.rail.dispatch(self.key, [row], self.reg)
        self.assertEqual(self.receipts, [1])
        self.assert_receipts_visible()
        digest, cuts = self.rail._digest([row])
        self.assertLessEqual(len(digest), self.rail.PAYLOAD_CAP)
        self.assertEqual(cuts, 1)
        self.assertIn('/tasks/42 for the rest', digest)

    def test_mention_tail_degrades_before_body_at_production_cap(self):
        self.rail.PAYLOAD_CAP = 24 * 1024
        self.reg[self.key]['contract_v'] = ''
        row = self.row(1, 15900)
        row['event_type'] = 'task.mentioned'
        row['payload']['thread_tail'] = [{'author': 'peer', 'body': 'TAIL' * 2250}]
        self.rail.dispatch(self.key, [row], self.reg)
        self.assertEqual(self.receipts, [1])
        self.assertIn(row['payload']['body'], self.sent[0])
        self.assertNotIn('TAIL', self.sent[0])
        self.assertIn('Earlier comments omitted', self.sent[0])
        self.assertIn('/tasks/42', self.sent[0])
        digest, _ = self.rail._digest([row])
        self.assertLessEqual(len(digest), self.rail.PAYLOAD_CAP)

    def test_tail_and_body_can_both_shorten_without_wedging(self):
        row = self.row(1, 18000)
        row['payload']['thread_tail'] = [{'author': 'peer', 'body': 'TAIL' * 4000}] * 3
        digest, cuts = self.rail._digest([row])
        self.assertLessEqual(len(digest), self.rail.PAYLOAD_CAP)
        self.assertEqual(cuts, 1)
        self.assertIn('Earlier comments omitted', digest)
        self.assertIn('UNIQUE-ROW-1:', digest)
        self.assertIn('/tasks/42 for the rest', digest)

    def test_long_status_header_also_fits_with_source_pointer(self):
        row = self.row(1, 0)
        row['payload'] = {'status': 'x' * 8000}
        digest, _ = self.rail._digest([row])
        self.assertLessEqual(len(digest), self.rail.PAYLOAD_CAP)
        self.assertIn('/tasks/42 for the rest', digest)

    def test_batch_split_does_not_shorten_rows_that_fit_alone(self):
        rows = [self.row(1, 2700), self.row(2, 2700)]
        self.rail.dispatch(self.key, rows, self.reg)
        for row, text in zip(rows, self.sent):
            self.assertIn(row['payload']['body'], text)
            self.assertNotIn('cut at', text)

    def test_wrap_up_only_after_last_batch_even_if_close_is_first_row(self):
        self.rail._THREAD_INFO[self.key] = {
            "root": "task:42", "state": "closed", "open_members": 0,
        }
        rows = [self.row(1, 2700), self.row(2, 2700)]
        rows[0]['event_type'] = 'task.updated'
        rows[0]['payload']['status'] = 'done'
        self.assertEqual(self.rail.dispatch(self.key, rows, self.reg), "closing")
        self.assertEqual(self.receipts, [1, 2])
        self.assertNotIn("This thread is done", self.sent[0])
        self.assertIn("This thread is done", self.sent[1])
        self.assertIn("peer marked done", self.sent[1])
        self.assert_receipts_visible()

    def test_pending_native_wake_sent_once_then_continue(self):
        self.reg[self.key]['contract_v'] = ''
        self.reg[self.key]['wake_resumed'] = True
        self.rail.dispatch(self.key, [self.row(1, 2700), self.row(2, 2700)], self.reg)
        self.assertIn("Thread resumed", self.sent[0])
        self.assertNotIn("Thread resumed", self.sent[1])
        self.assertEqual(self.receipts, [1, 2])
        self.assert_receipts_visible()

    def test_claude_stream_uses_same_batch_boundary(self):
        self.rail.STREAM_CMD = ''
        self.rail.RUNTIME = 'claude'
        self.rail.dispatch(self.key, [self.row(1, 2700), self.row(2, 2700)], self.reg)
        self.assertEqual(self.receipts, [1, 2])
        self.assertEqual(len(self.sent), 2)
        self.assert_receipts_visible()

    def test_late_oneshot_overflow_refused_before_receipt_or_launch(self):
        self.rail._THREAD_INFO[self.key] = {
            'root': 'task:42', 'state': 'closed', 'open_members': 0,
        }
        self.reg[self.key]['state'] = 'closed'
        self.rail.holder_alive.return_value = False
        self.rail.start_oneshot = Mock()
        with self.assertRaisesRegex(ValueError, "digest exceeds"):
            self.rail.dispatch(self.key, [self.row(1, 2700), self.row(2, 2700)], self.reg)
        self.assertEqual(self.receipts, [])
        self.rail.start_oneshot.assert_not_called()

    def test_batch_adapter_overflow_refused_before_receipt_or_launch(self):
        self.rail.RUNTIME_STREAMS = False
        self.rail.STREAM_CMD = ''
        self.reg[self.key]['holder'] = 'adapter'
        self.rail.start_adapter_turn = Mock()
        with self.assertRaisesRegex(ValueError, "digest exceeds"):
            self.rail.dispatch(self.key, [self.row(1, 2700), self.row(2, 2700)], self.reg)
        self.assertEqual(self.receipts, [])
        self.rail.start_adapter_turn.assert_not_called()

    def test_closed_binding_with_live_pid_does_not_split_into_two_oneshots(self):
        self.rail._THREAD_INFO[self.key] = {
            'root': 'task:42', 'state': 'closed', 'open_members': 0,
        }
        self.reg[self.key]['state'] = 'closed'
        self.rail.holder_alive.return_value = True
        self.rail.start_oneshot = Mock()
        with self.assertRaisesRegex(ValueError, "digest exceeds"):
            self.rail.dispatch(self.key, [self.row(1, 2700), self.row(2, 2700)], self.reg)
        self.assertEqual(self.receipts, [])
        self.rail.start_oneshot.assert_not_called()

    def test_out_of_order_rows_are_delivered_chronologically(self):
        rows = [self.row(3, 2700), self.row(1, 2700), self.row(2, 2700)]
        self.rail.dispatch(self.key, rows, self.reg)
        self.assertEqual(self.receipts, [1, 2, 3])
        self.assertEqual(len(self.sent), 3)
        for number, text in enumerate(self.sent, 1):
            self.assertIn(f'UNIQUE-ROW-{number}:', text)
        self.assert_receipts_visible()

    def test_tail_only_on_pending_wake_not_following_batches(self):
        self.reg[self.key]['contract_v'] = ''
        rows = [self.row(1, 2700), self.row(2, 2700)]
        for row in rows:
            row['payload']['thread_tail'] = [{'author': 'peer', 'body': 'OLD-TAIL'}]
        self.rail.dispatch(self.key, rows, self.reg)
        self.assertIn('OLD-TAIL', self.sent[0])
        self.assertNotIn('OLD-TAIL', self.sent[1])
        self.assertEqual(self.receipts, [1, 2])

    def test_preexisting_closing_reminder_only_on_last_batch(self):
        self.reg[self.key].update(state='closing', closed_by='peer', close_why='done')
        self.rail.dispatch(self.key, [self.row(1, 2700), self.row(2, 2700)], self.reg)
        self.assertNotIn('This thread is done', self.sent[0])
        self.assertIn('This thread is done', self.sent[1])

    def test_shortened_body_still_has_source_and_receipt(self):
        self.rail.DIGEST_BODY_CAP = 100
        self.rail.dispatch(self.key, [self.row(1, 200)], self.reg)
        self.assertIn('cut at', self.sent[0])
        self.assertIn('/tasks/42 for the rest', self.sent[0])
        self.assertEqual(self.receipts, [1])
        self.assert_receipts_visible()


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
    print('ALL PASSED: digest/receipt correspondence')
