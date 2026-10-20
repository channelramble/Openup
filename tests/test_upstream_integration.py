"""Opt-in shell integration tests for the hash-pinned upstream backend.

Run with BL_IOS_UPSTREAM_SCRIPT=/path/to/Bruteforce.sh. The fixture is supplied
separately rather than vendoring upstream source. Only individual adapted
functions run, with temporary files and fake download, patch, and USB tools.
These tests exercise shell control flow; they do not validate firmware binaries
or communicate with a phone.
"""

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

from ios_timeout.upstream import adapt_script


UPSTREAM_SCRIPT = os.environ.get("BL_IOS_UPSTREAM_SCRIPT")
BASH = shutil.which("bash")

STUB_TOOL = r'''
import json
import os
from pathlib import Path
import re
import shutil
import sys

tool, args = sys.argv[1], sys.argv[2:]
entry = {"tool": tool, "args": args}
if tool == "hfsplus" and args[1:3] == ["add", "setup.temp"]:
    entry["startup"] = Path("setup.temp").read_text()
with open(os.environ["BL_TEST_TRACE"], "a") as trace:
    trace.write(json.dumps(entry) + "\n")

if tool == "jq":
    query = args[-1]
    component = re.search(r'\.image == "([^"]+)"', query).group(1)
    filenames = {
        # Missing upstream entries must use the corrected n82ap fallbacks.
        "iBSS": "", "iBEC": "",
        "DeviceTree": "DeviceTree.n82ap.img3",
        "Kernelcache": "kernelcache.release.n82",
        "RestoreRamdisk": "038-0029-002.dmg",
    }
    sys.stdin.read()
    print(filenames[component] if query.endswith(".filename") else "null",
          end="")
elif tool == "pzb":
    member = args[args.index("-g") + 1]
    Path(args[args.index("-o") + 1]).write_text("fixture: " + member)
elif tool in ("xpwntool", "bspatch"):
    shutil.copyfile(args[0], args[1])
elif tool == "managed-python":
    if args[:2] not in (
        ["-m", "ios_timeout.armv6_boot"],
        ["-m", "ios_timeout.armv6_kernel"],
    ):
        raise SystemExit("Unexpected Python command")
    shutil.copyfile(args[-2], args[-1])
elif tool == "hfsplus":
    if args[1] == os.environ.get("BL_TEST_FAIL_HFS"):
        raise SystemExit(9)
    if args[1] == "add" and not Path(args[2]).is_file():
        raise SystemExit(8)
elif tool == "sudo":
    expected = Path(os.environ["BL_TEST_TRACE"]).parent / "bin/irecovery"
    if not args or Path(args[0]) != expected:
        raise SystemExit("Unexpected sudo target")
    os.execv(args[0], args)
elif tool == "irecovery":
    if args == ["-q"]:
        print(os.environ.get("BL_TEST_RECOVERY", "MODE: DFU\n"), end="")
    elif args[:1] == ["-f"]:
        if os.environ.get("BL_TEST_FAIL_UPLOAD") == "1":
            raise SystemExit(9)
    else:
        raise SystemExit("Unexpected USB command")
else:
    raise SystemExit("Unexpected external tool: " + tool)
'''


@unittest.skipUnless(UPSTREAM_SCRIPT and BASH and os.name != "nt",
                     "Set BL_IOS_UPSTREAM_SCRIPT and use POSIX bash (WSL on Windows)")
class UpstreamShellIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # adapt_script verifies the source hash before any function is extracted.
        cls.adapted = adapt_script(Path(UPSTREAM_SCRIPT).read_bytes()).decode()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bl-ios-shell-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.backend = self.root / "backend"
        self.bin = self.root / "bin"
        self.work = self.root / "work"
        self.payloads = self.root / "payloads"
        self.wait_helper = self.root / "bundled-armv6/restored_external"
        self.trace = self.root / "trace.jsonl"
        self.startup = self.root / "iphone3g-setup.sh"
        source_root = Path(__file__).resolve().parents[1]
        self.startup.write_bytes(
            (source_root / "ios_timeout/data/iphone3g-setup.sh").read_bytes())
        for folder in (self.bin, self.work, self.payloads, self.wait_helper.parent,
                       self.backend / "resources/sshrd",
                       self.backend / "resources/patch",
                       self.backend / "saved"):
            folder.mkdir(parents=True, exist_ok=True)
        for relative in (
            "resources/sshrd/sbplist.tar",
            "resources/sshrd/ssh_old.tar",
            "resources/setup.sh",
            "resources/patch/WTF.s5l8900xall.RELEASE.patch",
            "saved/WTF.s5l8900xall.RELEASE.dfu",
        ):
            (self.backend / relative).write_text("temporary test fixture\n")
        for name in ("bruteforce", "device_infos", "restored_external"):
            (self.payloads / name).write_text("temporary payload fixture\n")
        self.wait_helper.write_text("independent bundled wait helper fixture\n")
        stub = self.root / "stub_tool.py"
        stub.write_text(STUB_TOOL)
        # All backend tool paths resolve to this fixture. Unexpected tools fail.
        for name in ("jq", "pzb", "xpwntool", "bspatch", "hfsplus",
                     "managed-python", "irecovery", "iBoot32Patcher",
                     "python3", "curl", "sudo", "ssh", "iproxy"):
            wrapper = self.bin / name
            wrapper.write_text(
                "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                + shlex.quote(str(stub)) + " " + shlex.quote(name) + ' "$@"\n')
            wrapper.chmod(0o700)
        self.destination = self.backend / "saved/iPhone1,2/ramdisk_8C148"

    def function(self, name):
        matches = re.findall(
            r"^" + re.escape(name) + r"\(\) \{\n.*?^\}",
            self.adapted, flags=re.MULTILINE | re.DOTALL)
        self.assertEqual(len(matches), 1, f"Expected one complete {name} function")
        return matches[0]

    def run_functions(self, names, invocation, **overrides):
        env = {
            "PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin",
            "PROJECT_ROOT": str(self.backend),
            "BL_IOS_PYTHON": str(self.bin / "managed-python"),
            "BL_IOS_PAYLOADS": str(self.payloads),
            "BL_IOS_3G_SETUP": str(self.startup),
            "BL_IOS_3G_WAIT": str(self.wait_helper),
            "BL_TEST_TRACE": str(self.trace),
            **overrides,
        }
        script = r'''
dir="$PATH"
dir="${dir%%:*}"
jq="$dir/jq"
bspatch="$dir/bspatch"
irecovery="$dir/irecovery"
device_type="iPhone1,2"
device_model="n82"
device_proc=1
device_mode=DFU
all_flash="Firmware/all_flash/all_flash.n82ap.production"
log() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
error() { printf 'ERROR: %s\n' "$*" >&2; exit 73; }
sleep() { :; }
device_fw_key_check() { device_fw_key='{}'; }
device_enter_mode() { error "Unexpected device mode transition"; }
mv() {
    if [[ ${BL_TEST_FAIL_FINAL_MV:-} == 1 && $# == 6 && $1 == iBSS ]]; then
        return 9
    fi
    command mv "$@"
}
'''
        script += "\n".join(self.function(name) for name in names)
        script += "\n" + invocation + "\nprintf 'AFTER_FUNCTION\\n'\n"
        return subprocess.run(
            [BASH, "--noprofile", "--norc"], input=script, text=True,
            cwd=self.work, env=env, capture_output=True, timeout=30)

    def trace_entries(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def run_create(self, **overrides):
        return self.run_functions(
            ("select_ramdisk_version", "ipsw_get_url", "create_sshrd"),
            "create_sshrd", **overrides)

    def assert_stopped(self, result, message):
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("AFTER_FUNCTION", result.stdout)

    def assert_no_boot_patching(self):
        self.assertFalse(any(entry["tool"] == "managed-python"
                             for entry in self.trace_entries()))
        self.assertFalse((self.destination / "iBSS").exists())

    def test_entire_adapted_script_has_valid_bash_syntax(self):
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-n"], input=self.adapted,
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_early_wtf_branch_rejects_missing_pwnd_and_wrong_srtg(self):
        result = self.run_functions(
            ("bl_ios_send", "enter_pwndfu"), "enter_pwndfu",
            BL_TEST_RECOVERY="MODE: DFU\nSRTG: iBoot-wrong\n")
        self.assert_stopped(result, "S5L8900 pwnDFU verification failed")
        uploads = [entry for entry in self.trace_entries()
                   if entry["tool"] == "irecovery" and entry["args"][:1] == ["-f"]]
        self.assertEqual(len(uploads), 1)
        self.assertTrue(uploads[0]["args"][1].endswith(
            "WTF.s5l8900xall.RELEASE.dfu.patched"))

    def test_early_wtf_branch_accepts_expected_srtg_fallback(self):
        result = self.run_functions(
            ("bl_ios_send", "enter_pwndfu"),
            'enter_pwndfu\n[[ $device_pwnd == "Pwnage 2.0" ]] || error "Bad state"',
            BL_TEST_RECOVERY="MODE: DFU\nSRTG: iBoot-636.66.3x\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Exploit success (SRTG verified)", result.stdout)

    def test_failed_file_upload_stops_the_caller(self):
        result = self.run_functions(
            ("bl_ios_send",), 'bl_ios_send "firmware with spaces.img3"',
            BL_TEST_FAIL_UPLOAD="1")
        self.assert_stopped(result, "Firmware upload failed; boot cancelled")
        self.assertEqual(self.trace_entries(), [
            {"tool": "irecovery", "args": ["-f", "firmware with spaces.img3"]},
        ])

    def test_linux_sudo_prefix_preserves_upload_filename(self):
        result = self.run_functions(
            ("bl_ios_send",),
            'irecovery="sudo $irecovery"\nbl_ios_send "firmware with spaces.img3"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AFTER_FUNCTION", result.stdout)
        self.assertEqual(self.trace_entries(), [
            {"tool": "sudo", "args": [
                str(self.bin / "irecovery"), "-f", "firmware with spaces.img3",
            ]},
            {"tool": "irecovery", "args": ["-f", "firmware with spaces.img3"]},
        ])

    def test_required_ramdisk_grow_failure_stops_preparation(self):
        result = self.run_create(BL_TEST_FAIL_HFS="grow")
        self.assert_stopped(result, "Required ramdisk update failed")
        hfs = [entry["args"][1] for entry in self.trace_entries()
               if entry["tool"] == "hfsplus"]
        self.assertEqual(hfs, ["grow"])
        self.assert_no_boot_patching()

    def test_required_ramdisk_add_failure_stops_preparation(self):
        result = self.run_create(BL_TEST_FAIL_HFS="add")
        self.assert_stopped(result, "Required ramdisk update failed")
        hfs = [entry for entry in self.trace_entries() if entry["tool"] == "hfsplus"]
        self.assertEqual(hfs[-1]["args"][1:], [
            "add", str(self.payloads / "bruteforce"), "usr/bin/bruteforce",
        ])
        self.assert_no_boot_patching()

    def test_final_publish_failure_stops_preparation(self):
        result = self.run_create(BL_TEST_FAIL_FINAL_MV="1")
        self.assert_stopped(result, "Could not publish all ramdisk components")
        self.assertNotIn("Ramdisk files saved:", result.stdout)
        self.assertFalse((self.destination / "iBSS").exists())

    def test_missing_independent_wait_helper_stops_preparation(self):
        result = self.run_create(BL_IOS_3G_WAIT=str(self.root / "missing-helper"))
        self.assert_stopped(result, "Required ramdisk update failed")
        self.assert_no_boot_patching()

    def test_3g_preparation_uses_421_managed_startup_and_validators(self):
        result = self.run_create()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4.2.1/8C148", result.stdout)
        self.assertIn("AFTER_FUNCTION", result.stdout)
        entries = self.trace_entries()
        downloads = [entry["args"] for entry in entries if entry["tool"] == "pzb"]
        members = [args[args.index("-g") + 1] for args in downloads]
        self.assertEqual(members, [
            "Firmware/dfu/iBSS.n82ap.RELEASE.dfu",
            "Firmware/dfu/iBEC.n82ap.RELEASE.dfu",
            "Firmware/all_flash/all_flash.n82ap.production/DeviceTree.n82ap.img3",
            "kernelcache.release.n82",
            "038-0029-002.dmg",
        ])
        for args in downloads:
            self.assertTrue(args[-1].startswith("https://secure-appldnld.apple.com/"))
            self.assertTrue(args[-1].endswith("iPhone1,2_4.2.1_8C148_Restore.ipsw"))
        startup_entries = [entry for entry in entries if "startup" in entry]
        self.assertEqual(len(startup_entries), 1)
        self.assertEqual(startup_entries[0]["startup"], self.startup.read_text())
        self.assertIn("/usr/local/bin/bl-ios-wait --sleep 1", startup_entries[0]["startup"])
        self.assertIn("/usr/local/bin/bl-ios-wait --sleep 60", startup_entries[0]["startup"])
        self.assertNotIn("restored_external.sshrd --sleep", startup_entries[0]["startup"])
        added = [entry["args"][2:] for entry in entries
                 if entry["tool"] == "hfsplus" and entry["args"][1] == "add"]
        self.assertIn([str(self.payloads / "restored_external"),
                       "usr/local/bin/restored_external.sshrd"], added)
        self.assertIn([str(self.wait_helper), "usr/local/bin/bl-ios-wait"], added)
        self.assertNotIn([str(self.payloads / "restored_external"),
                          "usr/local/bin/bl-ios-wait"], added)
        python_calls = [entry["args"] for entry in entries
                        if entry["tool"] == "managed-python"]
        self.assertEqual(python_calls, [
            ["-m", "ios_timeout.armv6_boot", "ibss", str(self.bin / "iBoot32Patcher"),
             "iBSS.raw", "iBSS.patched"],
            ["-m", "ios_timeout.armv6_boot", "ibec", str(self.bin / "iBoot32Patcher"),
             "iBEC.raw", "iBEC.patched"],
            ["-m", "ios_timeout.armv6_kernel", "Kernelcache.raw", "Kernelcache.patched"],
        ])
        self.assertFalse(any(entry["tool"] in ("bspatch", "iBoot32Patcher", "irecovery")
                             for entry in entries))
        for name in ("iBSS", "iBEC", "DeviceTree.dec", "Kernelcache.dec", "Ramdisk.dmg"):
            self.assertTrue((self.destination / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
