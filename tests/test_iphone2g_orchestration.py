"""Synthetic-only coverage for the normal-phone to RAM-session handoff."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from ios_timeout import cli, iphone2g, upstream
from ios_timeout.core import Identity, ResetError
from tests.test_legacy_keychain import KEY, KNOWN_RECORD


NORMAL_SERIAL = "a" * 40
RAM_SERIAL = "b" * 40
IDENTITY = Identity(RAM_SERIAL, "iPhone1,1", "3.1.3", "7E18")


class OriginalIPhoneOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(directory)
        database = root / "synthetic.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE tversion(version INTEGER)")
            connection.execute("INSERT INTO tversion VALUES(3)")
            connection.execute("CREATE TABLE genp(acct TEXT, svce TEXT, agrp TEXT, data BLOB)")
            connection.execute("INSERT INTO genp VALUES(?, ?, ?, ?)",
                               ("DeviceLockPassword", "SpringBoard", "apple", KNOWN_RECORD))
            connection.commit()
        self.args = cli.parser().parse_args([
            "recover", "--model", "iPhone1,1", "--udid", NORMAL_SERIAL,
            "--state-dir", str(root / "state"), "--backend-dir", str(root / "kit"), "--apply",
        ])
        self.selected = {"udid": RAM_SERIAL, "device_id": 42}
        self.device = MagicMock()
        self.device.inspect.return_value = IDENTITY
        self.device.exists.return_value = False
        self.device.read.return_value = (database.read_bytes(), None)

        def command(value, **kwargs):
            self.assertEqual(kwargs, {"timeout": 15})
            if value == iphone2g.HELPER + " --identity":
                return '{"schema":1,"product":"iPhone1,1","board":"m68ap"}'
            if value == iphone2g.HELPER + " --key835":
                return KEY.hex()
            self.fail("Unexpected device command: " + value)

        self.device.command.side_effect = command
        self.helper = self.stack.enter_context(patch.object(iphone2g, "read_helper", return_value=b"verified fixture"))
        self.boot = self.stack.enter_context(patch.object(cli, "launch_backend", return_value=RAM_SERIAL))
        self.install = self.stack.enter_context(patch.object(cli, "ensure_kit"))
        self.wait = self.stack.enter_context(patch("ios_timeout.usbmux.wait_for_device", return_value=self.selected))
        self.connection = self.stack.enter_context(patch("ios_timeout.device.USBDevice"))
        self.connection.return_value.__enter__.return_value = self.device
        self.agents = self.stack.enter_context(patch("ios_timeout.macos_agents.recovery_agents"))
        self.other_payload = self.stack.enter_context(patch.object(upstream, "read_bundled_payloads"))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))

    def test_normal_selection_boots_once_then_reads_only_the_new_ram_session(self):
        self.assertEqual(upstream.run_backend(self.args), 0)
        self.helper.assert_called_once_with()
        self.boot.assert_called_once()
        prepared = self.boot.call_args.args[0]
        self.assertEqual(prepared.model, "iPhone1,1")
        self.assertEqual(prepared.udid, NORMAL_SERIAL)
        self.assertEqual(prepared.kit, self.args.backend_dir)
        self.assertEqual(prepared.state_dir, self.args.state_dir)
        self.wait.assert_called_once_with(RAM_SERIAL, timeout=45)
        self.connection.assert_called_once_with(self.selected, self.args.state_dir, "alpine")
        self.assertEqual(self.device.inspect.call_args_list[0].kwargs,
                         {"mount": True, "expected_model": "iPhone1,1", "read_only": True})
        self.assertEqual(self.device.inspect.call_args_list[1].kwargs,
                         {"expected_model": "iPhone1,1", "read_only": True})
        self.device.read.assert_called_once_with(iphone2g.KEYCHAIN)
        self.install.assert_not_called()
        self.other_payload.assert_not_called()
        lines = self.output.getvalue().splitlines()
        results = [line.removeprefix("Legacy passcode: ") for line in lines if line.startswith("Legacy passcode: ")]
        self.assertEqual([json.loads(value) for value in results], ["0427"])
        self.assertNotIn(KEY.hex(), self.output.getvalue())
        self.connection.return_value.__exit__.assert_called_once_with(None, None, None)
        self.agents.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_missing_or_malformed_ram_session_cannot_select_an_arbitrary_phone(self):
        for serial in (None, "", "b" * 39, "B" * 40, "b" * 40 + "\n", [], "unrelated"):
            self.boot.return_value = serial
            with self.subTest(serial=serial), self.assertRaisesRegex(ResetError, "USB session identity"):
                upstream.run_backend(self.args)
        self.wait.assert_not_called()
        self.connection.assert_not_called()
        self.other_payload.assert_not_called()
        self.assertNotIn("Legacy passcode:", self.output.getvalue())

    def test_failed_boot_never_connects_or_reports_a_passcode(self):
        self.boot.side_effect = ResetError("Synthetic boot failure")
        with self.assertRaisesRegex(ResetError, "Synthetic boot failure"):
            upstream.run_backend(self.args)
        self.wait.assert_not_called()
        self.connection.assert_not_called()
        self.assertNotIn("Legacy passcode:", self.output.getvalue())
        self.agents.return_value.__exit__.assert_called_once()

    def test_missing_expected_ram_session_never_falls_back_to_another_device(self):
        self.wait.side_effect = ResetError("Expected session did not appear")
        with self.assertRaisesRegex(ResetError, "Expected session"):
            upstream.run_backend(self.args)
        self.wait.assert_called_once_with(RAM_SERIAL, timeout=45)
        self.connection.assert_not_called()
        self.assertNotIn("Legacy passcode:", self.output.getvalue())

    def test_changed_identity_closes_connection_without_reading_keychain_or_key(self):
        self.device.inspect.side_effect = [IDENTITY, Identity("c" * 40, "iPhone1,1", "3.1.3", "7E18")]
        with self.assertRaisesRegex(ResetError, "changed during legacy recovery"):
            upstream.run_backend(self.args)
        self.device.read.assert_not_called()
        self.device.command.assert_called_once_with(iphone2g.HELPER + " --identity", timeout=15)
        self.connection.return_value.__exit__.assert_called_once()
        self.assertNotIn("Legacy passcode:", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
