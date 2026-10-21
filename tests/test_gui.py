import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import contextmanager
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
        # A failed assertion must not leave a fake operation blocking close().
        if self.window.process is not None:
            self.window.process.deleteLater()
            self.window.process = None
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    @contextmanager
    def fake_worker(self):
        """Exercise real GUI lifecycle slots without starting any backend."""
        from PySide6.QtCore import QObject, QProcess, Signal

        class Worker(QObject):
            readyReadStandardOutput = Signal()
            finished = Signal(int, object)
            errorOccurred = Signal(object)
            ProcessChannelMode = QProcess.ProcessChannelMode
            ProcessError = QProcess.ProcessError

            def __init__(self, parent=None):
                super().__init__(parent)
                self.arguments = []
                self.input = []
                self.output = b""
                self.started = False

            def setProgram(self, value):
                self.program = value

            def setArguments(self, value):
                self.arguments = value

            def setProcessEnvironment(self, value):
                self.environment = value

            def setWorkingDirectory(self, value):
                self.directory = value

            def setProcessChannelMode(self, _value):
                pass

            def start(self):
                self.started = True

            def write(self, value):
                self.input.append(bytes(value))
                return len(value)

            def readAllStandardOutput(self):
                value, self.output = self.output, b""
                return value

            def finish(self, code=0):
                self.finished.emit(code, QProcess.ExitStatus.NormalExit)

        with patch("ios_timeout.gui.QProcess", Worker):
            yield Worker

    def test_all_pages_and_device_selection(self):
        self.assertEqual(self.window.pages.count(), 6)
        self.assertEqual(
            [self.window.nav_group.button(index).text() for index in range(6)],
            ["Start", "Reset timer", "Recover passcode", "Backups", "Updates", "Help"],
        )
        for index in range(6):
            # Accessibility activates checkable buttons through their checked state.
            self.window.nav_group.button(index).setChecked(True)
            self.app.processEvents()
            self.assertEqual(self.window.pages.currentIndex(), index)
        self.window.populate_devices([{"udid": "abc123456", "device_id": 1}])
        self.assertEqual(self.window.settings.udid, "abc123456")
        self.window.populate_devices([])
        self.assertEqual(self.window.settings.udid, "")

    def test_details_and_advanced_options_start_collapsed(self):
        self.assertTrue(self.window.details_panel.isHidden())
        self.assertFalse(self.window.details_toggle.isChecked())
        self.assertTrue(self.window.reply_row.isHidden())
        for disclosure in (self.window.reset_advanced, self.window.recovery_advanced):
            self.assertFalse(disclosure.toggle.isChecked())
            self.assertTrue(disclosure.content.isHidden())
            disclosure.toggle.click()
            self.assertTrue(disclosure.toggle.isChecked())
            self.assertFalse(disclosure.content.isHidden())
        self.window.details_toggle.click()
        self.assertFalse(self.window.details_panel.isHidden())
        self.window.set_details_visible(False)
        self.assertFalse(self.window.details_toggle.isChecked())
        self.assertTrue(self.window.details_panel.isHidden())

    def test_device_rescan_preserves_only_a_present_selection(self):
        devices = [{"udid": "first", "device_id": 1}, {"udid": "second", "device_id": 2}]
        self.window.populate_devices(devices)
        self.assertEqual(self.window.settings.udid, "")
        self.window.device_combo.setCurrentIndex(self.window.device_combo.findData("second"))
        self.assertEqual(self.window.settings.udid, "second")
        self.window.populate_devices(list(reversed(devices)))
        self.assertEqual(self.window.settings.udid, "second")
        self.window.populate_devices([devices[0]])
        self.assertEqual(self.window.settings.udid, "first")
        self.window.populate_devices([])
        self.assertEqual(self.window.settings.udid, "")

    def test_busy_worker_uses_guided_choices_with_terminal_and_manual_input_off(self):
        with self.fake_worker():
            self.window.show()
            self.window.start_operation("prepare")
            worker = self.window.process
            self.app.processEvents()
            self.assertTrue(worker.started)
            self.assertEqual(worker.environment.value("BL_IOS_UI_PROTOCOL"), "1")
            self.assertTrue(self.window.busy)
            for field in (self.window.phone_model, self.window.device_combo,
                          self.window.ipsw_edit, self.window.armv6_edit):
                self.assertFalse(field.isEnabled())
            self.assertTrue(self.window.details_panel.isHidden())
            self.assertTrue(self.window.reply_row.isHidden())
            self.assertTrue(self.window.prompt_input_row.isHidden())
            self.assertFalse(self.window.manual_input.isChecked())
            event = {"type": "prompt", "id": "p1", "kind": "continue", "title": "Get ready for DFU",
                     "message": "Keep the phone connected. Hold Power and Home when instructed."}
            worker.output = (json.dumps(event) + "\n").encode()
            worker.readyReadStandardOutput.emit()
            self.app.processEvents()
            self.assertEqual(self.window.guidance_title.text(), "Get ready for DFU")
            self.assertEqual(len(self.window.prompt_buttons), 1)
            self.assertTrue(self.window.prompt_input_row.isHidden())
            self.assertTrue(self.window.details_panel.isHidden())
            self.window.prompt_buttons[0].click()
            self.window.answer_prompt("")  # Double activation cannot send a second response.
            self.assertEqual([json.loads(value) for value in worker.input],
                             [{"type": "reply", "id": "p1", "value": ""}])
            self.window.nav_group.button(5).click()
            self.assertEqual(self.window.pages.currentIndex(), 5)
            self.window.start_operation("devices")
            self.assertIs(self.window.process, worker)
            worker.finish()
            self.assertFalse(self.window.busy)
            self.assertTrue(self.window.phone_model.isEnabled())
            self.assertTrue(self.window.prompt_controls.isHidden())
            self.assertTrue(self.window.reply_row.isHidden())
            self.assertTrue(self.window.details_panel.isHidden())

    def test_password_prompt_uses_a_temporary_masked_form_and_clears_it(self):
        from PySide6.QtWidgets import QLineEdit
        with self.fake_worker():
            self.window.start_operation("inspect")
            worker = self.window.process
            self.window.handle_session_event({"type": "prompt", "id": "secret1", "kind": "password",
                                              "title": "SSH password", "message": "Enter the ramdisk password."})
            self.assertFalse(self.window.prompt_input_row.isHidden())
            self.assertEqual(self.window.prompt_input.echoMode(), QLineEdit.EchoMode.Password)
            self.window.prompt_input.setText("fixture-secret")
            self.window.prompt_submit.click()
            self.assertEqual(self.window.prompt_input.text(), "")
            self.assertTrue(self.window.prompt_input_row.isHidden())
            self.assertNotIn("fixture-secret", self.window.session_log.text())
            self.assertNotIn("fixture-secret", self.window.console.toPlainText())
            self.assertEqual(json.loads(worker.input[-1])["value"], "fixture-secret")
            worker.finish(1)
            self.assertTrue(self.window.details_panel.isHidden())

    def test_manual_terminal_input_requires_explicit_opt_in(self):
        with self.fake_worker():
            self.window.start_operation("prepare")
            worker = self.window.process
            self.window.reply.setText("not sent")
            self.window.send_input()
            self.assertEqual(worker.input, [])
            self.window.details_toggle.click()
            self.assertTrue(self.window.reply_row.isHidden())
            self.window.manual_input.click()
            self.assertFalse(self.window.reply_row.isHidden())
            self.window.reply.setText("fixture reply")
            self.window.send_input()
            self.assertEqual(json.loads(worker.input[-1]), {"type": "raw", "value": "fixture reply"})
            self.window.set_details_visible(False)
            self.assertFalse(self.window.manual_input.isChecked())
            self.assertTrue(self.window.reply_row.isHidden())
            worker.finish()

    def test_failure_displays_result_without_opening_terminal(self):
        with self.fake_worker():
            self.window.start_operation("prepare")
            worker = self.window.process
            worker.output = (json.dumps({"type": "output", "text": "[Error] Fixture USB failure. Reconnect the phone.\n"}) + "\n").encode()
            worker.finish(1)
            self.assertFalse(self.window.guidance_panel.isHidden())
            self.assertIn("Fixture USB failure", self.window.guidance_text.text())
            self.assertTrue(self.window.details_panel.isHidden())
            self.assertTrue(self.window.reply_row.isHidden())

    def test_progress_and_menu_are_accessible_without_manual_input(self):
        with self.fake_worker():
            self.window.start_operation("prepare")
            worker = self.window.process
            self.window.session_hint.setText("Response sent. Waiting for the next step…")
            self.window.handle_session_event({"type": "progress", "title": "Hold Home", "message": "Release Power.", "remaining": 7})
            self.assertIn("7 seconds", self.window.guidance_text.text())
            self.assertNotIn("Waiting", self.window.session_hint.text())
            self.window.handle_session_event({"type": "prompt", "kind": "choice", "id": "menu1", "title": "Recovery menu", "message": "Choose an action.",
                                              "options": [{"label": "Show passcode", "value": "1"}, {"label": "Finish session", "value": "2"}]})
            self.assertEqual([b.text() for b in self.window.prompt_buttons], ["Show passcode", "Finish session"])
            self.assertTrue(self.window.prompt_input_row.isHidden())
            self.window.prompt_buttons[1].click()
            self.assertEqual(json.loads(worker.input[-1])["value"], "2")
            worker.finish()

    def test_dark_palette_covers_native_surfaces_and_disabled_text(self):
        from PySide6.QtGui import QPalette
        from ios_timeout.gui import dark_palette
        palette = dark_palette()
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            self.assertLess(palette.color(role).lightness(), 50)
        self.assertGreater(palette.color(QPalette.ColorRole.Text).lightness(), 180)
        self.assertGreater(palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).lightness(), 100)

    def test_reported_result_survives_the_next_menu_and_clears_for_next_session(self):
        from PySide6.QtWidgets import QMessageBox
        with self.fake_worker(), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window.start_operation("recover", apply=True)
            for text in ("found ", ": 12", "34\n"):
                self.window.handle_session_event({"type": "output", "text": text})
            self.window.show_prompt({"type": "prompt", "kind": "continue", "id": "result",
                                     "title": "Continue", "message": "Continue when ready."})
            self.assertEqual(self.window.reported_passcode.text(), "Reported passcode: 1234")
            self.assertFalse(self.window.reported_passcode.isHidden())
            self.assertTrue(self.window.details_panel.isHidden())
            self.window.process.finish()
            self.window.start_operation("doctor")
            self.assertTrue(self.window.reported_passcode.isHidden())
            self.assertEqual(self.window.reported_passcode.text(), "")
            self.window.process.finish()

    def test_recovery_starts_once_and_hands_off_in_gui_without_a_menu_or_terminal(self):
        from PySide6.QtWidgets import QMessageBox
        from ios_timeout.backend_ui import RECOVERY_HANDOFF_LINE, RECOVERY_HANDOFF_TITLE
        with self.fake_worker(), patch.object(QMessageBox, "question") as question:
            self.window.recovery_start.click()
            worker = self.window.process
            self.assertTrue(worker.started)
            self.assertIn("recover", worker.arguments)
            self.assertIn("--apply", worker.arguments)
            question.assert_not_called()
            self.window.recovery_start.click()
            self.assertIs(self.window.process, worker)
            worker.output = (json.dumps({"type": "output", "text": RECOVERY_HANDOFF_LINE + "\n"}) + "\n").encode()
            worker.finish()
            self.assertFalse(self.window.busy)
            self.assertEqual(self.window.guidance_title.text(), RECOVERY_HANDOFF_TITLE)
            self.assertIn("no further menu choice", self.window.guidance_text.text())
            self.assertTrue(self.window.details_panel.isHidden())
            self.assertTrue(self.window.prompt_controls.isHidden())
            self.assertTrue(self.window.reply_row.isHidden())
            self.assertTrue(self.window.reported_passcode.isHidden())
            self.assertEqual(worker.input, [])

    def test_old_choice_cannot_answer_a_replacement_prompt(self):
        with self.fake_worker():
            self.window.start_operation("prepare")
            event = {"type": "prompt", "kind": "choice", "title": "Continue?",
                     "message": "Choose an action.", "options": [{"label": "Yes", "value": "y"}]}
            self.window.show_prompt({**event, "id": "p1"})
            old_button = self.window.prompt_buttons[0]
            self.window.show_prompt({**event, "id": "p2"})
            old_button.click()
            self.window.answer_prompt("y", "p1")
            self.assertEqual(self.window.process.input, [])
            self.window.prompt_buttons[0].click()
            self.assertEqual(json.loads(self.window.process.input[-1])["id"], "p2")
            self.window.process.finish()

    def test_interrupt_keeps_busy_until_worker_reports_completion(self):
        with self.fake_worker():
            self.window.start_operation("doctor")
            worker = self.window.process
            self.window.interrupt()
            self.assertEqual([json.loads(value) for value in worker.input], [{"type": "interrupt"}])
            self.assertTrue(self.window.busy)
            self.assertFalse(self.window.stop_button.isEnabled())
            self.window.interrupt()
            self.assertEqual([json.loads(value) for value in worker.input], [{"type": "interrupt"}])
            worker.finish(130)
            self.assertFalse(self.window.busy)
            self.assertIn("130", self.window.console.toPlainText())

    def test_failed_worker_start_restores_controls_and_hides_prompt(self):
        from PySide6.QtCore import QProcess
        with self.fake_worker():
            self.window.start_operation("doctor")
            self.window.process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
            self.assertFalse(self.window.busy)
            self.assertIsNone(self.window.process)
            self.assertTrue(self.window.phone_model.isEnabled())
            self.assertTrue(self.window.reply_row.isHidden())
            self.assertFalse(self.window.stop_button.isEnabled())
            self.assertIn("Could not start", self.window.session_status.text())

    def test_update_failure_status_survives_thread_completion(self):
        self.window.set_busy(True, "Checking for updates")
        self.window.task_failed("Update fixture failure")
        self.window.task_finished()
        self.assertFalse(self.window.busy)
        self.assertIn("Update fixture failure", self.window.update_info.text())
        self.assertIn("Update fixture failure", self.window.session_log.text())
        self.assertNotIn("Ready", self.window.session_status.text())

    def test_invalid_backup_selection_never_starts_a_restore(self):
        from PySide6.QtWidgets import QMessageBox
        self.window.backup_rows = [
            {"path": "/unused/invalid-backup", "valid": False},
        ]
        self.window.backup_table.setRowCount(1)
        self.window.backup_table.selectRow(0)
        with patch.object(QMessageBox, "information"), \
             patch.object(self.window, "start_operation") as start:
            self.window.recover_backup("restore")
            self.window.recover_backup("finalize")
        start.assert_not_called()

    def test_timer_primary_requires_review_before_apply_and_advances_on_success(self):
        from PySide6.QtWidgets import QMessageBox
        self.assertEqual(self.window.timer_flow.stage, "prepare")
        self.assertFalse(self.window.timer_flow.can_apply)
        with self.fake_worker(), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window.run_timer_step()
            worker = self.window.process
            self.assertIn("prepare", worker.arguments)
            self.assertNotIn("--apply", worker.arguments)
            self.assertFalse(self.window.timer_primary.isEnabled())
            worker.finish()
            self.assertEqual(self.window.timer_flow.stage, "review")
            self.assertTrue(self.window.timer_flow.can_apply)
            self.assertTrue(self.window.timer_primary.isEnabled())
            self.window.run_timer_step()
            worker = self.window.process
            self.assertIn("reset", worker.arguments)
            self.assertIn("--apply", worker.arguments)
            worker.finish()
        self.assertEqual(self.window.timer_flow.stage, "complete")
        self.assertFalse(self.window.timer_flow.can_apply)

    def test_direct_timer_apply_without_review_never_starts_worker(self):
        from PySide6.QtWidgets import QMessageBox
        with self.fake_worker(), \
             patch.object(QMessageBox, "information"), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window.start_operation("reset", apply=True)
        self.assertIsNone(self.window.process)
        self.assertFalse(self.window.busy)
        self.assertEqual(self.window.timer_flow.stage, "prepare")

    def test_target_changes_invalidate_timer_review(self):
        self.window.ipsw_edit.setText("/unused/local-firmware.ipsw")
        self.window.timer_flow.finish("inspect", 0)
        self.window.sync_timer_flow()
        other_model = next(index for index in range(self.window.phone_model.count())
                           if self.window.phone_model.itemData(index) != self.window.settings.model)
        self.window.phone_model.setCurrentIndex(other_model)
        self.assertEqual(self.window.settings.model, self.window.phone_model.currentData())
        self.assertEqual(self.window.timer_flow.stage, "prepare")
        self.assertFalse(self.window.timer_flow.can_apply)
        self.assertEqual(self.window.ipsw_edit.text(), "")
        self.assertFalse(self.window.ipsw_edit.isEnabled())
        self.window.timer_flow.finish("inspect", 0)
        self.window.sync_timer_flow()
        self.window.populate_devices([{"udid": "changed-device", "device_id": 1}])
        self.assertEqual(self.window.settings.udid, "changed-device")
        self.assertEqual(self.window.timer_flow.stage, "prepare")

    def test_connection_options_cancel_preserves_review_and_save_invalidates_it(self):
        from PySide6.QtWidgets import QDialog, QSpinBox
        self.window.timer_flow.finish("inspect", 0)
        self.window.sync_timer_flow()

        def edit_and_cancel(dialog):
            dialog.findChild(QSpinBox).setValue(2222)
            return QDialog.DialogCode.Rejected

        with patch.object(QDialog, "exec", edit_and_cancel):
            self.window.options_dialog()
        self.assertEqual(self.window.settings.port, 22)
        self.assertEqual(self.window.timer_flow.stage, "review")

        def edit_and_save(dialog):
            dialog.findChild(QSpinBox).setValue(2222)
            return QDialog.DialogCode.Accepted

        with patch.object(QDialog, "exec", edit_and_save):
            self.window.options_dialog()
        self.assertEqual(self.window.settings.port, 2222)
        self.assertEqual(self.window.timer_flow.stage, "prepare")

    def test_window_close_waits_for_worker_cleanup(self):
        from PySide6.QtGui import QCloseEvent
        from PySide6.QtWidgets import QMessageBox
        with self.fake_worker(), patch.object(QMessageBox, "information"):
            self.window.start_operation("doctor")
            worker = self.window.process
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertTrue(self.window.busy)
            worker.finish()
            self.window.token_edit.setText("fixture-token")
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertTrue(event.isAccepted())
            self.assertEqual(self.window.token_edit.text(), "")

    def test_failed_reinspection_cannot_reuse_a_previous_timer_review(self):
        self.window.timer_flow.finish("inspect", 0)
        self.window.sync_timer_flow()
        with self.fake_worker():
            self.window.start_operation("inspect")
            self.assertFalse(self.window.timer_flow.can_apply)
            self.window.process.finish(1)
        self.assertEqual(self.window.timer_flow.stage, "prepare")
        self.assertFalse(self.window.timer_flow.can_apply)

    def test_backup_operations_invalidate_review_even_when_the_worker_fails(self):
        from PySide6.QtWidgets import QMessageBox
        with self.fake_worker(), \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            for action in ("restore", "finalize"):
                with self.subTest(action=action):
                    self.window.timer_flow.finish("inspect", 0)
                    self.window.sync_timer_flow()
                    self.assertTrue(self.window.timer_flow.can_apply)
                    self.window.start_operation(
                        action, apply=True, backup=Path(tempfile.gettempdir()) / "fixture-backup",
                    )
                    worker = self.window.process
                    self.assertIn(action, worker.arguments)
                    self.assertIn("--apply", worker.arguments)
                    self.assertFalse(self.window.timer_flow.can_apply)
                    worker.finish(1)
                    self.assertFalse(self.window.busy)
                    self.assertEqual(self.window.timer_flow.stage, "prepare")
                    self.assertFalse(self.window.timer_flow.can_apply)

    def test_recovery_download_does_not_start_passcode_recovery(self):
        with self.fake_worker():
            self.window.start_operation("recover", install_only=True)
            worker = self.window.process
            self.assertTrue(self.window.operation_install_only)
            self.assertFalse(self.window.operation_apply)
            self.assertIn("--install-only", worker.arguments)
            self.assertNotIn("--apply", worker.arguments)
            worker.finish()
        self.assertEqual(self.window.timer_flow.stage, "prepare")

    def test_update_install_requires_a_newer_release_and_idle_state(self):
        from ios_timeout import __version__
        self.window.pending_release = None
        self.window.set_busy(False)
        self.assertFalse(self.window.install_button.isEnabled())
        self.window.pending_release = {"version": __version__}
        self.window.set_busy(False)
        self.assertFalse(self.window.install_button.isEnabled())
        self.window.pending_release = {"version": "99.0.0"}
        self.window.set_busy(False)
        self.assertTrue(self.window.install_button.isEnabled())
        self.window.set_busy(True)
        self.assertFalse(self.window.install_button.isEnabled())
        self.window.set_busy(False)
        self.assertTrue(self.window.install_button.isEnabled())

    def test_version_shortcut_opens_updates_and_restart_explains_running_version(self):
        from ios_timeout import __version__
        self.assertIn(__version__, self.window.windowTitle())
        self.window.update_shortcut.click()
        self.assertEqual(self.window.pages.currentIndex(), 4)
        self.assertTrue(self.window.online_updates.content.isHidden())
        self.assertFalse(self.window.local_update_button.isHidden())
        self.window.token_edit.setText("fixture-private-token")
        self.window.update_ready("99.0.0")
        self.assertIn(__version__, self.window.update_info.text())
        self.assertIn("99.0.0", self.window.relaunch_button.text())
        self.assertFalse(self.window.relaunch_button.isHidden())
        self.assertEqual(self.window.token_edit.text(), "")
        self.assertNotIn("fixture-private-token", self.window.session_log.text())

    def test_downloaded_update_picker_uses_adjacent_checksum_without_a_token(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.zip"
            checksum = Path(str(archive) + ".sha256")
            checksum.write_text("fixture")
            with patch("ios_timeout.gui.QFileDialog.getOpenFileName", return_value=(str(archive), "")) as choose, \
                 patch("ios_timeout.gui.desktop_updates.install_downloaded", return_value={"version": "99.0.0"}) as install, \
                 patch.object(self.window, "run_task") as run:
                self.window.install_downloaded_update()
                function, finished, _ = run.call_args.args
                finished(function(lambda text: None))
            choose.assert_called_once()
            self.assertEqual(install.call_args.args, (str(archive), str(checksum)))
            self.assertFalse(self.window.relaunch_button.isHidden())

    def test_cancelled_update_file_selection_and_busy_window_do_not_install(self):
        for selected in ([("", "")], [("/missing/release.zip", ""), ("", "")]):
            with patch("ios_timeout.gui.QFileDialog.getOpenFileName", side_effect=selected), \
                 patch.object(self.window, "run_task") as run:
                self.window.install_downloaded_update()
            run.assert_not_called()
        self.window.set_busy(True)
        self.assertFalse(self.window.local_update_button.isEnabled())
        with patch("ios_timeout.gui.QFileDialog.getOpenFileName") as choose:
            self.window.install_downloaded_update()
        choose.assert_not_called()

    def test_excluding_prereleases_discards_the_previous_update_selection(self):
        self.window.prereleases.setChecked(True)
        self.window.pending_release = {"version": "99.0.0", "prerelease": True}
        self.window.update_action_states()
        self.assertTrue(self.window.install_button.isEnabled())
        self.assertTrue(self.window.update_check_button.isHidden())
        self.window.prereleases.setChecked(False)
        self.assertIsNone(self.window.pending_release)
        self.assertFalse(self.window.install_button.isEnabled())
        self.assertTrue(self.window.install_button.isHidden())
        self.assertFalse(self.window.update_check_button.isHidden())
        with patch.object(self.window, "run_task") as run:
            self.window.install_update()
        run.assert_not_called()

    def test_cancelled_confirmation_never_starts_device_write(self):
        from PySide6.QtWidgets import QMessageBox
        self.window.timer_flow.finish("inspect", 0)
        self.window.sync_timer_flow()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.window.start_operation("reset", apply=True)
        self.assertIsNone(self.window.process)
        self.assertFalse(self.window.busy)
        self.assertEqual(self.window.timer_flow.stage, "review")

    def test_real_bundled_worker_streams_output_and_completes(self):
        from ios_timeout import APP_NAME, __version__
        self.window.start_operation("doctor")
        self.assertTrue(self.window.busy)
        self.assertTrue(self.window.reply_row.isHidden())
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
