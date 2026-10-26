import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ios_timeout.core import ResetError
from ios_timeout.desktop_model import Settings, operation_arguments
from tests import test_gui


class ModelPlanningTests(unittest.TestCase):
    def test_default_is_unselected_and_boot_operations_require_a_model(self):
        self.assertEqual(Settings().model, "")
        for action, options in (("prepare", {}), ("recover", {"apply": True})):
            with self.subTest(action=action), self.assertRaisesRegex(ResetError, "Choose.*model"):
                operation_arguments(action, Settings(), **options)

    def test_blank_model_allows_download_preview_and_existing_ramdisk_inspection(self):
        for action, options in (("recover", {"install_only": True}), ("recover", {}), ("inspect", {})):
            with self.subTest(action=action, options=options):
                self.assertNotIn("--model", operation_arguments(action, Settings(), **options))


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Desktop dependencies are not installed.")
class ModelSelectionGuiTests(unittest.TestCase):
    fake_worker = test_gui.GuiTests.fake_worker

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QSettings
        from ios_timeout.gui import MainWindow, STYLE, dark_palette
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        preferences = QSettings(str(root / "preferences.ini"), QSettings.Format.IniFormat)
        preferences.setValue("model", "iPhone1,2")
        preferences.setValue("phone/model", "iPhone1,2")
        for patcher in (patch("ios_timeout.gui.QSettings", return_value=preferences),
                        patch("ios_timeout.gui.state_directory", return_value=root / "state")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow(auto_scan=False)
        self.window.setStyleSheet(STYLE)
        self.window.setPalette(dark_palette())

    def tearDown(self):
        self.window.phone_model.hidePopup()
        if self.window.process is not None:
            self.window.process.deleteLater()
            self.window.process = None
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_startup_display_is_empty_and_does_not_restore_a_model(self):
        self.assertEqual(self.window.settings.model, "")
        self.assertEqual(self.window.phone_model.currentIndex(), -1)
        self.assertEqual(self.window.phone_model.currentText(), "")
        self.assertEqual(self.window.phone_model.placeholderText(), "")
        self.assertTrue(self.window.armv6_notice.isHidden())
        self.assertFalse(self.window.ipsw_edit.isEnabled())
        self.window.populate_devices([])
        self.assertEqual(self.window.phone_model.currentIndex(), -1)

    def test_unknown_stored_setting_cannot_fall_back_to_the_first_model(self):
        self.window.settings.model = "unknown"
        combo = self.window.model_combo()
        self.assertEqual(combo.currentIndex(), -1)
        self.assertEqual(combo.currentText(), "")
        combo.deleteLater()

    def test_opening_and_dismissing_popup_keeps_the_model_blank(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        self.window.show()
        self.app.processEvents()
        self.window.phone_model.showPopup()
        self.app.processEvents()
        QTest.keyClick(self.window.phone_model.view(), Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertEqual(self.window.phone_model.currentIndex(), -1)
        self.assertEqual(self.window.settings.model, "")

    def test_detected_phone_still_selects_its_exact_model_from_blank(self):
        self.window.populate_devices([{"udid": "a" * 40, "device_id": 1,
                                      "model": "iPhone5,1", "mode": "normal"}])
        self.assertEqual(self.window.phone_model.currentData(), "iPhone5,1")
        self.assertEqual(self.window.settings.model, "iPhone5,1")

    def test_blank_model_blocks_boot_worker_but_keeps_download_available(self):
        with self.fake_worker(), patch("ios_timeout.gui.QMessageBox.information") as message:
            self.window.start_operation("prepare")
            self.window.start_operation("recover", apply=True)
            self.assertIsNone(self.window.process)
            self.assertEqual(message.call_count, 2)
            self.window.start_operation("recover", install_only=True)
            self.assertTrue(self.window.process.started)
            self.assertNotIn("--model", self.window.process.arguments)

    def test_popup_hover_paints_the_pointed_row_without_changing_the_selection(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest
        combo = self.window.phone_model
        combo.setCurrentIndex(0)
        self.window.show()
        self.app.processEvents()
        combo.showPopup()
        self.app.processEvents()
        view = combo.view()
        first = view.visualRect(view.model().index(0, 0))
        target = view.visualRect(view.model().index(2, 0))
        point = QPoint(target.right() - 12, target.center().y())
        QTest.mouseMove(view.viewport(), first.center())
        self.app.processEvents()
        before = view.viewport().grab().toImage()
        QTest.mouseMove(view.viewport(), target.center())
        self.app.processEvents()
        after = view.viewport().grab().toImage()
        ratio = after.devicePixelRatio()
        x, y = int(point.x() * ratio), int(point.y() * ratio)
        self.assertNotEqual(before.pixelColor(x, y), after.pixelColor(x, y))
        self.assertGreater(after.pixelColor(x, y).blue(), 80)
        self.assertEqual(combo.currentIndex(), 0)
        combo.hidePopup()


if __name__ == "__main__":
    unittest.main()
