import io
import json
import plistlib
import stat
import tempfile
import unittest
import zipfile
import hashlib
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from ios_timeout import cli, iphone2g, upstream
from ios_timeout.core import Identity, Plan, ResetError, SYSTEM_VERSION, compatible, make_plan, apply_plan
from ios_timeout.device import USBDevice
from tests.test_core import MemoryDevice

IDENTITY = Identity("a" * 40, "iPhone1,1", "3.1.3", "7E18")
PROBE = '{"schema":1,"product":"iPhone1,1","board":"m68ap"}'


class BareDevice(USBDevice):
    def __init__(self, model="iPhone1,1", version="3.1.3", options="read-only", initially_mounted=True):
        self.selected = {"udid": IDENTITY.udid}
        self.model = model
        self.version = version
        self.options = options
        self.mounted = {"1", "2"} if initially_mounted else set()
        self.commands = []
        self.sftp = Mock()
        self.sftp.lstat.return_value.st_mode = stat.S_IFDIR | 0o755

    def command(self, command, timeout=30):
        self.commands.append(command)
        if command == iphone2g.HELPER + " --identity":
            if self.model != "iPhone1,1":
                raise ResetError("Wrong actual kernel identity")
            return PROBE
        if command == "/sbin/mount":
            return "/dev/md0 on / (hfs, local)\n" + "".join(
                f"/dev/disk0s{n} on /mnt{n} (hfs, local, {self.options})\n" for n in sorted(self.mounted))
        if command.startswith("if test -x /bin/ps"):
            return "/sbin/launchd\n/usr/sbin/sshd"
        if command.startswith("/sbin/mount_hfs "):
            self.options = "read-only" if "-o rdonly " in command else ""
            self.mounted.add(command[-1])
            return ""
        if command.startswith("test -b "):
            return ""
        raise ResetError("Missing command")

    def read(self, path):
        assert path == SYSTEM_VERSION
        return plistlib.dumps({"ProductVersion": self.version, "ProductBuildVersion": "7E18"}), None


class OriginalIPhoneTests(unittest.TestCase):
    def test_range_and_cli_include_original_iphone_but_reject_unproven_versions(self):
        for version in ("2.2", "2.2.1", "3.0", "3.1.3"):
            self.assertEqual(compatible("iPhone1,1", version), "iPhone 2G")
        for version in ("1.0", "1.1.4", "2.0", "2.1", "3.1.4", "4.0"):
            with self.subTest(version=version), self.assertRaises(ResetError):
                compatible("iPhone1,1", version)
        self.assertEqual(cli.parser().parse_args(["prepare", "--model", "iPhone1,1"]).model, "iPhone1,1")

    def test_bare_ramdisk_requires_real_hardware_probe_not_selected_model_or_flat_layout(self):
        device = BareDevice()
        self.assertEqual(device.inspect(expected_model="iPhone1,1", read_only=True), IDENTITY)
        with self.assertRaisesRegex(ResetError, "Cannot identify"):
            BareDevice(model="iPhone1,2").inspect(expected_model="iPhone1,1")
        self.assertNotIn("test -b /dev/disk0s1", device.commands)

    def test_read_only_recovery_mounts_exact_flat_volumes_without_repair(self):
        device = BareDevice(initially_mounted=False)
        self.assertEqual(device.inspect(mount=True, expected_model="iPhone1,1", read_only=True), IDENTITY)
        mounts = [c for c in device.commands if c.startswith("/sbin/mount_hfs")]
        self.assertEqual(mounts, ["/sbin/mount_hfs -o rdonly /dev/disk0s1 /mnt1",
                                  "/sbin/mount_hfs -o rdonly /dev/disk0s2 /mnt2"])
        self.assertFalse(any("fsck" in c for c in device.commands))

    def test_passcode_inspection_refuses_existing_writable_mount_and_unproven_os(self):
        with self.assertRaisesRegex(ResetError, "read-only volumes"):
            BareDevice(options="").inspect(expected_model="iPhone1,1", read_only=True)
        with self.assertRaisesRegex(ResetError, "range"):
            BareDevice(version="1.0").inspect(expected_model="iPhone1,1", read_only=True)

    def test_2g_writable_inspection_stops_before_mount_even_without_selected_model(self):
        device = BareDevice(initially_mounted=False)
        with patch.object(device, "_probe_model", return_value="iPhone1,1"):
            with self.assertRaisesRegex(ResetError, "Timer reset is not supported"):
                device.inspect(mount=True, writable=True)
        self.assertEqual(device.commands, [])

    def test_unproven_2g_timer_mutations_are_rejected_before_reads_or_locks(self):
        device = MemoryDevice(journal=False)
        original = dict(device.files)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ResetError, "Timer reset is not supported"):
                make_plan(device, IDENTITY)
            with self.assertRaisesRegex(ResetError, "Timer reset is not supported"):
                apply_plan(device, Plan(IDENTITY, [], []), Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual(device.files, original)
        self.assertIsNone(device.lock)

    def test_bundled_helper_verifies_and_extracts_but_never_overwrites(self):
        data = iphone2g.read_helper()
        self.assertGreater(len(data), 1000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "helper"
            self.assertEqual(iphone2g.extract_helper(path), path)
            self.assertEqual(path.read_bytes(), data)
            with self.assertRaises(FileExistsError):
                iphone2g.extract_helper(path)

    def test_bundled_helper_source_and_validation_match_every_native_source(self):
        from importlib import resources
        directory = resources.files("ios_timeout").joinpath("data")
        manifest = json.loads(directory.joinpath("iphone2g-helper.json").read_bytes())
        with zipfile.ZipFile(io.BytesIO(directory.joinpath("iphone2g-source.zip").read_bytes())) as archive:
            prefix = "third_party/iphone2g-helper/"
            validation = json.loads(archive.read(prefix + "VALIDATION.json"))
            self.assertEqual(validation["payload"]["sha256"], manifest["sha256"])
            self.assertEqual(validation["sources"], manifest["sources"])
            self.assertEqual(set(manifest["sources"]), {"openup-iphone2g.c", "bootstrap.c", "ssh-owner.c"})
            for name, digest in manifest["sources"].items():
                self.assertEqual(hashlib.sha256(archive.read(prefix + name)).hexdigest(), digest)
            self.assertTrue(any(name.endswith("licenses/BSD-3-Clause.txt") for name in archive.namelist()))

    def test_damaged_or_wrong_build_helper_is_rejected(self):
        from importlib import resources
        source = resources.files("ios_timeout").joinpath("data")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = source.joinpath("iphone2g-helper.zip").read_bytes()
            manifest = json.loads(source.joinpath("iphone2g-helper.json").read_bytes())
            (root / "iphone2g-helper.zip").write_bytes(archive)
            for field, value in (("product", "iPhone1,2"), ("ramdisk_build", "8C148"), ("sha256", "0" * 64)):
                (root / "iphone2g-helper.json").write_text(json.dumps({**manifest, field: value}))
                with self.subTest(field=field), self.assertRaises(ResetError):
                    iphone2g.read_helper(root)
            (root / "iphone2g-helper.json").write_text(json.dumps(manifest))
            (root / "iphone2g-helper.zip").write_bytes(archive + b"changed")
            with self.assertRaises(ResetError):
                iphone2g.read_helper(root)

    def device(self):
        device = Mock()
        device.inspect.return_value = IDENTITY
        device.exists.return_value = False
        device.read.return_value = (b"synthetic database", None)
        device.command.side_effect = lambda c, **kw: PROBE if "--identity" in c else "ab" * 16
        return device

    def test_passcode_reader_uses_only_expected_database_and_never_logs_key(self):
        device = self.device()
        output = io.StringIO()
        with patch("ios_timeout.legacy_keychain.recover_passcode", return_value="1234") as recover, redirect_stdout(output):
            self.assertEqual(iphone2g.recover_from_device(device, IDENTITY), "1234")
        recover.assert_called_once_with(b"synthetic database", bytes.fromhex("ab" * 16), installed_version="3.1.3")
        device.read.assert_called_once_with(iphone2g.KEYCHAIN)
        device.inspect.assert_called_once_with(expected_model="iPhone1,1", read_only=True)
        self.assertEqual(output.getvalue(), "")

    def test_pending_journal_or_identity_change_stops_before_reading_key(self):
        for scenario in ("journal", "identity"):
            device = self.device()
            if scenario == "journal": device.exists.return_value = True
            else: device.inspect.return_value = Identity("b" * 40, "iPhone1,1", "3.1.3", "7E18")
            with self.subTest(scenario=scenario), self.assertRaises(ResetError):
                iphone2g.recover_from_device(device, IDENTITY)
            device.read.assert_not_called()
            self.assertFalse(any("--key835" in str(c) for c in device.command.call_args_list))

    def test_invalid_hardware_key_never_reaches_parser(self):
        for key in ("", "00" * 16, "01" * 16, "ab" * 17, "unavailable"):
            device = self.device()
            device.command.side_effect = lambda c, **kw: PROBE if "--identity" in c else key
            with patch("ios_timeout.legacy_keychain.recover_passcode") as parser:
                with self.subTest(key=key), self.assertRaises(ResetError):
                    iphone2g.recover_from_device(device, IDENTITY)
                parser.assert_not_called()

    def test_2g_recovery_dispatch_cannot_fall_through_to_ios4_payloads(self):
        args = Namespace(model="iPhone1,1")
        with patch.object(iphone2g, "run_recovery", return_value=0) as run, patch.object(upstream, "read_bundled_payloads") as payloads:
            self.assertEqual(upstream.run_backend(args), 0)
        run.assert_called_once_with(args)
        payloads.assert_not_called()

    def test_preview_does_not_download_or_touch_device(self):
        args = Namespace(model="iPhone1,1", apply=False, install_only=False, armv6_tools=None)
        with patch("ios_timeout.cli.launch_backend") as boot, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(iphone2g.run_recovery(args), 0)
        boot.assert_not_called()
        self.assertIn("2.2–3.1.3", out.getvalue())

    def test_custom_3g_payload_override_is_rejected_before_boot(self):
        args = Namespace(model="iPhone1,1", apply=True, install_only=False, armv6_tools=Path("custom"))
        with patch("ios_timeout.cli.launch_backend") as boot, self.assertRaisesRegex(ResetError, "cannot run"):
            iphone2g.run_recovery(args)
        boot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
