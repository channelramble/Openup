import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ios_timeout import armv6_boot
from ios_timeout.core import ResetError


class Armv6BootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="boot component ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "original"
        self.output = self.root / "patched"
        self.source.write_bytes(b"known decoded test boot component")
        self.patched = b"known patched test boot component"
        self.profile = {
            "input": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "output": hashlib.sha256(self.patched).hexdigest(),
            "flags": ["--rsa", "--debug", "-b", "rd=md0 -v"],
        }

    def fake_patcher(self, code=1, data=None):
        def run(command, **kwargs):
            self.assertEqual(command[-len(self.profile["flags"]):], self.profile["flags"])
            self.assertEqual(Path(command[1]), self.source.resolve())
            Path(command[2]).write_bytes(self.patched if data is None else data)
            return subprocess.CompletedProcess(command, code)
        return run

    def test_known_success_exit_one_requires_exact_output(self):
        with patch.dict(armv6_boot.PROFILES, {"ibss": self.profile}), \
             patch.object(armv6_boot.subprocess, "run", side_effect=self.fake_patcher()):
            armv6_boot.patch_component("ibss", self.root / "patcher", self.source, self.output)
        self.assertEqual(self.output.read_bytes(), self.patched)
        self.assertEqual(self.source.read_bytes(), b"known decoded test boot component")
        self.assertFalse(list(self.root.glob(".3g-boot-*")))

    def test_wrong_original_never_runs_patcher(self):
        with patch.object(armv6_boot.subprocess, "run") as run, self.assertRaises(ResetError):
            armv6_boot.patch_component("ibss", "patcher", self.source, self.output)
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_wrong_output_or_failure_preserves_existing_destination(self):
        self.output.write_bytes(b"previous output")
        for code, data in ((1, b"bad"), (0, b"bad"), (255, self.patched)):
            with self.subTest(code=code, data=data), \
                 patch.dict(armv6_boot.PROFILES, {"ibss": self.profile}), \
                 patch.object(armv6_boot.subprocess, "run", side_effect=self.fake_patcher(code, data)), \
                 self.assertRaises(ResetError):
                armv6_boot.patch_component("ibss", "patcher", self.source, self.output)
            self.assertEqual(self.output.read_bytes(), b"previous output")
            self.assertFalse(list(self.root.glob(".3g-boot-*")))

    def test_missing_output_and_timeout_never_publish(self):
        results = (
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1),
            subprocess.TimeoutExpired("patcher", 60),
        )
        for result in results:
            with patch.dict(armv6_boot.PROFILES, {"ibss": self.profile}), \
                 patch.object(armv6_boot.subprocess, "run", side_effect=result), \
                 self.assertRaises((OSError, ResetError, subprocess.TimeoutExpired)):
                armv6_boot.patch_component("ibss", "patcher", self.source, self.output)
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.root.glob(".3g-boot-*")))

    def test_cannot_overwrite_original(self):
        with patch.object(armv6_boot.subprocess, "run") as run, self.assertRaises(ResetError):
            armv6_boot.patch_component("ibss", "patcher", self.source, self.source)
        run.assert_not_called()

    def test_both_bootloaders_replace_stock_reformat_boot_arguments(self):
        for profile in armv6_boot.PROFILES.values():
            self.assertIn("-b", profile["flags"])
            arguments = profile["flags"][profile["flags"].index("-b") + 1]
            self.assertIn("rd=md0", arguments)
            self.assertNotIn("nand-enable-reformat", arguments)
