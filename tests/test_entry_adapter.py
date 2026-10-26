"""Exercise the pinned shell glue without accessing a physical USB device."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from ios_timeout import legacy_kit, upstream


BASH = shutil.which("bash")
FIXTURES = (os.environ.get("BL_IOS_UPSTREAM_SCRIPT"), os.environ.get("BL_IOS_LEGACY_SCRIPT"))


@unittest.skipUnless(all(FIXTURES) and BASH and os.name != "nt", "Pinned POSIX backend fixtures required")
class EntryAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Both adapters verify their complete upstream script hash first.
        cls.sources = {
            "recover": upstream.adapt_script(Path(FIXTURES[0]).read_bytes()).decode(),
            "legacy": legacy_kit.adapt_menu_only(Path(FIXTURES[1]).read_bytes()).decode(),
        }

    def function(self, backend, name):
        found = re.findall(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}",
                           self.sources[backend], re.M | re.S)
        self.assertEqual(len(found), 1, name)
        return found[0]

    def make_python_tool(self, path, source):
        program = path.with_suffix(".py")
        program.write_text(source)
        path.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -B "
                        + shlex.quote(str(program)) + ' "$@"\n')
        path.chmod(0o700)

    def test_first_managed_ecid_survives_stock_recovery_metadata_reads(self):
        """Skip stock identity reads and reject a changed phone despite a stale variable."""
        for backend in self.sources:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(prefix="entry-continuity-") as temporary:
                root = Path(temporary)
                tools = root / "tools"
                tools.mkdir()
                trace = root / "trace.jsonl"
                managed = tools / "managed-python"
                self.make_python_tool(managed, '''import json,os,sys
with open(os.environ["TRACE"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[3] == "detect":
    print("Recovery\\tiPhone5,1\\tn41\\t123\\tUnknown\\t-")
else:
    from ios_timeout import backend_entry
    sys.argv = ["entry"] + sys.argv[3:]
    raise SystemExit(backend_entry.main())
''')
                self.make_python_tool(tools / "irecovery", '''import json,os,sys
with open(os.environ["TRACE"] + ".queries", "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
print("MODE: Recovery\\nPRODUCT: iPhone5,1\\nMODEL: n41ap\\nCPID: 8950\\nBDID: 00\\nECID: 1c8\\nSRNM: fixture")
''')
                # The Legacy guard checks this file exists, but managed normal
                # detection must not execute an unselected stock info query.
                (tools / "ideviceinfo").write_text("not executed")
                detect = self.function(backend, "device_get_info")
                if backend == "legacy":
                    # Stop before firmware/cache setup, after the real entry gate.
                    detect = detect[:detect.index('    device_fw_dir="../saved/firmware/$device_type"')] + "\n}\n"
                # Managed detection now skips stock metadata. Independently
                # emulate a stale stock assignment at the verification boundary
                # to ensure it cannot replace the separately captured ECID.
                self.assertEqual(detect.count("    bl_ios_verify_entry\n"), 1)
                detect = detect.replace("    bl_ios_verify_entry\n",
                                        "    device_ecid=456\n    bl_ios_verify_entry\n")
                shell = "\n".join((self.function(backend, "bl_ios_entry"),
                                    self.function(backend, "bl_ios_verify_entry"), detect)) + r'''
log() { :; }
print() { :; }
warn() { :; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 73; }
device_get_name() { device_name=fixture; }
device_manufacturing() { :; }
device_entry() { echo UNEXPECTED_MANUAL_ENTRY; exit 91; }
device_proc=6
device_argmode=""
device_type=iPhone5,1
main_argmode=device_enter_ramdisk
platform=macos
ideviceinfo="$dir/ideviceinfo"
irecovery="$dir/irecovery"
device_get_info
echo UNEXPECTED_ENTRY_ACCEPTED
'''
                environment = {**os.environ, "TRACE": str(trace), "dir": str(tools),
                               "BL_IOS_PYTHON": str(managed), "BL_IOS_MODEL": "iPhone5,1",
                               "BL_IOS_UDID": "", "PROJECT_ROOT": str(root),
                               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
                result = subprocess.run([BASH, "--noprofile", "--norc"], input=shell,
                                        text=True, capture_output=True, cwd=root,
                                        env=environment, timeout=10)
                calls = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertEqual([call[2] for call in calls], ["detect", "verify"])
                self.assertEqual(calls[1][calls[1].index("--ecid") + 1], "123")
                queries = [json.loads(line) for line in Path(str(trace) + ".queries").read_text().splitlines()]
                # Only the fresh managed verification query ran: no stock
                # repeated -q snapshots, console -s, or manual identity prompt.
                self.assertEqual(queries, [["-q"]])
                self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
                self.assertIn("different iPhone", result.stderr)
                self.assertNotIn("UNEXPECTED_ENTRY_ACCEPTED", result.stdout)

    def test_query_command_preserves_sudo_wrapper_and_tool_arguments(self):
        tool = "/tools with spaces/irecovery"
        cases = [
            ("", tool, [tool]),
            ("", "sudo " + tool, ["sudo", tool]),
            ("/usr/bin/sudo", "/usr/bin/sudo " + tool, ["/usr/bin/sudo", tool]),
            ("/usr/bin/sudo.ws", "/usr/bin/sudo.ws " + tool, ["/usr/bin/sudo.ws", tool]),
            ("/wrapper with spaces/sudo.ws", "/wrapper with spaces/sudo.ws " + tool,
             ["/wrapper with spaces/sudo.ws", tool]),
        ]
        for backend in self.sources:
            for wrapper, command, expected in cases:
                with self.subTest(backend=backend, wrapper=wrapper), \
                        tempfile.TemporaryDirectory(prefix="entry argv ") as temporary:
                    root = Path(temporary)
                    trace = root / "trace.json"
                    managed = root / "managed python"
                    self.make_python_tool(managed, '''import json,os,sys
with open(os.environ["TRACE"], "w") as stream: json.dump(sys.argv[1:], stream)
print("Normal\\tiPhone5,1\\tn41\\t123\\t8.4.1\\tfixture-serial")
''')
                    shell = self.function(backend, "bl_ios_entry") + r'''
log() { :; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 73; }
bl_ios_entry detect
'''
                    environment = {**os.environ, "TRACE": str(trace), "BL_IOS_PYTHON": str(managed),
                                   "sudo": wrapper, "irecovery": command,
                                   "ideviceinfo": "/tool path/ideviceinfo", "dir": "/tool path",
                                   "BL_IOS_MODEL": "iPhone5,1", "BL_IOS_UDID": "fixture-serial"}
                    result = subprocess.run([BASH, "--noprofile", "--norc"], input=shell, text=True,
                                            capture_output=True, env=environment, cwd=root, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    arguments = json.loads(trace.read_text())
                    self.assertEqual(arguments[arguments.index("--query") + 1:], expected)
                    for flag, value in (("--info", "/tool path/ideviceinfo"),
                                        ("--enter", "/tool path/ideviceenterrecovery"),
                                        ("--model", "iPhone5,1"), ("--udid", "fixture-serial")):
                        self.assertEqual(arguments[arguments.index(flag) + 1], value)


if __name__ == "__main__":
    unittest.main()
