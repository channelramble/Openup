"""Exercise the bundled wait helper as host C, never as an iPhone executable."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party/iphone-dataprotection/source/ramdisk_tools/restored_external.c"
STARTUP = ROOT / "ios_timeout/data/iphone3g-setup.sh"
COMPILER = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")


def helper_source():
    source = SOURCE.read_text()
    match = re.search(
        r"^static\s+int\s+sleep_command\s*\(\s*int\s+argc\s*,\s*"
        r"char\s*\*\s*argv\s*\[\s*\]\s*\)\s*\{.*?^\}",
        source, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("Could not locate the exact bundled sleep_command helper")
    return match.group(0)


@unittest.skipUnless(COMPILER, "A native C compiler is required for the helper test")
class Armv6WaitHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(cls.temporary.name)
        cls.program = root / ("wait-helper.exe" if os.name == "nt" else "wait-helper")
        harness = root / "wait-helper.c"
        # The helper is copied verbatim from the shipped source. Only sleep is
        # substituted, so validation and interrupted-wait logic execute exactly.
        harness.write_text(
            "#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n"
            "#include <limits.h>\n"
            "static unsigned int calls;\n"
            "static unsigned int mock_sleep(unsigned int seconds) {\n"
            "    if (++calls > 8) abort();\n"
            "    printf(\"sleep:%u\\n\", seconds);\n"
            "    if (getenv(\"BL_WAIT_TEST_INTERRUPT\") && seconds > 1) return seconds - 1;\n"
            "    return 0;\n"
            "}\n"
            "#define sleep mock_sleep\n"
            + helper_source()
            + r'''
int main(int argc, char* argv[]) {
    /* Feed exact UTF-8 bytes without the host CRT's argv conversion. */
    if (argc == 2 && !strcmp(argv[1], "--test-value-from-stdin")) {
        char value[4096];
        size_t size = fread(value, 1, sizeof(value) - 1, stdin);
        if (ferror(stdin) || size == sizeof(value) - 1) return 99;
        value[size] = '\0';
        char *helper_argv[] = {argv[0], "--sleep", value};
        return sleep_command(3, helper_argv);
    }
    return sleep_command(argc, argv);
}
'''
        )
        completed = subprocess.run(
            [COMPILER, "-std=c99", "-Wall", "-Wextra", str(harness), "-o", str(cls.program)],
            capture_output=True, text=True, timeout=30,
        )
        if completed.returncode:
            raise AssertionError("Wait helper compilation failed:\n" + completed.stderr)

    def run_helper(self, arguments, interrupted=False):
        environment = dict(os.environ)
        environment.pop("BL_WAIT_TEST_INTERRUPT", None)
        if interrupted:
            environment["BL_WAIT_TEST_INTERRUPT"] = "1"
        value = None
        if len(arguments) == 2 and arguments[0] == "--sleep":
            value = arguments[1]
            arguments = ["--test-value-from-stdin"]
        return subprocess.run(
            [str(self.program), *arguments], env=environment,
            input=value, capture_output=True, text=True, encoding="utf-8", timeout=5,
        )

    def test_zero_delay_succeeds_without_calling_sleep(self):
        result = self.run_helper(["--sleep", "0"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_decimal_limits_are_accepted_without_real_waiting(self):
        for value, expected in (("1", 1), ("60", 60), ("3600", 3600), ("0007", 7)):
            with self.subTest(value=value):
                result = self.run_helper(["--sleep", value])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "sleep:%d\n" % expected)

    def test_invalid_values_and_overflow_do_not_call_sleep(self):
        for value in (
            "", "-1", "+1", " 1", "1 ", "1\n", "\t1", "1.0", "0x10", "1e2",
            "3601", "4294967296", "18446744073709551616", "9" * 200,
            "١", "１", "nan",
        ):
            with self.subTest(value=value):
                result = self.run_helper(["--sleep", value])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "", "Invalid input reached sleep()")

    def test_missing_and_extra_arguments_are_rejected(self):
        for arguments in ([], ["--sleep"], ["--sleep", "0", "extra"], ["--other", "1"]):
            with self.subTest(arguments=arguments):
                result = self.run_helper(arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_interrupted_wait_resumes_remaining_seconds(self):
        result = self.run_helper(["--sleep", "3"], interrupted=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["sleep:3", "sleep:2", "sleep:1"])


class Armv6WaitIntegrationTests(unittest.TestCase):
    def test_sleep_dispatch_precedes_hardware_initialization(self):
        source = SOURCE.read_text()
        body = source[source.index("int main("):]
        dispatch = body.index("return sleep_command(argc, argv);")
        for operation in ("get_device_infos(", "init_tcp(", 'IOServiceMatching("IOWatchDogTimer")'):
            self.assertLess(dispatch, body.index(operation))
        self.assertIn('"--sleep"', body[:dispatch])

    def test_startup_uses_bundled_wait_helper(self):
        startup = STARTUP.read_text()
        self.assertIn("/usr/local/bin/bl-ios-wait --sleep", startup)
        self.assertNotIn("restored_external.sshrd --sleep", startup)
        self.assertNotRegex(startup, r"(?m)(?:^|[;\s])sleep\s+[0-9]")
        self.assertIn("--sleep 1", startup)
        self.assertIn("--sleep 60", startup)
        self.assertIn('kill -STOP "$$"', startup)


if __name__ == "__main__":
    unittest.main()
