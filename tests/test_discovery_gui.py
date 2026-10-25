import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ios_timeout import cli
from ios_timeout.core import ResetError
from tests import test_gui


NORMAL = {"udid": "a" * 40, "device_id": 7, "model": "iPhone5,1",
          "board": "n41ap", "version": "10.3.4", "mode": "normal"}
DFU = {"udid": "", "selection_id": "recovery:fixture-hardware-identity",
       "model": "iPhone5,1", "board": "n41ap", "mode": "DFU"}


class DiscoveryCliTests(unittest.TestCase):
    def test_combines_normal_and_recovery_rows_without_fabricating_udid(self):
        base = {key: NORMAL[key] for key in ("udid", "device_id")}
        with patch("ios_timeout.usbmux.list_devices", return_value=[base]), \
                patch("ios_timeout.normal_device.enrich_devices", return_value=[NORMAL]) as enrich, \
                patch("ios_timeout.usb_modes.list_devices", return_value=[DFU]):
            result = cli.discover_devices()
        enrich.assert_called_once_with([base])
        self.assertEqual(result, [NORMAL, DFU])
        self.assertEqual(result[1]["udid"], "")

    def test_lockdown_failure_keeps_the_discovered_usb_phone(self):
        base = {key: NORMAL[key] for key in ("udid", "device_id")}
        with patch("ios_timeout.usbmux.list_devices", return_value=[base]), \
                patch("ios_timeout.normal_device.device_info", side_effect=ResetError("locked")), \
                patch("ios_timeout.usb_modes.list_devices", return_value=[]):
            result = cli.discover_devices()
        self.assertEqual(result[0]["udid"], base["udid"])
        self.assertIn("info_error", result[0])
        self.assertNotIn("model", result[0])

    def test_unavailable_usbmux_service_does_not_hide_valid_dfu(self):
        with patch("ios_timeout.usbmux.list_devices", side_effect=ResetError("service unavailable")), \
                patch("ios_timeout.usb_modes.list_devices", return_value=[DFU]):
            self.assertEqual(cli.discover_devices(), [DFU])

    def test_unavailable_usbmux_service_is_reported_without_recovery_fallback(self):
        error = ResetError("fixture USB service is unavailable")
        with patch("ios_timeout.usbmux.list_devices", side_effect=error), \
                patch("ios_timeout.usb_modes.list_devices", return_value=[]), \
                self.assertRaises(ResetError) as failure:
            cli.discover_devices()
        self.assertIs(failure.exception, error)

    def test_empty_scan_reports_connection_guidance_without_claiming_ramdisk_required(self):
        output = io.StringIO()
        with patch("ios_timeout.usbmux.list_devices", return_value=[]), \
                patch("ios_timeout.usb_modes.list_devices", return_value=[]), redirect_stdout(output):
            self.assertEqual(cli.run(cli.parser().parse_args(["devices"])), 0)
        self.assertTrue(output.getvalue().startswith("[]\n"))
        self.assertIn("Refresh".lower(), output.getvalue().lower())
        self.assertNotIn("boot the SSH ramdisk first", output.getvalue())


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Desktop dependencies are not installed.")
class DiscoveryGuiTests(unittest.TestCase):
    # Reuse only the worker fixture, not the existing TestCase's test methods.
    fake_worker = test_gui.GuiTests.fake_worker

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QSettings
        from ios_timeout.gui import MainWindow
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        preferences = QSettings(str(root / "preferences.ini"), QSettings.Format.IniFormat)
        for patcher in (patch("ios_timeout.gui.QSettings", return_value=preferences),
                        patch("ios_timeout.gui.state_directory", return_value=root / "state")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow(auto_scan=False)

    def tearDown(self):
        if self.window.process is not None:
            self.window.process.deleteLater()
            self.window.process = None
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def choose_wrong_variant(self):
        self.window.phone_model.setCurrentIndex(self.window.phone_model.findData("iPhone5,2"))
        self.assertEqual(self.window.settings.model, "iPhone5,2")

    def test_normal_phone_auto_selects_variant_and_preserves_real_udid(self):
        self.choose_wrong_variant()
        self.window.populate_devices([NORMAL])
        self.assertEqual(self.window.settings.model, "iPhone5,1")
        self.assertEqual(self.window.phone_model.currentData(), "iPhone5,1")
        self.assertEqual(self.window.settings.udid, NORMAL["udid"])
        self.assertIn("Normal mode", self.window.connection.text())

    def test_dfu_corrects_wrong_variant_without_using_selection_id_as_udid(self):
        self.window.populate_devices([NORMAL])
        self.choose_wrong_variant()
        self.window.populate_devices([DFU])
        self.assertEqual(self.window.settings.model, "iPhone5,1")
        self.assertEqual(self.window.device_combo.currentData(), DFU["selection_id"])
        self.assertEqual(self.window.settings.udid, "")
        with self.fake_worker():
            self.window.start_operation("recover", apply=True)
            arguments = self.window.process.arguments
        self.assertNotIn("--udid", arguments)
        self.assertNotIn(DFU["selection_id"], arguments)
        self.assertEqual(arguments[arguments.index("--model") + 1], "iPhone5,1")

    def test_model_picker_distinguishes_same_named_hardware_variants(self):
        combo = self.window.phone_model
        first = combo.itemText(combo.findData("iPhone5,1"))
        second = combo.itemText(combo.findData("iPhone5,2"))
        self.assertNotEqual(first, second)
        self.assertIn("iPhone5,1", first)
        self.assertIn("iPhone5,2", second)

    def test_selected_normal_udid_reaches_recovery_worker_and_cli_parser(self):
        self.window.populate_devices([NORMAL])
        with self.fake_worker():
            self.window.start_operation("recover", apply=True)
            worker = self.window.process
            arguments = worker.arguments[worker.arguments.index("--session") + 1:]
            parsed = cli.parser().parse_args(arguments)
        self.assertTrue(worker.started)
        self.assertEqual(parsed.action, "recover")
        self.assertEqual(parsed.udid, NORMAL["udid"])
        self.assertEqual(parsed.model, "iPhone5,1")
        self.assertTrue(parsed.apply)

    def test_multiple_discovered_phones_block_recovery_and_preparation(self):
        for devices in ([NORMAL, DFU], [NORMAL, dict(NORMAL)]):
            with self.subTest(devices=devices):
                self.window.populate_devices(devices)
                self.window.device_combo.setCurrentIndex(self.window.device_combo.findData(NORMAL["udid"]))
                with self.fake_worker(), patch("ios_timeout.gui.QMessageBox.information") as message:
                    self.window.start_operation("recover", apply=True)
                    self.window.start_operation("prepare")
                self.assertEqual(message.call_count, 2)
                self.assertTrue(all(call.args[1] == "Connect one phone" for call in message.call_args_list))
                self.assertIsNone(self.window.process)
                self.assertFalse(self.window.busy)

    def test_changed_dfu_selection_invalidates_review_even_with_same_model_and_empty_udid(self):
        self.window.populate_devices([DFU])
        self.window.timer_flow.finish("inspect", 0)
        self.window.sync_timer_flow()
        self.assertTrue(self.window.timer_flow.can_apply)
        self.window.populate_devices([{**DFU, "selection_id": "recovery:other-hardware-identity"}])
        self.assertEqual(self.window.settings.model, "iPhone5,1")
        self.assertEqual(self.window.settings.udid, "")
        self.assertFalse(self.window.timer_flow.can_apply)

    def test_usb_row_without_readable_metadata_remains_selected(self):
        self.choose_wrong_variant()
        base = {"udid": NORMAL["udid"], "device_id": 7, "info_error": "Unavailable"}
        self.window.populate_devices([base])
        self.assertEqual(self.window.device_combo.count(), 1)
        self.assertEqual(self.window.settings.udid, NORMAL["udid"])
        self.assertEqual(self.window.settings.model, "iPhone5,2")
        self.assertIn("USB connected", self.window.connection.text())
        self.assertIn("details are unavailable", self.window.device_detail.text())

    def test_empty_usbmux_plus_dfu_discovery_updates_real_scan_result(self):
        self.choose_wrong_variant()
        output = io.StringIO()
        with patch("ios_timeout.usbmux.list_devices", return_value=[]), \
                patch("ios_timeout.usb_modes.list_devices", return_value=[DFU]), redirect_stdout(output):
            cli.run(cli.parser().parse_args(["devices"]))
        self.assertEqual(json.loads(output.getvalue()), [DFU])
        with self.fake_worker():
            self.window.scan()
            self.window.process.output = output.getvalue().encode()
            self.window.process.finish()
        self.assertEqual(self.window.settings.model, "iPhone5,1")
        self.assertEqual(self.window.settings.udid, "")
        self.assertIn("DFU mode", self.window.connection.text())
        self.assertFalse(self.window.busy)


if __name__ == "__main__":
    unittest.main()
