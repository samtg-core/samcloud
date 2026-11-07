"""Framing coverage from task827, with explicit partial-contribution attribution.

Hermes-codex authored the multiple-envelope and invalid-UTF8 cases during its
controlled stock-v0.21.6 turns. Codex reviewed/mutation-checked those cases,
adapted their imports/formatting, supplied the fragmented-envelope case and
strengthened the partial-EOF assertions. The model's own full submission failed;
this completed suite is collaborative work, not unattended-model acceptance.
"""
import json
import unittest
from unittest.mock import MagicMock, patch

from native import ProtocolError
from stream import input_lines, message_text


class InputFramingTest(unittest.TestCase):
    def test_two_envelopes_in_one_read(self):
        # Hermes-codex: escaped newline must not split the first JSON envelope.
        envelope1 = {"type": "user", "message": {"role": "user", "content": "Hello\nWorld"}}
        envelope2 = {"type": "user", "message": {"role": "user", "content": "Second"}}
        combined = ((json.dumps(envelope1, ensure_ascii=False) + "\n") +
                    (json.dumps(envelope2, ensure_ascii=False) + "\n")).encode("utf-8")
        with patch("stream.os.read", side_effect=[combined, b""]), \
                patch("stream.select.select", return_value=([0], [], [])):
            result = list(input_lines(0, MagicMock()))
        self.assertEqual(len(result), 2)
        self.assertEqual(json.loads(result[0])["message"]["content"], "Hello\nWorld")
        self.assertEqual(json.loads(result[1])["message"]["content"], "Second")

    def test_invalid_utf8_raises_unicode_decode_error(self):
        # Hermes-codex: newline completes the frame, so the decoder must reject.
        invalid_utf8_bytes = b"\xff\xfe\xfd\x0a"
        with patch("stream.os.read", side_effect=[invalid_utf8_bytes, b""]), \
                patch("stream.select.select", return_value=([0], [], [])):
            with self.assertRaises(UnicodeDecodeError):
                list(input_lines(0, MagicMock()))

    def test_fragmented_utf8_envelope(self):
        # Codex: fragment a real newline-delimited envelope inside the euro sign.
        content = "Before €\nafter"
        envelope = {"type": "user", "message": {"role": "user", "content": content}}
        wire = (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")
        split = wire.index("€".encode("utf-8")) + 1
        with patch("stream.os.read", side_effect=[wire[:split], wire[split:], b""]) as read, \
                patch("stream.select.select", return_value=([0], [], [])):
            result = list(input_lines(0, MagicMock()))
        self.assertGreaterEqual(read.call_count, 2)
        self.assertEqual(result, [wire[:-1].decode("utf-8")])
        self.assertEqual(message_text(json.loads(result[0])), content)

    def test_complete_frame_then_partial_eof(self):
        # Codex: preserve the first frame exactly; do not silently emit the tail.
        envelope = {"type": "user", "message": {"role": "user", "content": "complete"}}
        complete = json.dumps(envelope)
        chunks = [(complete + "\n" + '{"type":"user"').encode(), b""]
        with patch("stream.os.read", side_effect=chunks), \
                patch("stream.select.select", return_value=([0], [], [])):
            frames = input_lines(0, MagicMock())
            first = next(frames)
            self.assertEqual(first, complete)
            self.assertEqual(message_text(json.loads(first)), "complete")
            with self.assertRaisesRegex(ProtocolError, "Incomplete input envelope at EOF"):
                next(frames)


if __name__ == "__main__":
    unittest.main()
