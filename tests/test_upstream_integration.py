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

from ios_timeout.core import ResetError
from ios_timeout.upstream import adapt_script, early_dfu_state, early_dfu_summary


UPSTREAM_SCRIPT = os.environ.get("BL_IOS_UPSTREAM_SCRIPT")
BASH = shutil.which("bash")
WTF = "MODE: WTF\nCPID: 8900\nBDID: 00\nPRODUCT: iPhone1,1\nMODEL: m68ap\n"
PATCHED = "MODE: DFU\nCPID: 8900\nBDID: 04\nSRTG: iBoot-636.66.3x\n"
STOCK = PATCHED.replace("iBoot-636.66.3x", "iBoot-596.24")
EARLY_FUNCTIONS = (
    "bl_ios_send", "bl_ios_early_read", "bl_ios_early_diagnostics",
    "bl_ios_early_wait", "bl_ios_early_prepare", "enter_pwndfu",
)


class EarlyDfuValidationTests(unittest.TestCase):
    def test_generic_wtf_does_not_override_selected_3g(self):
        self.assertEqual(early_dfu_state("iPhone1,2", WTF), "wtf")
        self.assertEqual(early_dfu_state("iPhone1,1", WTF), "wtf")
        for model in ("", "iPhone3,1"):
            with self.assertRaises(ResetError):
                early_dfu_state(model, WTF)

    def test_real_dfu_requires_matching_board_and_exact_patched_signature(self):
        self.assertEqual(early_dfu_state("iPhone1,2", PATCHED), "patched")
        self.assertEqual(early_dfu_state("iPhone1,2", STOCK), "stock")
        self.assertEqual(early_dfu_state("iPhone1,2", STOCK + "PWND: Pwnage 2.0\n"), "stock")
        for query in (
            PATCHED.replace("04", "00"),
            PATCHED.replace("8900", "8940"),
            PATCHED.replace("BDID: 04\n", ""),
            PATCHED.replace("CPID: 8900\n", ""),
            PATCHED.replace("BDID: 04", "BDID: invalid"),
        ):
            with self.subTest(query=query), self.assertRaises(ResetError):
                early_dfu_state("iPhone1,2", query)

    def test_missing_state_can_be_polled_but_other_modes_never_count_as_patched(self):
        self.assertEqual(early_dfu_state("iPhone1,2", ""), "waiting")
        for mode in ("Recovery", "iBSS", "iBEC"):
            self.assertEqual(early_dfu_state("iPhone1,2", PATCHED.replace("DFU", mode)), "waiting")

    def test_diagnostics_exclude_unique_identifiers_and_control_characters(self):
        query = STOCK + "ECID: private-id\nSRNM: private-serial\nIMEI: private-imei\n"
        summary = early_dfu_summary(query)
        self.assertEqual(summary, "MODE=DFU, CPID=8900, BDID=04, SRTG=iBoot-596.24")
        self.assertNotIn("\x1b", early_dfu_summary("MODE: \x1b[31mDFU\n"))
        self.assertEqual(early_dfu_summary(""), "no readable USB recovery state")

STUB_TOOL = r'''
import json
import os
from pathlib import Path
import re
import shutil
import sys

tool, args = sys.argv[1], sys.argv[2:]
entry = {"tool": tool, "args": args}
if tool == "pzb":
    entry["cwd"] = str(Path.cwd())
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
    output = Path(args[args.index("-o") + 1])
    # pzb 9bfdde2 cmd_get strips directories from -o unless -c is present.
    # https://github.com/tihmstar/partialZipBrowser/blob/
    # 9bfdde2b2456181045f74631683fba491d8bf4f2/pzb/main.cpp#L146-L150
    if "-c" not in args:
        output = Path(output.name)
    behavior = os.environ.get("BL_TEST_PZB_RESULT")
    if behavior != "missing":
        output.write_text("" if behavior == "empty" else "fixture: " + member)
    if behavior == "failure":
        raise SystemExit(9)
elif tool in ("xpwntool", "bspatch"):
    shutil.copyfile(args[0], args[1])
elif tool == "managed-python":
    if args[:2] == ["-m", "ios_timeout.upstream"]:
        os.execv(sys.executable, [sys.executable, "-B"] + args)
    if args[:2] == ["-m", "ios_timeout.recovery_timing"]:
        # Exercise the real guidance/identity contract with virtual button time.
        # Nonblocking real query timing is covered in test_recovery_timing.
        import subprocess
        from ios_timeout.recovery_timing import guide, wait_bootloader
        from ios_timeout.core import ResetError
        now = [0.0]
        after_ibss = args[2] == "--after-ibss"
        model = args[3] if after_ibss else args[2]
        command = args[5:] if after_ibss else args[4:]
        class Probe:
            def poll(self):
                result = subprocess.run(command + ["-q"], capture_output=True, text=True)
                return result.stdout if result.returncode == 0 else ""
            def close(self):
                pass
        def advance(_):
            now[0] += 0.5
        try:
            operation = wait_bootloader if after_ibss else guide
            operation(model, Probe(), clock=lambda: now[0], sleep=advance)
        except ResetError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(0)
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
        sequence = os.environ.get("BL_TEST_RECOVERY_SEQUENCE")
        if sequence:
            states = json.loads(sequence)
            counter = Path(os.environ["BL_TEST_TRACE"]).with_suffix(".queries")
            index = int(counter.read_text()) if counter.exists() else 0
            counter.write_text(str(index + 1))
            print(states[min(index, len(states) - 1)], end="")
        else:
            print(os.environ.get("BL_TEST_RECOVERY", "MODE: DFU\n"), end="")
        if os.environ.get("BL_TEST_QUERY_FAILURE") == "1":
            raise SystemExit(9)
    elif args[:1] == ["-f"]:
        if os.environ.get("BL_TEST_FAIL_UPLOAD") == "1":
            raise SystemExit(9)
    elif args[:1] == ["-c"] and args[1] in ("getenv ramdisk-delay", "ramdisk", "devicetree", "bootx"):
        if args[1] == os.environ.get("BL_TEST_FAIL_COMMAND"):
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
        ):
            (self.backend / relative).write_text("temporary test fixture\n")
        for name in ("bruteforce", "device_infos", "restored_external"):
            (self.payloads / name).write_text("temporary payload fixture\n")
        self.wait_helper.write_text("independent bundled wait helper fixture\n")
        stub = self.root / "stub_tool.py"
        stub.write_text(STUB_TOOL)
        # All backend tool paths resolve to this fixture. Unexpected tools fail.
        # Its fresh environment omits inherited Python flags; -B also protects
        # signed bundled runtimes when the fixture is used for packaged QA.
        for name in ("jq", "pzb", "xpwntool", "bspatch", "hfsplus",
                     "managed-python", "irecovery", "iBoot32Patcher",
                     "python3", "curl", "sudo", "ssh", "iproxy"):
            wrapper = self.bin / name
            wrapper.write_text(
                "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -B "
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
        self.trace.with_suffix(".queries").unlink(missing_ok=True)
        env = {
            "PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin",
            "PROJECT_ROOT": str(self.backend),
            "BL_IOS_PYTHON": str(self.bin / "managed-python"),
            "BL_IOS_PAYLOADS": str(self.payloads),
            "BL_IOS_3G_SETUP": str(self.startup),
            "BL_IOS_3G_WAIT": str(self.wait_helper),
            "BL_TEST_TRACE": str(self.trace),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
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
platform=macos
all_flash="Firmware/all_flash/all_flash.n82ap.production"
log() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
error() { printf 'ERROR: %s\n' "$*" >&2; exit 73; }
print() { printf '%s\n' "$*"; }
pause() { printf 'PAUSE\n'; }
sleep() { :; }
device_fw_key_check() { device_fw_key='{}'; }
device_enter_mode() { error "Unexpected device mode transition"; }
mv() {
    if [[ ${BL_TEST_FAIL_FINAL_MV:-} == 1 && $# == 6 && $1 == iBSS ]]; then
        return 9
    fi
    if [[ ${BL_TEST_FAIL_WTF_MV:-} == 1 && $1 == */.wtf-download.*/WTF.* ]]; then
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

    def run_boot(self, **overrides):
        return self.run_functions(
            (*EARLY_FUNCTIONS, "bl_ios_command", "bl_ios_early_before_ibss", "boot_sshrd"),
            'device_target_build=8C148\ndevice_enter_mode() { enter_pwndfu; }\nboot_sshrd',
            **overrides)

    def test_completed_ibss_upload_that_stays_in_dfu_never_sends_ramdisk(self):
        result = self.run_boot(BL_TEST_RECOVERY=PATCHED)
        self.assert_stopped(result, "no ramdisk was sent")
        self.assertIn("still in DFU", result.stdout + result.stderr)
        transfers = [entry["args"] for entry in self.trace_entries()
                     if entry["tool"] == "irecovery" and entry["args"][0] in ("-f", "-c")]
        self.assertEqual(transfers, [["-f", str(self.destination / "iBSS")]])

    def test_verified_recovery_allows_boot_chain_in_order(self):
        recovery = PATCHED.replace("DFU", "Recovery")
        result = self.run_boot(BL_TEST_RECOVERY_SEQUENCE=json.dumps([PATCHED, PATCHED, recovery]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("phone identity verified", result.stdout)
        commands = [entry["args"] for entry in self.trace_entries()
                    if entry["tool"] == "irecovery" and entry["args"][0] in ("-f", "-c")]
        self.assertEqual(commands, [
            ["-f", str(self.destination / "iBSS")],
            ["-f", str(self.destination / "Ramdisk.dmg")],
            ["-c", "getenv ramdisk-delay"], ["-c", "ramdisk"],
            ["-f", str(self.destination / "DeviceTree.dec")], ["-c", "devicetree"],
            ["-f", str(self.destination / "Kernelcache.dec")], ["-c", "bootx"],
        ])

    def test_failed_ramdisk_command_stops_before_kernel_upload(self):
        recovery = PATCHED.replace("DFU", "Recovery")
        result = self.run_boot(BL_TEST_RECOVERY_SEQUENCE=json.dumps([PATCHED, PATCHED, recovery]),
                               BL_TEST_FAIL_COMMAND="ramdisk")
        self.assert_stopped(result, "Recovery command failed: ramdisk")
        self.assertFalse(any(entry["args"][-1].endswith("Kernelcache.dec")
                             for entry in self.trace_entries() if entry["args"]))

    def test_early_wtf_branch_rejects_missing_pwnd_and_wrong_srtg(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([WTF, WTF, STOCK]))
        self.assert_stopped(result, "S5L8900 patched verification timed out")
        self.assertIn("SRTG=iBoot-596.24", result.stderr)
        uploads = [entry for entry in self.trace_entries()
                   if entry["tool"] == "irecovery" and entry["args"][:1] == ["-f"]]
        self.assertEqual(len(uploads), 1)
        self.assertTrue(uploads[0]["args"][1].endswith(
            "WTF.s5l8900xall.RELEASE.dfu.patched"))

    def test_early_wtf_branch_accepts_expected_srtg_fallback(self):
        result = self.run_functions(
            EARLY_FUNCTIONS,
            'enter_pwndfu\n[[ $device_pwnd == "Pwnage 2.0" ]] || error "Bad state"',
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([WTF, WTF, PATCHED]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Patched S5L8900 DFU verified", result.stdout)

    def test_wtf_empty_cache_downloads_to_staging_then_reuses_cache_with_spaces(self):
        moved = self.root / "backend with spaces"
        self.backend.rename(moved)
        self.backend = moved
        cache = self.backend / "saved/WTF.s5l8900xall.RELEASE.dfu"
        cache.parent.rmdir()
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([WTF, WTF, PATCHED]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(cache.read_text(),
                         "fixture: Firmware/dfu/WTF.s5l8900xall.RELEASE.dfu")
        downloads = [entry for entry in self.trace_entries() if entry["tool"] == "pzb"]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0]["args"][3], cache.name)
        self.assertEqual(Path(downloads[0]["cwd"]).parent.resolve(), cache.parent.resolve())
        self.assertNotEqual(Path(downloads[0]["cwd"]).resolve(), self.work.resolve())
        self.assertFalse(list(cache.parent.glob(".wtf-download.*")))
        self.assertFalse((self.work / cache.name).exists())
        self.trace.unlink()
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([WTF, WTF, PATCHED]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(entry["tool"] == "pzb" for entry in self.trace_entries()))

    def test_wtf_missing_empty_or_failed_download_never_reaches_patch_or_upload(self):
        cache = self.backend / "saved/WTF.s5l8900xall.RELEASE.dfu"
        for behavior in ("missing", "empty", "failure"):
            with self.subTest(behavior=behavior):
                if self.trace.exists():
                    self.trace.unlink()
                result = self.run_functions(
                    EARLY_FUNCTIONS, "enter_pwndfu",
                    BL_TEST_RECOVERY=WTF,
                    BL_TEST_PZB_RESULT=behavior)
                self.assert_stopped(result, "WTF DFU download")
                self.assertFalse(cache.exists())
                self.assertFalse(list(cache.parent.glob(".wtf-download.*")))
                self.assertFalse(any(
                    entry["tool"] == "bspatch"
                    or (entry["tool"] == "irecovery" and entry["args"][:1] == ["-f"])
                    for entry in self.trace_entries()))

    def test_wtf_cache_publish_failure_never_reaches_patch_or_upload(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY=WTF,
            BL_TEST_FAIL_WTF_MV="1")
        self.assert_stopped(result, "Could not cache WTF DFU")
        self.assertFalse(list((self.backend / "saved").glob(".wtf-download.*")))
        self.assertFalse(any(
            entry["tool"] == "bspatch"
            or (entry["tool"] == "irecovery" and entry["args"][:1] == ["-f"])
            for entry in self.trace_entries()))

    def test_already_patched_dfu_never_reuploads_shared_wtf(self):
        result = self.run_functions(EARLY_FUNCTIONS, "enter_pwndfu",
                                    BL_TEST_RECOVERY=PATCHED)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipping WTF upload", result.stdout)
        self.assertFalse(any(entry["tool"] in ("pzb", "bspatch")
                             or entry["args"][:1] == ["-f"]
                             for entry in self.trace_entries()))
        self.assertEqual(sum(entry["tool"] == "irecovery"
                             for entry in self.trace_entries()), 1)

    def test_late_reenumeration_waits_for_atomic_matching_dfu_snapshot(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([WTF, WTF, "", "", WTF, "", STOCK, PATCHED]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Patched S5L8900 DFU verified", result.stdout)
        usb = [entry["args"] for entry in self.trace_entries() if entry["tool"] == "irecovery"]
        self.assertEqual(sum(args == ["-q"] for args in usb), 8)
        self.assertEqual(sum(args[:1] == ["-f"] for args in usb), 1)

    def test_patched_signature_on_wrong_board_stops_after_shared_upload(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([WTF, WTF, PATCHED.replace("04", "00")]))
        self.assert_stopped(result, "USB identity verification failed")
        self.assertIn("Connected device is iPhone1,1", result.stderr)
        self.assertEqual(sum(entry["args"][:1] == ["-f"]
                             for entry in self.trace_entries()), 1)

    def test_stock_dfu_and_untrusted_pwnd_do_not_trigger_shared_upload(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY=STOCK + "PWND: Pwnage 2.0\nECID: private-id\n")
        self.assert_stopped(result, "S5L8900 wtf verification timed out")
        self.assertIn("Force restart", result.stderr)
        self.assertNotIn("private-id", result.stdout + result.stderr)
        self.assertFalse(any(entry["args"][:1] == ["-f"]
                             for entry in self.trace_entries()))

    def test_button_helper_detects_wtf_during_countdown(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu",
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([STOCK, STOCK, "", WTF, WTF, PATCHED]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PAUSE", result.stdout)
        self.assertNotIn("Release TOP, keep holding HOME", result.stdout)
        self.assertIn("Patched S5L8900 DFU verified", result.stdout)

    def test_normal_entry_requests_recovery_then_uses_the_same_verified_button_path(self):
        invocation = '''
device_mode=Normal
device_enter_mode() {
    [[ $1 == Recovery ]] || error "Wrong requested mode"
    print "NORMAL_TO_RECOVERY"
    device_mode=Recovery
}
enter_pwndfu
'''
        recovery = STOCK.replace("MODE: DFU", "MODE: Recovery")
        result = self.run_functions(
            EARLY_FUNCTIONS, invocation,
            BL_TEST_RECOVERY_SEQUENCE=json.dumps(["", "", recovery, "", WTF, WTF, PATCHED]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("NORMAL_TO_RECOVERY"), 1)
        self.assertIn("PAUSE", result.stdout)
        self.assertIn("Patched S5L8900 DFU verified", result.stdout)
        self.assertEqual(sum(entry["args"][:1] == ["-f"] for entry in self.trace_entries()), 1)

    def test_linux_wait_has_24_attempts_and_timeout_has_last_state(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "platform=linux\nenter_pwndfu",
            BL_TEST_RECOVERY_SEQUENCE=json.dumps([WTF, WTF, STOCK + "ECID: private-id\n", ""]))
        self.assert_stopped(result, "S5L8900 patched verification timed out")
        self.assertIn("up to 24 polls", result.stdout)
        self.assertIn("no readable USB recovery state", result.stderr)
        self.assertIn("Last readable USB state: MODE=DFU", result.stderr)
        self.assertNotIn("private-id", result.stdout + result.stderr)
        self.assertEqual(sum(entry["args"] == ["-q"] for entry in self.trace_entries()), 26)

    def test_query_exit_failure_cannot_authorize_upload(self):
        result = self.run_functions(
            ("bl_ios_early_read", "bl_ios_early_diagnostics", "bl_ios_early_before_ibss"),
            "bl_ios_early_before_ibss", BL_TEST_RECOVERY=PATCHED, BL_TEST_QUERY_FAILURE="1")
        self.assert_stopped(result, "Verified patched S5L8900 DFU is required")

    def test_pre_ibss_gate_rechecks_board_and_signature(self):
        for query in (STOCK, WTF, PATCHED.replace("04", "00")):
            with self.subTest(query=query):
                result = self.run_functions(
                    ("bl_ios_early_read", "bl_ios_early_diagnostics", "bl_ios_early_before_ibss"),
                    "bl_ios_early_before_ibss\nbl_ios_send iBSS", BL_TEST_RECOVERY=query)
                self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
                self.assertNotIn("AFTER_FUNCTION", result.stdout)
                self.assertFalse(any(entry["args"][:1] == ["-f"]
                                     for entry in self.trace_entries()))
        boot = self.function("boot_sshrd")
        self.assertIn('[[ $device_proc == 1 ]] && bl_ios_early_before_ibss\n'
                      '        log "Sending iBSS..."; bl_ios_send $ramdisk_path/iBSS', boot)

    def test_failed_shared_upload_cannot_reach_verification(self):
        result = self.run_functions(
            EARLY_FUNCTIONS, "enter_pwndfu", BL_TEST_RECOVERY=WTF,
            BL_TEST_FAIL_UPLOAD="1")
        self.assert_stopped(result, "Firmware upload failed")
        self.assertEqual(sum(entry["args"] == ["-q"] for entry in self.trace_entries()), 2)

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
        self.assertTrue(downloads[0][-1].endswith("iPhone1,2_3.1.3_7E18_Restore.ipsw"))
        for args in downloads[1:]:
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

    def test_3g_first_stage_does_not_reuse_421_ibss_cache(self):
        self.destination.mkdir(parents=True)
        old = self.destination / "iBSS.n82ap.RELEASE.dfu"
        old.write_text("old 4.2.1 first-stage image")
        result = self.run_create()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(old.read_text(), "old 4.2.1 first-stage image")
        first_stage = self.backend / "saved/iPhone1,2/ramdisk_7E18" / old.name
        self.assertEqual(first_stage.read_text(), "fixture: Firmware/dfu/" + old.name)
        self.assertEqual((self.destination / "iBSS").read_bytes(), first_stage.read_bytes())
        self.trace.unlink()
        result = self.run_create()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(entry["tool"] == "pzb" for entry in self.trace_entries()))

    def test_failed_first_stage_download_cannot_use_a_leftover_work_file(self):
        (self.work / "iBSS.n82ap.RELEASE.dfu").write_text("old work file")
        result = self.run_create(BL_TEST_PZB_RESULT="failure")
        self.assert_stopped(result, "Failed to download iBSS")
        self.assert_no_boot_patching()
        self.assertFalse((self.backend / "saved/iPhone1,2/ramdisk_7E18/iBSS.n82ap.RELEASE.dfu").exists())


if __name__ == "__main__":
    unittest.main()
