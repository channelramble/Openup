import importlib.util
import os
from pathlib import Path
import tempfile
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
        from ios_timeout import APP_NAME, __version__
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
        transcript = self.window.session_log.text()
        self.assertIn(f"App: {APP_NAME} {__version__}\n", transcript)
        self.assertIn("Action: doctor\n", transcript)
        self.assertIn("Model: iPhone1,2\n", transcript)
        self.assertIn("Platform: ", transcript)

    def test_export_retains_context_evicted_from_visible_scrollback(self):
        from PySide6.QtWidgets import QFileDialog
        self.window.append_output("App header\nFirst failure\n")
        self.window.append_output("".join(f"Progress line {index}\n" for index in range(3500)))
        self.window.append_output("Latest line\n")
        self.assertLessEqual(self.window.console.document().blockCount(), 2500)
        self.assertNotIn("First failure", self.window.console.toPlainText())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "export.txt"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(path), "")):
                self.window.save_output()
            result = path.read_text()
        self.assertTrue(result.startswith("App header\nFirst failure\n"))
        self.assertTrue(result.endswith("Latest line\n"))
        self.assertIn("Progress line 0\n", result)

    def test_clear_button_clears_visible_output_and_export(self):
        from PySide6.QtWidgets import QPushButton
        from ios_timeout.session_log import SessionLog, OMISSION_MARKER
        self.window.session_log = SessionLog(max_bytes=512)
        self.window.append_output("Old context\n" + "x" * 1000)
        self.assertIn(OMISSION_MARKER, self.window.session_log.text())
        button = next(button for button in self.window.findChildren(QPushButton) if button.text() == "Clear")
        button.click()
        self.assertEqual(self.window.console.toPlainText(), "")
        self.assertEqual(self.window.session_log.text(), "")
        self.window.append_output("New output\n")
        self.assertEqual(self.window.session_log.text(), "New output\n")
