import io
import plistlib
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ios_timeout import cli, usbmux
from ios_timeout.backend_support import check_payloads, macho_architectures, resolve_identity
from ios_timeout.core import ResetError, compatible
from ios_timeout.firmware import validate_ipsw, extract_component
from ios_timeout.upstream import adapt_script
from ios_timeout.wsl import build_runner, translated_arguments
from tests import test_backend


class CompatibilityTests(unittest.TestCase):
    def test_3g_board_is_resolved_from_chip_and_board_ids(self):
        self.assertEqual(
            resolve_identity("iPhone1,1", "m68", "iPhone1,2", "CPID: 0x8900\nBDID: 0x04\n"),
            ("iPhone1,2", "n82"),
        )

    def test_wrong_physical_device_cannot_be_forced_to_3g(self):
        with self.assertRaises(ResetError):
            resolve_identity("iPhone1,1", "m68", "iPhone1,2", "CPID: 0x8900\nBDID: 0x00\n")

    def test_inconsistent_board_without_hardware_ids_is_rejected(self):
        with self.assertRaises(ResetError):
            resolve_identity("iPhone1,2", "m68")
        self.assertEqual(resolve_identity("iPhone1,2", "N82AP"), ("iPhone1,2", "n82"))

    def test_3g_ios4_gate(self):
        compatible("iPhone1,2", "4.2.1")
        for version in ("5.0", "3.1.3", "4.2.2"):
            with self.assertRaises(ResetError):
                compatible("iPhone1,2", version)

    def test_armv7_payloads_are_rejected_for_3g(self):
        with tempfile.TemporaryDirectory() as folder:
            data = struct.pack("<IIIIIII", 0xFEEDFACE, 12, 9, 2, 0, 0, 0)
            for name in ("bruteforce", "device_infos", "restored_external"):
                (Path(folder) / name).write_bytes(data)
            check_payloads("iPhone3,1", folder)
            with self.assertRaisesRegex(ResetError, "ARMv6"):
                check_payloads("iPhone1,2", folder)
            for name in ("bruteforce", "device_infos", "restored_external"):
                (Path(folder) / name).write_bytes(struct.pack("<IIIIIII", 0xFEEDFACE, 12, 6, 2, 0, 0, 0))
            check_payloads("iPhone1,2", folder)

    def test_malformed_executables_are_rejected(self):
        for data in (b"", b"x" * 30, b"\xca\xfe\xba\xbe" + b"\xff" * 40):
            with self.assertRaises(ResetError):
                macho_architectures(data)

    def test_unknown_upstream_revision_is_not_patched(self):
        with self.assertRaisesRegex(ResetError, "changed"):
            adapt_script(b"#!/bin/bash\nunknown source\n")

    def test_local_ipsw_checks_model_build_and_image(self):
        with tempfile.TemporaryDirectory() as folder:
            ipsw = Path(folder) / "phone.ipsw"
            member = "Firmware/dfu/iBSS.n82ap.RELEASE.dfu"
            with zipfile.ZipFile(ipsw, "w") as archive:
                archive.writestr("Restore.plist", plistlib.dumps({
                    "ProductType": "iPhone1,2", "ProductBuildVersion": "7E18",
                }))
                archive.writestr(member, b"test firmware" * 10)
            self.assertEqual(validate_ipsw(ipsw, "iPhone1,2", "7E18"), member)
            for model, build in (("iPhone1,1", "7E18"), ("iPhone1,2", "8C148")):
                with self.assertRaises(ResetError):
                    validate_ipsw(ipsw, model, build)
            destination = Path(folder) / "image"
            extract_component(ipsw, member, destination)
            self.assertEqual(destination.read_bytes(), b"test firmware" * 10)

    def test_windows_usb_transport_uses_loopback_tcp(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        errors = []
        def server():
            try:
                with listener.accept()[0] as peer:
                    header = usbmux.recv_exact(peer, 16)
                    length, _, _, tag = usbmux.HEADER.unpack(header)
                    message = plistlib.loads(usbmux.recv_exact(peer, length - 16))
                    self.assertEqual(message["MessageType"], "ListDevices")
                    body = plistlib.dumps({"DeviceList": [{
                        "DeviceID": 1, "Properties": {"ConnectionType": "USB", "SerialNumber": "a" * 40},
                    }]})
                    peer.sendall(usbmux.HEADER.pack(16 + len(body), 1, 8, tag) + body)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        try:
            devices = usbmux.list_devices(listener.getsockname())
            self.assertEqual(devices[0]["udid"], "a" * 40)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            with patch("ios_timeout.usbmux.sys.platform", "win32"):
                self.assertEqual(usbmux.default_endpoint(), ("127.0.0.1", 27015))
        finally:
            listener.close()

    def test_wsl_arguments_preserve_spaces_and_metacharacters(self):
        arguments = ["prepare", "--model", "iPhone1,2", "--kit=C:\\a b\\$(name)",
                     "--wsl-distro", "Ubuntu", "--apply"]
        self.assertEqual(translated_arguments(arguments, lambda path: "/translated/" + path), [
            "prepare", "--model", "iPhone1,2", "--kit", "/translated/C:\\a b\\$(name)", "--apply",
        ])

    def test_wsl_runner_contains_modules_and_dependency_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = Path(folder) / "runner.pyz"
            build_runner(runner)
            with zipfile.ZipFile(runner) as archive:
                self.assertIn("ios_timeout/upstream.py", archive.namelist())
                self.assertIn("ios_timeout/runtime-requirements.txt", archive.namelist())
                self.assertNotIn("requirements-lock.txt", archive.namelist())

    def test_wsl_recovery_translates_backup_but_not_device_identifier(self):
        result = translated_arguments([
            "restore", "--wsl", "--udid", "abcd1234", "C:\\Backups\\a b", "--apply",
        ], lambda path: "/mnt/" + path)
        self.assertEqual(result, [
            "restore", "--udid", "abcd1234", "/mnt/C:\\Backups\\a b", "--apply",
        ])


class LinuxBackendTests(unittest.TestCase):
    setUp = test_backend.BackendTests.setUp
    fake_run = test_backend.BackendTests.fake_run
    def test_3g_linux_preparation_uses_n82_device_and_matching_build(self):
        self.args.model = "iPhone1,2"
        with patch("ios_timeout.cli.subprocess.run", side_effect=self.fake_run), \
             patch("ios_timeout.cli.shutil.which", return_value="/usr/bin/git"), \
             patch("ios_timeout.cli.platform.system", return_value="Linux"), \
             patch("ios_timeout.legacy_kit.adapt_script", return_value=b"#!/bin/bash\n"), \
             redirect_stdout(io.StringIO()):
            cli.launch_backend(self.args)
        self.assertEqual(self.calls[-1][-2:], ["--device=iPhone1,2", "--build-id=7E18"])
