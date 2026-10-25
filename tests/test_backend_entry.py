import json
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ios_timeout import backend_entry as entry
from ios_timeout.core import ResetError
from ios_timeout import legacy_kit, upstream
from ios_timeout.entry_adapter import HOOK


SERIAL = "fixture-serial-1234"
DEVICE = {"udid": SERIAL, "device_id": 1, "product_id": 0x12A8}
VALUES = {"ProductType": "iPhone5,1", "HardwareModel": "N41AP", "UniqueChipID": 123,
          "UniqueDeviceID": SERIAL, "ProductVersion": "8.4.1"}
NORMAL = ("Normal", "iPhone5,1", "n41", "123", "8.4.1", SERIAL)
RECOVERY = "MODE: Recovery\nCPID: 8950\nBDID: 00\nECID: 7b\n"


class BackendEntryTests(unittest.TestCase):
    def test_simple_failure_uses_paired_xml_scoped_to_same_serial(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1 if "-s" in command else 0,
                                               stdout=plistlib.dumps(VALUES))
        with patch.object(entry, "_devices", return_value=[DEVICE]), \
             patch.object(entry, "_run_info", side_effect=run):
            self.assertEqual(entry.read_normal("/tool with spaces/ideviceinfo", SERIAL, "iPhone5,1"), NORMAL)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[1:3] == ["-u", SERIAL] for c in calls))

    def test_single_key_fallback_does_not_require_xml_support(self):
        def run(command, **kwargs):
            value = VALUES.get(command[-1], "") if "-k" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=str(value).encode())
        with patch.object(entry, "_devices", return_value=[DEVICE]), \
             patch.object(entry, "_run_info", side_effect=run):
            self.assertEqual(entry.read_normal("info", SERIAL, "iPhone5,1"), NORMAL)

    def test_wrong_or_multiple_serials_never_call_native_tools(self):
        for devices in ([{**DEVICE, "udid": "other-serial"}], [DEVICE, {**DEVICE, "udid": "other-serial"}]):
            with self.subTest(devices=devices), patch.object(entry, "_devices", return_value=devices), \
                 patch.object(entry, "_run_info") as run, self.assertRaises(ResetError):
                entry.read_normal("info", SERIAL, "iPhone5,1")
            run.assert_not_called()

    def test_wrong_actual_model_is_not_replaced_by_selected_model(self):
        with patch.object(entry, "_devices", return_value=[DEVICE]), \
             patch.object(entry, "_run_info", return_value=subprocess.CompletedProcess([], 0, plistlib.dumps(VALUES))), \
             self.assertRaisesRegex(ResetError, "iPhone5,1.*iPhone5,2"):
            entry.read_normal("info", SERIAL, "iPhone5,2")

    def test_locked_visible_phone_uses_manual_recovery_without_kick(self):
        with patch.object(entry, "read_normal", side_effect=entry.UnreadableNormalDevice), \
             patch.object(entry, "_devices", return_value=[DEVICE]), \
             patch.object(entry, "_continue") as prompt, \
             patch.object(entry, "wait_mode", return_value=("Recovery", *NORMAL[1:])) as wait, \
             patch.object(entry.subprocess, "run") as run:
            entry.detect("info", "iPhone5,1", SERIAL, ["irecovery"])
        self.assertIn("connected", prompt.call_args.args[0])
        self.assertIn("HOME", prompt.call_args.args[0])
        wait.assert_called_once()
        run.assert_not_called()

    def test_missing_normal_service_preserves_independent_dfu_discovery(self):
        with patch.object(entry, "_devices", side_effect=entry.NormalServiceUnavailable), \
             self.assertRaises(entry.NoNormalDevice):
            entry.detect("info", "iPhone5,1", SERIAL, ["query"])
        with patch.object(entry, "_devices", side_effect=ResetError("Ambiguous identities")), \
             self.assertRaisesRegex(ResetError, "Ambiguous"):
            entry.detect("info", "iPhone5,1", SERIAL, ["query"])

    def test_native_info_output_and_time_are_bounded(self):
        for code in ("import time;time.sleep(30)", "import sys,time;sys.stdout.write('x'*2000000);sys.stdout.flush();time.sleep(30)"):
            start = time.monotonic()
            with self.subTest(code=code):
                self.assertIsNone(entry._run_info([sys.executable, "-c", code], timeout=0.2))
                self.assertLess(time.monotonic() - start, 1.5)

    def test_native_info_cancellation_reaps_child(self):
        children = []
        popen = subprocess.Popen
        def start(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(entry.subprocess, "Popen", side_effect=start), \
             patch.object(entry.time, "sleep", side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            entry._run_info([sys.executable, "-c", "import time;time.sleep(30)"], timeout=2)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_cli_dfu_walkthrough_prints_every_button_phase_and_countdown(self):
        now = [0.0]
        def sleep(delay):
            now[0] += delay
        wait = entry.wait_mode
        def fast_wait(*args, **kwargs):
            return wait(*args, **kwargs, clock=lambda: now[0], sleep=sleep)
        stderr, stdout = io.StringIO(), io.StringIO()
        probe = Mock()
        probe.poll.return_value = ""
        with patch.object(entry, "_continue"), patch.object(entry, "wait_mode", side_effect=fast_wait), \
             patch("ios_timeout.recovery_timing.RecoveryProbe", return_value=probe), \
             patch.dict(os.environ, {"BL_IOS_UI_PROTOCOL": ""}), \
             redirect_stderr(stderr), redirect_stdout(stdout), self.assertRaises(ResetError):
            entry.guide_dfu("iPhone5,1", "123", ["query"])
        self.assertIn("Hold TOP and HOME together. (10 seconds remaining)", stderr.getvalue())
        self.assertIn("Release TOP; keep holding HOME. (8 seconds remaining)", stderr.getvalue())
        self.assertIn("Keep holding HOME while the iPhone reconnects.", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        probe.close.assert_called_once()

    def test_matching_query_after_deadline_does_not_authorize_transition(self):
        now = [0.0]
        def late_query():
            now[0] = 11.0
            return RECOVERY
        probe = Mock()
        probe.poll.side_effect = late_query
        with patch("ios_timeout.recovery_timing.RecoveryProbe", return_value=probe), \
             self.assertRaisesRegex(ResetError, "in time"):
            entry.wait_mode("iPhone5,1", "123", ["query"], timeout=10,
                            clock=lambda: now[0], sleep=lambda delay: None)
        probe.close.assert_called_once()

    def test_changed_ecid_stops_before_reboot(self):
        with patch.object(entry, "read_normal", return_value=NORMAL), \
             patch.object(entry.subprocess, "run") as run, self.assertRaisesRegex(ResetError, "changed"):
            entry.enter_recovery("info", "enter", "iPhone5,1", SERIAL, "456", ["query"])
        run.assert_not_called()

    def test_failed_or_timed_out_kick_requires_verified_manual_recovery(self):
        for failure in (subprocess.CompletedProcess([], 9), subprocess.TimeoutExpired(["enter"], 8)):
            with self.subTest(failure=failure), patch.object(entry, "read_normal", return_value=NORMAL), \
                 patch.object(entry.subprocess, "run", side_effect=failure if isinstance(failure, Exception) else None,
                              return_value=failure) as run, patch.object(entry, "_continue") as prompt, \
                 patch.object(entry, "wait_mode", side_effect=ResetError("Recovery not verified")) as wait, \
                 self.assertRaisesRegex(ResetError, "not verified"):
                entry.enter_recovery("info", "enter", "iPhone5,1", SERIAL, "123", ["query"])
            self.assertEqual(run.call_args.args[0], ["enter", SERIAL])
            self.assertEqual(run.call_count, 1)
            prompt.assert_called_once()
            wait.assert_called_once()

    def test_recovery_board_and_same_product_different_ecid_are_rejected(self):
        self.assertEqual(entry.recovery_identity("iPhone5,1", RECOVERY, "123", "Recovery")[3], "123")
        for query in (RECOVERY.replace("BDID: 00", "BDID: 02"),
                      RECOVERY.replace("ECID: 7b", "ECID: 7c"), RECOVERY + "BDID: 00\n"):
            with self.subTest(query=query), self.assertRaises(ResetError):
                entry.recovery_identity("iPhone5,1", query, "123", "Recovery")
        self.assertIsNone(entry.recovery_identity("iPhone5,1", "MODE: Recovery\n", "123", "Recovery"))
        self.assertIsNone(entry.recovery_identity("iPhone5,1", RECOVERY, "123", "DFU"))

    def test_blocked_query_deadline_and_cancellation_reap_child(self):
        from ios_timeout.recovery_timing import RecoveryProbe
        for cancel in (False, True):
            probe = RecoveryProbe([sys.executable, "-c", "import time;time.sleep(30)"])
            started = time.monotonic()
            def sleep(delay):
                if cancel and probe.process is not None:
                    raise KeyboardInterrupt
                time.sleep(delay)
            with self.subTest(cancel=cancel), \
                 patch("ios_timeout.recovery_timing.RecoveryProbe", return_value=probe), \
                 self.assertRaises(KeyboardInterrupt if cancel else ResetError):
                entry.wait_mode("iPhone5,1", "123", ["query"], timeout=0.2, sleep=sleep)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIsNone(probe.process)


@unittest.skipUnless(os.environ.get("BL_IOS_UPSTREAM_SCRIPT") and os.environ.get("BL_IOS_LEGACY_SCRIPT")
                     and shutil.which("bash") and os.name != "nt", "Pinned POSIX backend fixtures required")
class PinnedNormalEntryTests(unittest.TestCase):
    def run_entry(self, backend, *, model="iPhone5,1", selected="iPhone5,1", kickoff=0):
        original = Path(os.environ["BL_IOS_UPSTREAM_SCRIPT" if backend == "recover" else "BL_IOS_LEGACY_SCRIPT"]).read_bytes()
        source = (upstream.adapt_script(original) if backend == "recover" else legacy_kit.adapt_menu_only(original)).decode()
        def function(name):
            found = re.findall(r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S)
            self.assertEqual(len(found), 1)
            return found[0]
        with tempfile.TemporaryDirectory(prefix="entry shell ") as folder:
            root = Path(folder)
            tools = root / "tools"
            tools.mkdir()
            trace = root / "trace"
            state = root / "state"
            queries = root / "queries"
            product = {**VALUES, "ProductType": model, "HardwareModel": "N41AP" if model == "iPhone5,1" else "N42AP"}
            program = root / "tool.py"
            program.write_text('''import json,os,plistlib,sys
from pathlib import Path
name=sys.argv[1]; args=sys.argv[2:]
with open(os.environ['TRACE'],'a') as f:f.write(json.dumps([name,args])+'\\n')
state=Path(os.environ['STATE']); queries=Path(os.environ['QUERIES'])
if name=='ideviceinfo':
 if '-s' in args:sys.exit(1)
 sys.stdout.buffer.write(plistlib.dumps(json.loads(os.environ['VALUES'])))
elif name=='ideviceenterrecovery':
 if int(os.environ['KICKOFF']):sys.exit(int(os.environ['KICKOFF']))
 state.touch()
elif name=='irecovery':
 if not state.exists():sys.exit(1)
 n=int(queries.read_text()) if queries.exists() else 0
 queries.write_text(str(n+1))
 modes=[None,None,'Recovery',None,None,'DFU']
 mode=modes[min(n,len(modes)-1)]
 if mode is None:sys.exit(1)
 print('MODE: '+mode)
 print('CPID: 8950\\nBDID: '+os.environ['BDID']+'\\nECID: 7b')
elif name=='managed-python':
 from ios_timeout import backend_entry as entry
 entry._devices=lambda **kwargs: [] if state.exists() else [{'udid':os.environ['SERIAL'],'device_id':1,'product_id':4776}]
 if int(os.environ['KICKOFF']):
  wait=entry.wait_mode
  def short_wait(*args,**kwargs):
   kwargs['timeout']=0.4
   return wait(*args,**kwargs)
  entry.wait_mode=short_wait
 sys.argv=['helper']+args[2:]
 raise SystemExit(entry.main())
else:sys.exit(99)
''')
            for name in ("ideviceinfo", "ideviceenterrecovery", "irecovery", "managed-python"):
                path = tools / name
                path.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -B " + shlex.quote(str(program))
                                + " " + name + ' "$@"\n')
                path.chmod(0o700)
            detect = function("device_get_info")
            if backend == "legacy":
                detect = detect[:detect.index('    device_fw_dir="../saved/firmware/$device_type"')]
                detect += '\n}\n'
            shell = HOOK + '\n' + '\n'.join((detect, function("device_enter_mode"), function("device_dfuhelper"))) + r'''
log() { printf '%s\n' "$*"; }
print() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*"; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 73; }
device_entry() { echo UNEXPECTED_MANUAL_ENTRY; exit 90; }
device_get_name() { device_name=fixture; }
mkdir() { :; }
device_proc=6
platform=macos
main_argmode=device_enter_ramdisk
device_argmode=entry
device_type="$BL_IOS_MODEL"
ideviceinfo="$dir/ideviceinfo"
irecovery="$dir/irecovery"
device_get_info
device_enter_mode DFU
printf 'ENTRY_DONE:%s:%s:%s\n' "$device_type" "$device_mode" "$device_ecid"
'''
            script = root / "entry.sh"
            script.write_text(shell)
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "dir": str(tools),
                   "PROJECT_ROOT": str(root), "BL_IOS_PYTHON": str(tools / "managed-python"),
                   "BL_IOS_MODEL": selected, "BL_IOS_UDID": SERIAL, "TRACE": str(trace),
                   "STATE": str(state), "QUERIES": str(queries), "VALUES": json.dumps(product),
                   "SERIAL": SERIAL, "BDID": "00" if model == "iPhone5,1" else "02", "KICKOFF": str(kickoff)}
            result = subprocess.run([shutil.which("bash"), str(script)], input="\n", text=True,
                                    capture_output=True, env=env, cwd=root, timeout=15)
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            return result, events

    def test_both_pinned_backends_normal_to_verified_recovery_and_dfu(self):
        for backend in ("recover", "legacy"):
            for model in ("iPhone5,1", "iPhone5,2"):
                with self.subTest(backend=backend, model=model):
                    result, events = self.run_entry(backend, model=model, selected=model)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("ENTRY_DONE:" + model + ":DFU:123", result.stdout)
                    kicks = [args for name, args in events if name == "ideviceenterrecovery"]
                    self.assertEqual(kicks, [[SERIAL]])
                    helper_actions = [args[2] for name,args in events if name == "managed-python"]
                    self.assertEqual(helper_actions, ["detect", "recovery", "dfu"])
                    self.assertTrue(all(args[1] == SERIAL for name,args in events if name == "ideviceinfo"))
                    self.assertGreaterEqual(sum(name == "irecovery" for name,args in events), 6)

    def test_failed_kick_never_enters_dfu_without_verified_recovery(self):
        for backend in ("recover", "legacy"):
            with self.subTest(backend=backend):
                result, events = self.run_entry(backend, kickoff=9)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("ENTRY_DONE", result.stdout)
                self.assertIn("Automatic Recovery entry was not confirmed", result.stderr)
                self.assertIn("did not enter verified Recovery mode", result.stderr)
                self.assertEqual([args for name,args in events if name == "ideviceenterrecovery"], [[SERIAL]])
                self.assertEqual([args[2] for name,args in events if name == "managed-python"], ["detect", "recovery"])

    def test_wrong_selected_variant_stops_before_recovery_command(self):
        for backend in ("recover", "legacy"):
            with self.subTest(backend=backend):
                result, events = self.run_entry(backend, selected="iPhone5,2")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("ENTRY_DONE", result.stdout)
                self.assertFalse(any(name == "ideviceenterrecovery" for name,args in events))
                self.assertIn("iPhone5,1, but --model selects iPhone5,2", result.stderr)
