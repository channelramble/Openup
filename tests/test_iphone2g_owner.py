"""Execute the actual portable C service owner with an inert SSH fixture."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "Requires a POSIX C compiler")
class OriginalIPhoneOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="openup owner fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = Path(__file__).resolve().parents[1] / "third_party/iphone2g-helper/ssh-owner.c"
        fake = self.root / "sshd"
        fake.write_text("#!" + sys.executable + "\nimport os,sys,signal,json\n"
                        "with open(os.environ['OWNER_READY'],'w') as f: json.dump({'pid':os.getpid(),'args':sys.argv[1:]},f)\n"
                        "if os.environ.get('OWNER_HOLD'): signal.pause()\n"
                        "sys.exit(int(os.environ.get('OWNER_EXIT','0')))\n")
        fake.chmod(0o700)
        main = self.root / "main.c"
        main.write_text("int run_sshd(void); int main(void) { return run_sshd(); }\n")
        self.program = self.root / "owner"
        subprocess.run([shutil.which("cc"), "-Wall", "-Wextra", "-Werror",
                        "-DOPENUP_SSHD_PATH=" + json.dumps(str(fake)), str(source), str(main),
                        "-o", str(self.program)], check=True, capture_output=True, timeout=30)
        self.ready = self.root / "ready.json"
        self.env = {**os.environ, "OWNER_READY": str(self.ready)}

    def test_foreground_arguments_and_ssh_exit_status_are_preserved(self):
        result = subprocess.run([str(self.program)], env={**self.env, "OWNER_EXIT":"71"}, timeout=5)
        self.assertEqual(result.returncode, 71)
        self.assertEqual(json.loads(self.ready.read_text())["args"], ["-D", "-e"])

    def test_usb_owner_stays_alive_and_reaps_ssh_when_stopped(self):
        process = subprocess.Popen([str(self.program)], env={**self.env, "OWNER_HOLD":"1"}, start_new_session=True)
        try:
            deadline = time.monotonic() + 3
            while not self.ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.ready.exists())
            child = json.loads(self.ready.read_text())["pid"]
            self.assertIsNone(process.poll(), "USB owner exited while SSH was still running")
            process.terminate()
            self.assertEqual(process.wait(timeout=3), 128 + signal.SIGTERM)
            with self.assertRaises(ProcessLookupError): os.kill(child, 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
