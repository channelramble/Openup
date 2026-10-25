import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ios_timeout import backup_deletion as deletion
from ios_timeout.core import ResetError, make_backup, make_plan
from ios_timeout.host import host_lock
from tests.test_core import IDENTITY, MemoryDevice


class BackupFixtures:
    def fixture_setup(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.backups = self.state / "backups"
        self.backups.mkdir(parents=True)

    def backup(self, root=None, journal_only=False):
        plan = make_plan(MemoryDevice(), IDENTITY)
        if journal_only:
            plan.changes[:] = plan.changes[1:]
        return make_backup(plan, root or self.backups)[0]


class BackupDeletionTests(BackupFixtures, unittest.TestCase):
    def setUp(self):
        self.fixture_setup()

    def test_exact_backup_deleted_and_sibling_preserved(self):
        selected, sibling = self.backup(), self.backup()
        original = {file.name: file.read_bytes() for file in sibling.iterdir()}
        deletion.delete_backup(deletion.prepare_backup_deletion(selected, self.state), self.state)
        self.assertFalse(selected.exists())
        self.assertEqual(original, {file.name: file.read_bytes() for file in sibling.iterdir()})

    def test_valid_renamed_external_backup_allowed_even_unfinished(self):
        selected = self.backup(self.root / "imported")
        renamed = selected.with_name("my renamed recovery copy")
        selected.rename(renamed)
        snapshot = deletion.prepare_backup_deletion(renamed, self.state)
        self.assertTrue(snapshot.valid)
        self.assertEqual(snapshot.status, "backed_up")
        deletion.delete_backup(snapshot, self.state)
        self.assertFalse(renamed.exists())

    def test_damaged_managed_journal_only_backup_allowed(self):
        selected = self.backup(journal_only=True)
        self.assertTrue((selected / "00-LockoutStateJournal.plist").is_file())
        (selected / "manifest.json").write_text("broken")
        snapshot = deletion.prepare_backup_deletion(selected, self.state)
        self.assertFalse(snapshot.valid)
        deletion.delete_backup(snapshot, self.state)
        self.assertFalse(selected.exists())

    def test_invalid_external_or_noncanonical_managed_folder_refused(self):
        for selected in (self.root / "external", self.backups / "unrelated"):
            selected.mkdir()
            (selected / "manifest.json").write_text("broken")
            with self.assertRaises(ResetError):
                deletion.prepare_backup_deletion(selected, self.state)
            self.assertTrue((selected / "manifest.json").exists())

    def test_broader_roots_refused(self):
        for selected in (self.backups, self.state, self.root, Path(self.root.anchor)):
            with self.subTest(path=selected), self.assertRaises(ResetError):
                deletion.prepare_backup_deletion(selected, self.state)

    def test_unexpected_files_and_nested_folders_refused(self):
        for child in ("notes.txt", "subfolder"):
            selected = self.backup()
            extra = selected / child
            extra.mkdir() if child == "subfolder" else extra.write_text("preserve")
            with self.assertRaises(ResetError):
                deletion.prepare_backup_deletion(selected, self.state)
            self.assertTrue(extra.exists())

    @unittest.skipIf(os.name == "nt", "Creating symlinks may require Windows privileges.")
    def test_folder_and_known_file_symlinks_refused(self):
        selected = self.backup()
        linked = self.root / "link"
        linked.symlink_to(selected, target_is_directory=True)
        with self.assertRaises(ResetError):
            deletion.prepare_backup_deletion(linked, self.state)
        target = selected / "00-com.apple.springboard.plist"
        outside = self.root / "original.plist"
        target.rename(outside)
        target.symlink_to(outside)
        with self.assertRaises(ResetError):
            deletion.prepare_backup_deletion(selected, self.state)
        self.assertTrue(outside.is_file())

    def test_snapshot_change_rejected_before_any_delete(self):
        selected = self.backup()
        snapshot = deletion.prepare_backup_deletion(selected, self.state)
        manifest = selected / "manifest.json"
        data = json.loads(manifest.read_text())
        data["status"] = "recovery_required"
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ResetError, "changed after confirmation"):
            deletion.delete_backup(snapshot, self.state)
        self.assertEqual(len(list(selected.iterdir())), 3)

    def test_file_added_between_listing_and_snapshot_is_refused(self):
        selected = self.backup()
        original = deletion._snapshot
        def racing_snapshot(path, directory_fd=None):
            (path / "unrelated.txt").write_text("preserve")
            return original(path, directory_fd)
        with patch.object(deletion, "_snapshot", racing_snapshot):
            with self.assertRaisesRegex(ResetError, "contents changed"):
                deletion.prepare_backup_deletion(selected, self.state)
        self.assertTrue((selected / "unrelated.txt").exists())

    def test_active_host_lock_refuses_deletion(self):
        selected = self.backup()
        snapshot = deletion.prepare_backup_deletion(selected, self.state)
        with host_lock(self.state), self.assertRaises(ResetError):
            deletion.delete_backup(snapshot, self.state)
        self.assertEqual(len(list(selected.iterdir())), 3)

    def test_partial_failure_reports_remaining_folder(self):
        selected = self.backup()
        snapshot = deletion.prepare_backup_deletion(selected, self.state)
        unlink = os.unlink
        calls = []
        def fail_second(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError("fixture access denied")
            return unlink(path, *args, **kwargs)
        with patch.object(deletion.os, "unlink", fail_second):
            with self.assertRaisesRegex(ResetError, "Some files were removed.*Remaining folder") as error:
                deletion.delete_backup(snapshot, self.state)
        self.assertIn(str(selected), str(error.exception))
        self.assertEqual(len(list(selected.iterdir())), 2)


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "Desktop dependencies are not installed.")
class BackupDeletionGuiTests(BackupFixtures, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fixture_setup()
        from ios_timeout.gui import MainWindow
        state_patch = patch("ios_timeout.gui.state_directory", return_value=self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.selected = self.backup()
        self.window = MainWindow(auto_scan=False)
        self.window.backup_table.selectRow(0)

    def tearDown(self):
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def run_dialogs(self, choices, between=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox
        seen = []
        def execute(dialog):
            text = dialog.text() + "\n" + dialog.informativeText()
            seen.append(text)
            self.assertEqual(dialog.textFormat(), Qt.TextFormat.PlainText)
            if dialog.standardButtons() & QMessageBox.StandardButton.Yes:
                self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.Cancel))
                self.assertEqual(dialog.escapeButton(), dialog.button(QMessageBox.StandardButton.Cancel))
                self.assertIn("?", dialog.text())
                self.assertIn(str(self.selected), text)
                self.assertIn("cannot be undone", text)
                self.assertIn("finish an interrupted transaction", text)
                self.assertTrue(self.selected.exists())
            if between:
                between(len(seen))
            return choices[len(seen) - 1]
        with patch("ios_timeout.gui.QMessageBox.exec", execute):
            self.window.delete_selected_backup()
        return seen

    def test_cancel_first_dialog_preserves_every_file(self):
        from PySide6.QtWidgets import QMessageBox
        seen = self.run_dialogs([QMessageBox.StandardButton.Cancel])
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(list(self.selected.iterdir())), 3)

    def test_cancel_second_dialog_preserves_every_file(self):
        from PySide6.QtWidgets import QMessageBox
        seen = self.run_dialogs([QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Cancel])
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(list(self.selected.iterdir())), 3)

    def test_two_confirmations_delete_and_refresh_without_success_modal(self):
        from PySide6.QtWidgets import QMessageBox
        seen = self.run_dialogs([QMessageBox.StandardButton.Yes] * 2)
        self.assertEqual(len(seen), 2)
        self.assertFalse(self.selected.exists())
        self.assertEqual(self.window.backup_rows, [])
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.delete_backup_button.isEnabled())
        self.assertIn("Deleted backup:", self.window.backup_note.text())

    def test_busy_gui_cannot_start_deletion(self):
        self.window.set_busy(True)
        with patch.object(self.window, "backup_delete_dialog") as prompt:
            self.window.delete_selected_backup()
        prompt.assert_not_called()
        self.assertTrue(self.selected.exists())
        self.assertFalse(self.window.delete_backup_button.isEnabled())

    def test_invalid_managed_backup_is_selectable_and_warns_twice(self):
        from PySide6.QtWidgets import QMessageBox
        (self.selected / "manifest.json").write_text("broken")
        self.window.refresh_backups()
        self.window.backup_table.selectRow(0)
        self.assertTrue(self.window.delete_backup_button.isEnabled())
        self.assertFalse(self.window.restore_button.isEnabled())
        seen = self.run_dialogs([QMessageBox.StandardButton.Yes] * 2)
        self.assertTrue(all("might still be recoverable" in text for text in seen))
        self.assertFalse(self.selected.exists())

    def test_manual_valid_renamed_folder_is_deleted(self):
        from PySide6.QtWidgets import QMessageBox
        external = self.backup(self.root / "imported")
        self.selected = external.with_name("manual & backup")
        external.rename(self.selected)
        with patch("ios_timeout.gui.QFileDialog.getExistingDirectory", return_value=str(self.selected)):
            self.window.choose_backup()
        self.run_dialogs([QMessageBox.StandardButton.Yes] * 2)
        self.assertFalse(self.selected.exists())
        self.assertEqual(len(self.window.backup_rows), 1)

    def test_change_between_prompts_refuses_delete_and_refreshes(self):
        from PySide6.QtWidgets import QMessageBox
        def change(number):
            if number == 1:
                manifest = self.selected / "manifest.json"
                data = json.loads(manifest.read_text())
                data["status"] = "recovery_required"
                manifest.write_text(json.dumps(data))
        seen = self.run_dialogs([QMessageBox.StandardButton.Yes] * 2 + [QMessageBox.StandardButton.Ok], change)
        self.assertEqual(len(seen), 3)
        self.assertIn("changed after confirmation", seen[-1])
        self.assertEqual(self.window.backup_rows[0]["status"], "recovery_required")
        self.assertEqual(len(list(self.selected.iterdir())), 3)
        self.assertFalse(self.window.busy)


if __name__ == "__main__":
    unittest.main()
