"""The tool shell re-establishes helper discovery without changing identity."""
from pathlib import Path
import subprocess
import tempfile
import unittest


INIT = Path(__file__).with_name("terminal-init.sh")


class TerminalInitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.helpers = self.root / "helpers with spaces"
        self.helpers.mkdir()
        helper = self.helpers / "sc-say"
        helper.write_text("#!/bin/sh\nprintf 'fixture-helper'\n")
        helper.chmod(0o700)

    def shell(self, helper_dir=None, path="/usr/bin:/bin", command='printf "%s" "$PATH"'):
        env = {"PATH": path, "HOME": str(self.root / "profile"),
               "SAMCLOUD_AUTH_HDR": str(self.root / "seat-auth.hdr"),
               "SAMCLOUD_SESSION_ID": "owned-plane-conversation"}
        if helper_dir is not None:
            env["SAMCLOUD_HELPER_DIR"] = str(helper_dir)
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; . "$1"; ' + command,
             "terminal-init-test", str(INIT)], env=env, text=True,
            capture_output=True, timeout=5, check=True)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_reset_tool_path_can_find_helper(self):
        self.assertEqual(self.shell(self.helpers, command="sc-say"), "fixture-helper")

    def test_repeated_source_adds_directory_once(self):
        self.assertEqual(self.shell(self.helpers), f"{self.helpers}:/usr/bin:/bin")

    def test_existing_entry_is_preserved(self):
        path = f"/usr/bin:{self.helpers}:/bin"
        self.assertEqual(self.shell(self.helpers, path=path), path)

    def test_absent_empty_or_missing_directory_leaves_path_unchanged(self):
        for directory in (None, "", self.root / "missing"):
            with self.subTest(directory=directory):
                self.assertEqual(self.shell(directory), "/usr/bin:/bin")

    def test_similar_prefix_is_not_a_path_entry(self):
        path = f"{self.helpers}-other:/usr/bin:/bin"
        self.assertEqual(self.shell(self.helpers, path=path), f"{self.helpers}:{path}")

    def test_does_not_change_profile_credential_path_or_conversation(self):
        output = self.shell(self.helpers, command=
                            'printf "%s\\n" "$HOME" "$SAMCLOUD_AUTH_HDR" "$SAMCLOUD_SESSION_ID"')
        self.assertEqual(output.splitlines(), [str(self.root / "profile"),
                         str(self.root / "seat-auth.hdr"), "owned-plane-conversation"])


if __name__ == "__main__":
    unittest.main()
