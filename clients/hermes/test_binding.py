"""Client mapping checks, with independent processes for writer ownership."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from binding import Binding, BindingError


class BindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.sid = str(uuid.uuid4())

    def test_new_then_resume_exact_native_id(self):
        with Binding(self.home, self.sid, resume=False) as binding:
            self.assertIsNone(binding.stored_id)
            binding.save("native-older-not-latest")
            self.assertEqual(binding.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(binding.directory.stat().st_mode & 0o777, 0o700)
        with Binding(self.home, self.sid, resume=True) as binding:
            self.assertEqual(binding.stored_id, "native-older-not-latest")
            binding.save("native-resolved-compression-tip")
        with Binding(self.home, self.sid, resume=True) as binding:
            self.assertEqual(binding.stored_id, "native-resolved-compression-tip")

    def test_missing_resume_does_not_create_mapping(self):
        with self.assertRaises(BindingError):
            with Binding(self.home, self.sid, resume=True):
                self.fail("missing mapping accepted")
        self.assertFalse(list(self.home.rglob("*.json")))

    def test_create_does_not_overwrite_existing(self):
        with Binding(self.home, self.sid, resume=False) as binding:
            binding.save("original")
        with self.assertRaises(BindingError):
            with Binding(self.home, self.sid, resume=False):
                self.fail("existing mapping replaced")

    def test_corrupt_or_wrong_profile_mapping_is_not_unknown(self):
        with Binding(self.home, self.sid, resume=False) as binding:
            binding.save("native")
            path = binding.path
            valid = json.loads(path.read_text())
        invalid = ["{", "[]", json.dumps({**valid, "profile_home": "/another/profile"}),
                   json.dumps({**valid, "plane_id": str(uuid.uuid4())}),
                   json.dumps({**valid, "version": True}),
                   json.dumps({**valid, "stored_id": None})]
        for contents in invalid:
            with self.subTest(contents=contents):
                path.write_text(contents)
                with self.assertRaises(BindingError):
                    with Binding(self.home, self.sid, resume=True):
                        self.fail("corrupt mapping accepted")

    def test_profile_and_conversation_isolation(self):
        other = self.home / "operator"
        other.mkdir()
        with Binding(self.home, self.sid, resume=False) as first:
            first.save("rail")
            with Binding(other, self.sid, resume=False) as second:
                second.save("operator")
            with Binding(self.home, str(uuid.uuid4()), resume=False) as sibling:
                sibling.save("sibling")
        with Binding(other, self.sid, resume=True) as second:
            self.assertEqual(second.stored_id, "operator")

    def test_concurrent_process_refused_then_lock_released(self):
        code = "from binding import Binding; import sys;\nwith Binding(sys.argv[1],sys.argv[2],resume=True): print('owned')"
        def child():
            return subprocess.run([sys.executable, "-c", code, str(self.home), self.sid],
                                  cwd=Path(__file__).parent, capture_output=True, text=True)
        with Binding(self.home, self.sid, resume=False) as binding:
            binding.save("native")
            refused = child()
            self.assertNotEqual(refused.returncode, 0)
            self.assertNotIn("owned", refused.stdout)
        accepted = child()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(accepted.stdout.strip(), "owned")

    def test_failed_replace_keeps_previous_mapping(self):
        with Binding(self.home, self.sid, resume=False) as binding:
            binding.save("before")
            with patch("binding.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    binding.save("after")
            self.assertEqual(binding.stored_id, "before")
            self.assertFalse(list(binding.directory.glob(".mapping-*")))
        with Binding(self.home, self.sid, resume=True) as binding:
            self.assertEqual(binding.stored_id, "before")

    def test_invalid_path_id_refused(self):
        for sid in ("../other", "", None, self.sid.upper()):
            with self.subTest(sid=sid), self.assertRaises(BindingError):
                Binding(self.home, sid, resume=False)

    def test_unlocked_save_refused(self):
        binding = Binding(self.home, self.sid, resume=False)
        with self.assertRaises(BindingError):
            binding.save("native")
        with binding:
            binding.save("native")
        with self.assertRaises(BindingError):
            binding.save("replacement")


if __name__ == "__main__":
    unittest.main()
