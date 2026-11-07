#!/usr/bin/env python3
"""Execute the installer's real charter block in isolated homes, without services."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "registry/helpers/wake-install.sh"


class NativeCharterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="samcloud-charter-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        # Run production code, not a duplicate renderer. Auth/preflight/supervision
        # are deliberately outside this unit's scope.
        source = INSTALLER.read_text()
        start = source.index('GT_VERSION="')
        end = source.index("# ── preflight:", start)
        self.script = "set -eu\n" + source[start:end]

    def install(self, runtime="hermes", stream="", cwd=None):
        env = dict(os.environ, HOME=str(self.home), RAIL_RUNTIME=runtime,
                   RAIL_CWD=str(cwd or self.home), WHO="test-seat",
                   RAIL_STREAM_CMD=stream)
        env.pop("RAIL_GROUND_TRUTH", None)
        result = subprocess.run(["bash", "-c", self.script], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        target = ((cwd / "CLAUDE.md" if cwd else self.home / ".claude/CLAUDE.md")
                  if runtime == "claude" else (cwd or self.home) / "AGENTS.md")
        return target.read_text()

    def assert_modes(self, text):
        self.assertIn("native streaming client (`RAIL_STREAM_CMD`)", text)
        self.assertIn("reply using `sc-say`", text)
        self.assertIn("NOT posted automatically", text)
        self.assertIn("Without that client, the batch adapter posts", text)
        self.assertNotIn("Answer them — what you say is posted", text)

    def test_native_and_batch_copy_are_explicit(self):
        for runtime, stream in (("hermes", "/client"), ("hermes", ""),
                                ("codex", "/client")):
            with self.subTest(runtime=runtime, stream=stream):
                self.assert_modes(self.install(runtime, stream))

    def test_upgrade_preserves_appended_notes(self):
        for version in (7, 8):
            with self.subTest(version=version):
                target = self.home / "AGENTS.md"
                target.write_text(
                    f"<!-- samcloud:ground-truth v{version} -->\n"
                    "Old unconditional auto-posting promise\n"
                    "(admins may append charter sections below this block)\n"
                    "## Local notes\nKeep the operator profile separate.\n")
                text = self.install(stream="/client")
                self.assertIn("samcloud:ground-truth v9", text)
                self.assert_modes(text)
                self.assertNotIn("Old unconditional", text)
                self.assertTrue(text.endswith("## Local notes\nKeep the operator profile separate.\n"))

    def test_mode_switch_and_rerun_remain_truthful_without_a_stamp_change(self):
        batch = self.install()
        self.assert_modes(batch)
        native = self.install(stream="/client")
        self.assertEqual(batch, native)
        self.assert_modes(native)
        self.assertEqual(self.install(), native)

    def test_hand_authored_ground_truth_is_untouched(self):
        text = "# My own instructions\nDo not replace me.\n"
        (self.home / "AGENTS.md").write_text(text)
        self.assertEqual(self.install(stream="/client"), text)

    def test_claude_path_still_uses_sc_say(self):
        text = self.install("claude")
        self.assertIn("Skill(samcloud-agent)", text)
        self.assertIn("`sc-say` replies", text)
        self.assertNotIn("batch adapter", text)
        self.assertNotIn("posted automatically", text)

    def test_dedicated_cwd_does_not_touch_operator_home(self):
        cwd = self.home / "holder"
        cwd.mkdir()
        self.assert_modes(self.install(cwd=cwd, stream="/client"))
        self.assertFalse((self.home / "AGENTS.md").exists())


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    if not result.wasSuccessful():
        sys.exit(1)
    print("ALL PASSED")
