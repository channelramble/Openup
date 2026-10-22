import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ios_timeout import cli
from ios_timeout.core import ResetError
from ios_timeout import legacy_kit


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="itr path with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.kit = Path(self.temp.name) / "backend"
        self.args = cli.parser().parse_args(["prepare", "--kit", str(self.kit)])
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        if command[:2] == ["git", "clone"]:
            folder = Path(command[-1])
            folder.mkdir()
            (folder / "resources").mkdir()
            (folder / "restore.sh").write_text("#!/bin/bash\n")
        stdout = cli.KIT_COMMIT + "\n" if "rev-parse" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    def test_pinned_backend_download_and_argument_boundaries(self):
        with patch("ios_timeout.cli.subprocess.run", side_effect=self.fake_run), \
             patch("ios_timeout.cli.shutil.which", return_value="/usr/bin/git"), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()):
            cli.launch_backend(self.args)
        self.assertTrue((self.kit / "restore.sh").is_file())
        self.assertEqual(self.calls[1][-2:], ["--detach", cli.KIT_COMMIT])
        # On macOS the kit is told not to resume Apple's device agents on exit;
        # Openup's guardian owns suspending and restoring them.
        self.assertEqual(self.calls[-1], [
            "bash", str(self.kit.resolve() / "restore.sh"),
            "--sshrd", "--no-version-check", "--no-color", "--no-finder",
        ])
        self.assertTrue(self.args.mount)

    def test_failed_download_cleans_partial_checkout(self):
        def failure(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            result.returncode = 1
            return result
        with patch("ios_timeout.cli.subprocess.run", side_effect=failure), \
             patch("ios_timeout.cli.shutil.which", return_value="/usr/bin/git"), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()), self.assertRaises(ResetError):
            cli.launch_backend(self.args)
        self.assertFalse(self.kit.exists())
        self.assertEqual(list(self.kit.parent.iterdir()), [])

    def test_desktop_backend_uses_numbered_menus_instead_of_cursor_queries(self):
        with patch.dict(os.environ, {"BL_IOS_UI_PROTOCOL": "1"}), \
             patch("ios_timeout.cli.subprocess.run", side_effect=self.fake_run), \
             patch("ios_timeout.cli.shutil.which", return_value="/usr/bin/git"), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()):
            cli.launch_backend(self.args)
        self.assertIn("--old-menu", self.calls[-1])

    def test_existing_backend_is_not_replaced(self):
        self.kit.mkdir()
        (self.kit / "resources").mkdir()
        (self.kit / "restore.sh").write_text("#!/bin/bash\n# user checkout\n")
        with patch("ios_timeout.cli.subprocess.run", side_effect=self.fake_run), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()):
            cli.launch_backend(self.args)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.kit / "restore.sh").read_text().endswith("# user checkout\n"))

    def test_backend_failure_stops_prepare(self):
        self.kit.mkdir()
        (self.kit / "resources").mkdir()
        (self.kit / "restore.sh").write_text("#!/bin/bash\n")
        with patch("ios_timeout.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 1)), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()), self.assertRaises(ResetError):
            cli.launch_backend(self.args)

    def test_unknown_custom_3g_backend_is_not_executed_or_modified(self):
        self.kit.mkdir()
        (self.kit / "resources").mkdir()
        script = self.kit / "restore.sh"
        original = b"#!/bin/bash\n# user checkout\n"
        script.write_bytes(original)
        self.args.model = "iPhone1,2"
        with patch("ios_timeout.cli.subprocess.run") as run, \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()), self.assertRaisesRegex(ResetError, "reviewed version"):
            cli.launch_backend(self.args)
        run.assert_not_called()
        self.assertEqual(script.read_bytes(), original)
        self.assertEqual(list(self.kit.glob(".bl-ios-prepare-*")), [])

    def test_3g_temporary_adapter_cleanup_after_backend_failure(self):
        self.kit.mkdir()
        (self.kit / "resources").mkdir()
        script = self.kit / "restore.sh"
        original = b"#!/bin/bash\n# unchanged checkout\n"
        script.write_bytes(original)
        self.args.model = "iPhone1,2"
        adapted = []

        def run(command, **kwargs):
            if command[1] == "-n":
                return subprocess.CompletedProcess(command, 0)
            path = Path(command[1])
            self.assertEqual(path.parent, self.kit.resolve())
            self.assertNotEqual(path, script)
            self.assertEqual(path.read_bytes(), b"#!/bin/bash\nexit 9\n")
            self.assertEqual(kwargs["env"]["BL_IOS_PYTHON"], sys.executable)
            adapted.append(path)
            return subprocess.CompletedProcess(command, 9)

        with patch("ios_timeout.legacy_kit.adapt_script", return_value=b"#!/bin/bash\nexit 9\n"), \
             patch("ios_timeout.cli.subprocess.run", side_effect=run), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()), self.assertRaisesRegex(ResetError, "status 9"):
            cli.launch_backend(self.args)
        self.assertEqual(script.read_bytes(), original)
        self.assertEqual(len(adapted), 1)
        self.assertFalse(adapted[0].exists())

    def test_prepare_reserves_mac_device_agents_around_the_boot(self):
        import contextlib
        events = []

        @contextlib.contextmanager
        def fake_reserve():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        state = Path(self.temp.name) / "state"
        args = cli.parser().parse_args(
            ["prepare", "--kit", str(self.kit), "--state-dir", str(state)])
        # The device-agent reservation must wrap the backend boot and the
        # ramdisk SSH session, so Apple's agents cannot grab the phone first.
        with patch("ios_timeout.macos_agents.recovery_agents", fake_reserve), \
             patch("ios_timeout.cli.launch_backend",
                   side_effect=lambda a: events.append("launch_backend")), \
             patch("ios_timeout.usbmux.wait_for_device",
                   side_effect=ResetError("no device")), \
             redirect_stdout(io.StringIO()), self.assertRaises(ResetError):
            cli.run(args)
        self.assertEqual(events, ["enter", "launch_backend", "exit"])


class LegacyKitIdentityTests(unittest.TestCase):
    @staticmethod
    def query(mode="DFU", chip="8900", board="04", srtg="iBoot-636.66.3x"):
        return (f"MODE: {mode}\nCPID: {chip}\nBDID: {board}\n"
                f"PRODUCT: iPhone1,2\nMODEL: n82ap\nSRTG: {srtg}\n")

    def test_generic_wtf_selects_only_download_profile(self):
        recovery = self.query(mode="WTF", board="00", srtg="N/A")
        self.assertEqual(
            legacy_kit.select_3g_identity("prepare", "WTF", "iPhone1,1", "m68ap", recovery),
            ("iPhone1,2", "n82"),
        )
        with self.assertRaises(ResetError):
            legacy_kit.select_3g_identity("upload", "WTF", "iPhone1,1", "m68ap", recovery)

    def test_real_3g_dfu_ids_override_generic_name(self):
        self.assertEqual(legacy_kit.select_3g_identity(
            "upload", "DFU", "iPhone1,1", "m68ap", self.query()), ("iPhone1,2", "n82"))

    def test_other_board_or_unpatched_dfu_stops_before_upload(self):
        for recovery in (self.query(board="00"), self.query(chip="8920"),
                         self.query(srtg="iBoot-596.24"), self.query(mode="Recovery"),
                         self.query().replace("BDID: 04\n", ""),
                         self.query(chip="invalid"), ""):
            with self.subTest(recovery=recovery), self.assertRaises(ResetError):
                legacy_kit.select_3g_identity("upload", "DFU", "iPhone1,2", "n82", recovery)

    def test_normal_mode_checks_actual_product_and_board(self):
        self.assertEqual(legacy_kit.select_3g_identity(
            "prepare", "Normal", "iPhone1,2", "N82AP", ""), ("iPhone1,2", "n82"))
        for product, board in (("iPhone1,1", "m68ap"), ("iPhone1,2", "m68ap"), ("", "")):
            with self.subTest(product=product, board=board), self.assertRaises(ResetError):
                legacy_kit.select_3g_identity("prepare", "Normal", product, board, "")


@unittest.skipUnless(os.environ.get("BL_IOS_LEGACY_SCRIPT") and shutil.which("bash") and os.name != "nt",
                     "Set BL_IOS_LEGACY_SCRIPT to the reviewed Legacy iOS Kit restore.sh")
class LegacyKitPinnedShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = Path(os.environ["BL_IOS_LEGACY_SCRIPT"]).read_bytes()
        cls.adapted = legacy_kit.adapt_script(cls.original).decode()

    def run_identity_prefix(self, source, query):
        # Run the actual pinned detection and both board/product fallback tables,
        # stopping before firmware metadata lookup. All USB calls are fake.
        start = source.index("device_get_info() {\n")
        end = source.index('    device_fw_dir="../saved/firmware/$device_type"', start)
        prefix = source[start:end] + '    printf "RESULT:%s:%s\\n" "$device_type" "$device_model"\n}\n'
        with tempfile.TemporaryDirectory(prefix="legacy shell path ") as temporary:
            info = Path(temporary) / "ideviceinfo"
            info.write_text("#!/bin/sh\nexit 1\n")
            info.chmod(0o700)
            shell = legacy_kit.HOOK + "\n" + prefix + "\n" + r'''
log() { :; }
print() { :; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 9; }
device_entry() { :; }
device_get_name() { device_name="fixture"; }
device_manufacturing() { :; }
stub_recovery() { printf '%s\n' "$QUERY"; }
irecovery=stub_recovery
platform=macos
device_argmode=entry
device_type=iPhone1,2
main_argmode=device_enter_ramdisk
device_get_info
'''
            env = {**os.environ, "QUERY": query, "ideviceinfo": str(info),
                   "BL_IOS_PYTHON": sys.executable,
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            return subprocess.run(["bash", "-c", shell], env=env, capture_output=True,
                                  text=True, timeout=15)

    def test_reproduces_m68_regression_and_selects_correct_n82_profile(self):
        query = LegacyKitIdentityTests.query(mode="WTF", board="00", srtg="N/A")
        query = query.replace("PRODUCT: iPhone1,2", "PRODUCT: iPhone1,1").replace("MODEL: n82ap", "MODEL: m68ap")
        before = self.run_identity_prefix(self.original.decode(), query)
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertIn("RESULT:iPhone1,1:m68", before.stdout)
        after = self.run_identity_prefix(self.adapted, query)
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertIn("RESULT:iPhone1,2:n82", after.stdout)

    def test_real_dfu_wrong_board_never_reaches_firmware_selection(self):
        result = self.run_identity_prefix(self.adapted, LegacyKitIdentityTests.query(board="00"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("RESULT:", result.stdout)
        self.assertIn("identity verification failed", result.stderr)

    def test_actual_adapted_shell_syntax_and_upload_boundaries(self):
        result = subprocess.run(["bash", "-n"], input=self.adapted, text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        start = self.adapted.index("device_ramdisk() {\n")
        end = self.adapted.index("\ndevice_", start + 1)
        section = self.adapted[start:end]
        self.assertLess(section.index("bl_ios_legacy_identity upload"),
                        section.index('$irecovery -f iBSS'))
        self.assertIn("device_find_mode Recovery 15", section)
        self.assertIn('if [[ $platform == "linux" ]]; then\n        device_find_mode Recovery 24', section)
        for line in section.splitlines():
            if line.strip().startswith(("$irecovery -f ", "$irecovery -c ")):
                self.assertIn('|| error "Ramdisk USB command failed', line)

    def test_modified_upstream_rejected(self):
        with self.assertRaisesRegex(ResetError, "reviewed version"):
            legacy_kit.adapt_script(self.original + b"\n")

    def test_actual_wtf_transition_rechecks_real_identity(self):
        start = self.adapted.index("device_s5l8900xall() {\n")
        end = self.adapted.index("\ndevice_get_info()", start)
        shell = legacy_kit.HOOK + "\n" + self.adapted[start:end] + "\n" + r'''
log() { :; }
print() { :; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 9; }
stub_sha() { echo "cb96954185a91712c47f20adb519db45a318c30f file"; }
stub_patch() { printf patched > "$2"; }
download_with_pzb() { exit 99; }
stub_recovery() {
    if [[ $1 == "-q" ]]; then printf '%s\n' "$QUERY"
    elif [[ $1 == "-f" ]]; then echo SHARED_WTF_UPLOAD
    else exit 98
    fi
}
device_find_mode() { device_mode=DFU; }
device_get_name() { :; }
irecovery=stub_recovery
sha1sum=stub_sha
bspatch=stub_patch
platform=macos
device_type=iPhone1,2
device_model=n82
device_mode=WTF
device_s5l8900xall
echo TRANSITION_VERIFIED
'''
        for query, succeeds in ((LegacyKitIdentityTests.query(), True),
                                (LegacyKitIdentityTests.query(board="00"), False),
                                (LegacyKitIdentityTests.query(srtg="wrong"), False)):
            with self.subTest(query=query), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary) / "work"
                work.mkdir()
                env = {**os.environ, "QUERY": query, "BL_IOS_PYTHON": sys.executable,
                       "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
                result = subprocess.run(["bash", "-c", shell], env=env, cwd=work,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                self.assertEqual("TRANSITION_VERIFIED" in result.stdout, succeeds)
                self.assertEqual(result.stdout.count("SHARED_WTF_UPLOAD"), 1)

    def test_no_version_check_with_new_marker_never_updates(self):
        start = self.adapted.index("version_check() {\n")
        end = self.adapted.index("\n}\n", start) + 3
        shell = self.adapted[start:end] + "\n" + r'''
warn() { printf '%s\n' "$*"; }
version_update_check() { echo UNEXPECTED_CHECK; exit 97; }
version_update() { echo UNEXPECTED_UPDATE; exit 98; }
no_version_check=1
version_check
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "resources").mkdir()
            (root / "resources/new").write_text("marker")
            (root / "work").mkdir()
            result = subprocess.run(["bash", "-c", shell], cwd=root / "work",
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNEXPECTED", result.stdout)
        self.assertIn("update check is disabled", result.stdout)

    def test_actual_recovery_poll_is_bounded_and_reports_usb_state(self):
        start = self.adapted.index("device_find_mode() {\n")
        end = self.adapted.index("\n}\n", start) + 3
        shell = legacy_kit.HOOK + "\n" + self.adapted[start:end] + "\n" + r'''
log() { :; }
warn() { printf '%s\n' "$*"; }
print() { printf '%s\n' "$*"; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 9; }
sleep() { printf S >> "$TRACE"; }
stub_recovery() {
    printf Q >> "$TRACE"
    printf 'MODE: DFU\nCPID: 8900\nBDID: 04\nMODEL: n82ap\nSRTG: iBoot-636.66.3x\nECID: private-id\n'
}
irecovery=stub_recovery
platform=macos
device_proc=1
device_find_mode Recovery 15
echo UNEXPECTED_CONTINUATION
'''
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace"
            result = subprocess.run(["bash", "-c", shell], env={**os.environ, "TRACE": str(trace)},
                                    capture_output=True, text=True, timeout=15)
            calls = trace.read_text()
        self.assertEqual(result.returncode, 9)
        self.assertEqual(calls.count("Q"), 16)
        self.assertEqual(calls.count("S"), 15)
        self.assertIn("MODE: DFU", result.stdout)
        self.assertIn("BDID: 04", result.stdout)
        self.assertNotIn("private-id", result.stdout)
        self.assertNotIn("UNEXPECTED_CONTINUATION", result.stdout)
        self.assertIn("No timer reset was attempted", result.stderr)

    def test_actual_ramdisk_upload_failure_stops_before_later_commands(self):
        start = self.adapted.index("device_ramdisk() {\n")
        start = self.adapted.index("    bl_ios_legacy_identity upload\n", start)
        end = self.adapted.index('    log "Booting, please wait..."', start)
        shell = legacy_kit.HOOK + "\nrun_uploads() {\n" + self.adapted[start:end] + "\n}\n" + r'''
log() { :; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 9; }
sleep() { :; }
stub_recovery() {
    if [[ $1 == "-q" ]]; then printf '%s\n' "$QUERY"; return; fi
    printf '%s\n' "$*" >> "$TRACE"
    return 7
}
device_find_mode() { echo UNEXPECTED_POLL; exit 98; }
irecovery=stub_recovery
device_mode=DFU
device_type=iPhone1,2
device_model=n82
device_proc=1
build_id=7E18
platform=macos
run_uploads
echo UNEXPECTED_CONTINUATION
'''
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace"
            env = {**os.environ, "TRACE": str(trace),
                   "QUERY": LegacyKitIdentityTests.query(), "BL_IOS_PYTHON": sys.executable,
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            result = subprocess.run(["bash", "-c", shell], env=env, capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(trace.read_text().splitlines(), ["-f iBSS"])
        self.assertEqual(result.returncode, 9)
        self.assertIn("Ramdisk USB command failed", result.stderr)
        self.assertNotIn("UNEXPECTED", result.stdout)

    def test_sshrd_menu_auto_exits_for_openup_but_stays_interactive_otherwise(self):
        start = self.adapted.index("menu_ramdisk() {\n")
        end = self.adapted.index("\n}\n", start) + 3
        shell = self.adapted[start:end] + "\n" + r'''
log()   { printf 'LOG:%s\n' "$*"; }
print() { printf 'PRINT:%s\n' "$*"; }
warn()  { printf 'WARN:%s\n' "$*"; }
input() { printf 'INPUT:%s\n' "$*"; }
# Return the index of the final "Exit" item so the interactive loop ends.
select_option() { printf 'SELECT_OPTION_CALLED\n'; return $(($# - 1)); }
ssh=:
device_proc=4
device_ramdisk_ios8=0
ssh_port=2222
menu_ramdisk
printf 'RET:%s\n' "$?"
'''
        # Openup drives the backend non-interactively: the ramdisk boots and the
        # menu returns as if Exit were chosen, with no on-screen option to pick.
        auto = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, timeout=15,
                              env={**os.environ, "BL_IOS_SSHRD_AUTOEXIT": "1"})
        self.assertEqual(auto.returncode, 0, auto.stderr)
        self.assertIn("RET:0", auto.stdout)
        self.assertNotIn("SELECT_OPTION_CALLED", auto.stdout)
        self.assertNotIn("INPUT:Select an option", auto.stdout)
        # Without the flag, the stock interactive SSH Ramdisk Menu still runs.
        manual_env = {key: value for key, value in os.environ.items()
                      if key != "BL_IOS_SSHRD_AUTOEXIT"}
        manual = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, timeout=15,
                                env=manual_env)
        self.assertEqual(manual.returncode, 0, manual.stderr)
        self.assertIn("SELECT_OPTION_CALLED", manual.stdout)
        self.assertIn("INPUT:Select an option:", manual.stdout)

    def test_menu_only_adaptation_has_gui_handling_without_3g_identity_hooks(self):
        menu_only = legacy_kit.adapt_menu_only(self.original).decode()
        self.assertIn("BL_IOS_SSHRD_AUTOEXIT", menu_only)
        self.assertIn('bl_ios_ui prompt choice "Choose the next step"', menu_only)
        # Non-3G models get the shared prompt/menu handling but not the 3G hooks.
        self.assertNotIn("bl_ios_legacy_identity", menu_only)
        self.assertIn("bl_ios_legacy_identity", self.adapted)
        syntax = subprocess.run(["bash", "-n"], input=menu_only, text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        with self.assertRaisesRegex(ResetError, "reviewed version"):
            legacy_kit.adapt_menu_only(self.original + b"\n")

    def test_prepare_adapts_non_3g_pinned_kit_for_gui_menu_handling(self):
        with tempfile.TemporaryDirectory(prefix="kit menu only ") as temporary:
            kit = Path(temporary) / "Legacy-iOS-Kit"
            (kit / "resources").mkdir(parents=True)
            (kit / "restore.sh").write_bytes(self.original)
            args = cli.parser().parse_args(
                ["prepare", "--kit", str(kit), "--model", "iPhone4,1"])
            ran = []

            def fake_run(command, **kwargs):
                if command[:2] == ["bash", "-n"]:
                    return subprocess.CompletedProcess(command, 0)
                ran.append((list(command), dict(kwargs.get("env", {}))))
                return subprocess.CompletedProcess(command, 0)

            with patch("ios_timeout.cli.subprocess.run", side_effect=fake_run), \
                 patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
                 redirect_stdout(io.StringIO()):
                cli.launch_backend(args)
            command, env = ran[-1]
            script_arg = Path(command[1])
            self.assertEqual(script_arg.parent, kit.resolve())
            self.assertNotEqual(script_arg.name, "restore.sh")
            self.assertIn("--device=iPhone4,1", command)
            self.assertEqual(env.get("BL_IOS_SSHRD_AUTOEXIT"), "1")
            # The user's checkout is never modified, and no temp file lingers.
            self.assertEqual((kit / "restore.sh").read_bytes(), self.original)
            self.assertEqual(list(kit.glob(".bl-ios-prepare-*")), [])
