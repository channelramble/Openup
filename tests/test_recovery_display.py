"""Run the on-phone display shell with fake output, never a recovery binary."""

import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
DISPLAY = ROOT / "ios_timeout/data/recovery-display.sh"


@unittest.skipIf(os.name == "nt", "The ramdisk uses POSIX sh; Windows uses WSL")
class RecoveryDisplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="openup-display-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.engine = self.root / "fake engine"
        self.wrapper = self.root / "display.sh"
        self.wrapper.write_text(DISPLAY.read_text().replace(
            "/usr/bin/openup-recovery-engine", shlex.quote(str(self.engine))))

    def run_engine(self, body, arguments=(), stdin=""):
        self.engine.write_text("#!/bin/sh\n" + body + "\n")
        self.engine.chmod(0o700)
        # Only shell builtins may be used by either the wrapper or fixture.
        return subprocess.run(
            ["/bin/sh", str(self.wrapper), *arguments], input=stdin,
            env={"PATH": str(self.root / "no-tools")},
            capture_output=True, text=True, timeout=5, cwd=self.root,
        )

    def run_output(self, output, status=0):
        return self.run_engine("printf '%s' " + shlex.quote(output) + "\nexit " + str(status))

    def assert_no_recovered_result(self, result):
        self.assertNotIn("Recovered passcode:\n", result.stdout)
        self.assertNotIn("__OPENUP_RECOVERY_EXIT__", result.stdout)

    def test_passcode_is_last_after_stdout_and_stderr_diagnostics(self):
        for passcode in ("0000", "0042", "001234"):
            with self.subTest(passcode=passcode):
                result = self.run_engine(
                    "printf 'Found passcode : %s\\n' " + shlex.quote(passcode)
                    + "\ni=0; while [ $i -lt 100 ]; do "
                    "printf 'Keybag diagnostic details %s\\n' \"$i\"; i=$((i + 1)); done\n"
                    "printf 'Writing results to device.plist\\n' >&2"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertTrue(result.stdout.endswith("Recovered passcode:\n" + passcode + "\n"))
                # A short final block stays readable even in a tiny viewport.
                for columns in (20, 40):
                    rows = [row for line in result.stdout.splitlines()
                            for row in (textwrap.wrap(line, columns) or [""])]
                    self.assertEqual(rows[-2:], ["Recovered passcode:", passcode])
                self.assertNotIn("__OPENUP_RECOVERY_EXIT__", result.stdout)

    def test_last_attempt_is_not_a_recovered_passcode(self):
        result = self.run_output("0000\n0001\n9999\n")
        self.assert_no_recovered_result(result)
        self.assertTrue(result.stdout.endswith("No passcode was recovered.\n"))

    def test_explicit_no_passcode_has_a_distinct_result(self):
        result = self.run_output("No passcode set\nWriting results to device.plist\n")
        self.assertEqual(result.returncode, 0)
        self.assert_no_recovered_result(result)
        self.assertTrue(result.stdout.endswith("No passcode is set.\n"))

    def test_failed_validation_cannot_be_reported_as_success_even_on_exit_zero(self):
        for error in ("Invalid passcode !", "FAIL: keybag", "FAILed to open service"):
            with self.subTest(error=error):
                result = self.run_output("Found passcode : 0042\n" + error + "\n")
                self.assert_no_recovered_result(result)
                self.assertEqual(result.returncode, 1)
                self.assertTrue(result.stdout.endswith("No confirmed passcode.\n"))

    def test_nonzero_engine_status_is_preserved_without_success(self):
        for status in (1, 127, 139, 255):
            with self.subTest(status=status):
                result = self.run_output("Found passcode : 0042\n", status)
                self.assertEqual(result.returncode, status)
                self.assert_no_recovered_result(result)
                self.assertTrue(result.stdout.endswith("No confirmed passcode.\n"))

    def test_missing_engine_is_a_failure(self):
        result = subprocess.run(["/bin/sh", str(self.wrapper)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 127)
        self.assert_no_recovered_result(result)

    def test_ambiguous_or_unprintable_results_are_not_confirmed(self):
        for output in ("Found passcode : \n", "Found passcode : 00\t42\n",
                       "Found passcode : " + "1" * 129 + "\n",
                       "Found passcode : 0042\nFound passcode : 0043\n",
                       "Found passcode : 0042\nNo passcode set\n"):
            with self.subTest(output=output):
                result = self.run_output(output)
                self.assertEqual(result.returncode, 1)
                self.assert_no_recovered_result(result)

    def test_identical_reports_and_crlf_keep_leading_zeros(self):
        result = self.run_output("Found passcode : 0042\r\nFound passcode : 0042\r\n")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith("Recovered passcode:\n0042\n"))

    def test_literal_password_is_never_evaluated_as_shell_or_printf_code(self):
        passcode = "$(touch injected);`touch injected` %s \\"
        result = self.run_output("Found passcode : " + passcode + "\n")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith("Recovered passcode:\n" + passcode + "\n"))
        self.assertFalse((self.root / "injected").exists())

    def test_arguments_and_stdin_reach_the_engine_unchanged(self):
        result = self.run_engine(
            "[ \"$#\" -eq 3 ] && [ \"$1\" = '-u' ] && [ \"$2\" = 'two words' ] "
            "&& [ \"$3\" = '*' ] || exit 90\n"
            "IFS= read -r answer\n[ \"$answer\" = '0042' ] || exit 91\n"
            "printf 'Found passcode : %s\\n' \"$answer\"",
            arguments=("-u", "two words", "*"), stdin="0042\n",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.endswith("Recovered passcode:\n0042\n"))

    def test_unterminated_final_engine_output_is_separated_from_summary(self):
        result = self.run_output("Found passcode : 0042\nWriting results to device.plist")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith("\nRecovered passcode:\n0042\n"))

    def test_incomplete_stream_does_not_confirm_a_passcode(self):
        function = DISPLAY.read_text().split('\n{\n    /usr/bin/openup-recovery-engine')[0]
        for tail in ("", "__OPENUP_RECOVERY_EXIT__:999\n",
                     "__OPENUP_RECOVERY_EXIT__:0\nmore output\n"):
            with self.subTest(tail=tail):
                result = subprocess.run(
                    ["/bin/sh", "-c", function + "\nshow_recovery_result"],
                    input="Found passcode : 0042\n" + tail,
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 125)
                self.assert_no_recovered_result(result)

    def test_completed_startup_holds_quietly_after_the_result(self):
        startup = (ROOT / "ios_timeout/data/iphone3g-setup.sh").read_text()
        self.assertNotIn("Recovery process finished with status", startup)
        self.assertTrue(startup.rstrip().endswith('wait "$daemon_pid"\nhold_ramdisk quiet'))
        hold = re.search(r"^hold_ramdisk\(\) \{.*?^\}", startup,
                         re.MULTILINE | re.DOTALL).group(0)
        # Simulate an unavailable wait helper and stop before SIGSTOP, keeping
        # the real hold's message selection. No device process is involved.
        hold = hold.replace("/usr/local/bin/bl-ios-wait", str(self.root / "missing-helper"))
        hold = hold.replace('kill -STOP "$$"', "exit 0")
        for argument, expected in (("quiet", ""), ("", "Recovery stopped.")):
            result = subprocess.run(["/bin/sh", "-c", hold + "\nhold_ramdisk " + argument],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            if expected:
                self.assertIn(expected, result.stdout)
            else:
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
