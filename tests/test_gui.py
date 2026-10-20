import importlib.util
import os
import time
import unittest
from unittest.mock import patch

QT_AVAILABLE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(QT_AVAILABLE, "Desktop dependencies are not installed in this development environment.")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ios_timeout.gui import MainWindow
        self.window = MainWindow(auto_scan=False)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_all_pages_and_device_selection(self):
        self.assertEqual(self.window.pages.count(), 6)
        for index in range(6):
            # Accessibility activates checkable buttons through their checked state.
            self.window.nav_group.button(index).setChecked(True)
            self.app.processEvents()
            self.assertEqual(self.window.pages.currentIndex(), index)
        self.window.populate_devices([{"udid": "abc123456", "device_id": 1}])
        self.assertEqual(self.window.settings.udid, "abc123456")
        self.window.populate_devices([])
        self.assertEqual(self.window.settings.udid, "")

    def test_cancelled_confirmation_never_starts_device_write(self):
        from PySide6.QtWidgets import QMessageBox
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.window.start_operation("reset", apply=True)
        self.assertIsNone(self.window.process)
        self.assertFalse(self.window.busy)

    def test_real_bundled_worker_streams_output_and_completes(self):
        self.window.start_operation("doctor")
        self.assertTrue(self.window.busy)
        deadline = time.monotonic() + 20
        while self.window.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        if self.window.busy and self.window.process:
            self.window.process.kill()
            self.window.process.waitForFinished(3000)
            self.app.processEvents()
        self.assertFalse(self.window.busy, "The diagnostic worker did not finish.")
        self.assertIn("SSH library: Paramiko", self.window.console.toPlainText())
        self.assertIn("exit status 0", self.window.console.toPlainText())
        self.assertFalse(self.window.reply.isEnabled())
