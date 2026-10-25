import os
import shlex
import subprocess
import tempfile
import unittest
from importlib import resources
from pathlib import Path


@unittest.skipUnless(os.name == "posix", "Phone startup requires POSIX; Windows runs the backend in WSL")
class OriginalIPhoneStartupTests(unittest.TestCase):
    def run_startup(self, helper_status=0):
        source = resources.files("ios_timeout").joinpath("data/iphone2g-rc.boot").read_text()
        with tempfile.TemporaryDirectory(prefix="openup startup fixture ") as directory:
            root = Path(directory)
            log = root / "calls"
            helper = root / "helper"
            helper.write_text('#!/bin/sh\nprintf "helper %s\\n" "$*" >> "$FIXTURE_LOG"\nexit "$HELPER_STATUS"\n')
            helper.chmod(0o700)
            # Execute the real startup text, replacing only the native helper
            # with an inert fixture. There are no device operations.
            self.assertEqual(source.count("/usr/local/bin/openup-iphone2g"), 1)
            source = source.replace("/usr/local/bin/openup-iphone2g", shlex.quote(helper.as_posix()))
            script = root / "rc.boot"
            script.write_text(source)
            result = subprocess.run(["bash", script.as_posix()], capture_output=True, text=True,
                                    env={**os.environ, "FIXTURE_LOG": log.as_posix(),
                                         "HELPER_STATUS": str(helper_status)},
                                    timeout=5)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_startup_delegates_only_to_the_usb_owner_helper(self):
        result, calls = self.run_startup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["helper --bootstrap"])
        self.assertEqual(result.stdout, "")

    def test_failed_bootstrap_does_not_fall_back_to_stock_restore_services(self):
        result, calls = self.run_startup(helper_status=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["helper --bootstrap"])

    def test_helper_service_exit_status_is_preserved(self):
        result, calls = self.run_startup(helper_status=71)
        self.assertEqual(result.returncode, 71)
        self.assertEqual(calls, ["helper --bootstrap"])


if __name__ == "__main__":
    unittest.main()
