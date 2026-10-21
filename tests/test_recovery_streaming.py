"""Exercise the shipped stdio setup with a harmless host C output fixture."""

import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party/iphone-dataprotection/source/ramdisk_tools/systemkb_bruteforce.c"
DISPLAY = ROOT / "ios_timeout/data/recovery-display.sh"
COMPILER = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")


def console_helper():
    match = re.search(
        r"^static void configure_console_output\(void\)\s*\{.*?^\}",
        SOURCE.read_text(), re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("Could not locate the shipped console output helper")
    return match.group(0)


class RecoveryConsoleEntryTests(unittest.TestCase):
    def test_console_setup_is_first_in_main(self):
        # It must precede diagnostics, prompts and hardware initialization.
        self.assertRegex(SOURCE.read_text(),
                         r"int main\(int argc, char\* argv\[\]\)\s*\{\s*"
                         r"configure_console_output\(\);")


@unittest.skipUnless(os.name != "nt" and COMPILER,
                     "Requires a host C compiler and the ramdisk's POSIX shell")
class RecoveryStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="openup-stream-")
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        program = root / "fake engine"
        harness = root / "output.c"
        # Copy only the real stdio helper; no recovery or device code executes.
        # The stderr checkpoint distinguishes buffering from a slow producer.
        # A raw stdin read keeps the producer alive until the test releases it.
        harness.write_text(
            "#include <stdio.h>\n#include <unistd.h>\n" + console_helper() + r'''
int main(int argc, char **argv) {
    char release;
    (void)argv;
    if (argc == 1) configure_console_output();
    printf("Progress from running engine\n");
    fprintf(stderr, "Output checkpoint\n");
    if (read(STDIN_FILENO, &release, 1) != 1) return 90;
    printf("Found passcode : 0042\n");
    fprintf(stderr, "Writing results to fixture.plist\n");
    return 0;
}
''')
        subprocess.run([COMPILER, "-std=c99", "-Wall", "-Wextra", "-Werror",
                        str(harness), "-o", str(program)],
                       capture_output=True, text=True, check=True, timeout=30)
        cls.wrapper = root / "display.sh"
        cls.wrapper.write_text(DISPLAY.read_text().replace(
            "/usr/bin/openup-recovery-engine", shlex.quote(str(program))))

    def output_before_exit(self, buffered_control=False):
        arguments = ["buffered-control"] if buffered_control else []
        process = subprocess.Popen(
            ["/bin/sh", str(self.wrapper), *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        early = b""
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 5
                while b"Output checkpoint\n" not in early:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, "No live output checkpoint")
                    self.assertTrue(selector.select(remaining), "No live output checkpoint")
                    chunk = os.read(process.stdout.fileno(), 4096)
                    self.assertTrue(chunk, "Engine exited before its checkpoint")
                    early += chunk
            self.assertIsNone(process.poll(), "Progress must arrive before engine exit")
        finally:
            # Always release the producer, including on assertion failure.
            try:
                tail, errors = process.communicate(input=b"\n", timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
                raise
        self.assertEqual(process.returncode, 0, errors.decode())
        self.assertEqual(errors, b"")
        self.assertTrue((early + tail).endswith(b"Recovered passcode:\n0042\n"))
        return early, tail

    def test_progress_reaches_display_before_engine_exits(self):
        early, _ = self.output_before_exit()
        self.assertEqual(early, b"Progress from running engine\nOutput checkpoint\n")

    def test_control_reproduces_old_buffered_output(self):
        early, tail = self.output_before_exit(buffered_control=True)
        self.assertEqual(early, b"Output checkpoint\n")
        self.assertIn(b"Progress from running engine\n", tail)


if __name__ == "__main__":
    unittest.main()
