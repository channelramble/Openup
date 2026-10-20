import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ios_timeout.core import (
    JOURNAL, PREFS, ResetError, apply_plan, digest, finalize_backup, load_backup,
    make_plan, restore_backup,
)
from tests.test_core import IDENTITY, META, MemoryDevice, encoded


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backups = Path(self.temp.name) / "backups"

    def backup(self, device):
        return apply_plan(device, make_plan(device, IDENTITY), self.backups)

    def manifest(self, folder):
        return json.loads((folder / "manifest.json").read_text())

    def test_lost_lock_acknowledgment_is_recoverable(self):
        class DropAtLock(MemoryDevice):
            def acquire(self, token, recover=False):
                super().acquire(token, recover)
                if not recover:
                    self.disconnected = True
                    raise OSError("lost lock acknowledgment")
        d = DropAtLock()
        original = dict(d.files)
        with self.assertRaises(ResetError):
            self.backup(d)
        folder = next(self.backups.iterdir())
        self.assertEqual(self.manifest(folder)["status"], "recovery_required")
        self.assertEqual(d.files, original)
        d.disconnected = False
        restore_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, original)
        self.assertIsNone(d.lock)

    def test_busy_lock_records_no_started_transaction(self):
        d = MemoryDevice()
        d.lock = "a" * 32
        with self.assertRaises(ResetError):
            self.backup(d)
        self.assertEqual(self.manifest(next(self.backups.iterdir()))["status"], "not_started")

    def test_cleanup_failure_preserves_committed_reset(self):
        class FailRelease(MemoryDevice):
            fail = True
            def release(self, token):
                if self.fail:
                    self.fail = False
                    raise OSError("cleanup interrupted")
                super().release(token)
        d = FailRelease()
        with self.assertRaisesRegex(ResetError, "committed and verified"):
            self.backup(d)
        folder = next(self.backups.iterdir())
        self.assertEqual(self.manifest(folder)["status"], "committed_lock_pending")
        self.assertNotIn(JOURNAL, d.files)
        committed = dict(d.files)
        finalize_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, committed)
        self.assertIsNone(d.lock)
        self.assertEqual(self.manifest(folder)["status"], "committed")

    def test_lost_release_acknowledgment_can_be_finalized(self):
        class LostAck(MemoryDevice):
            fail = True
            def release(self, token):
                super().release(token)
                if self.fail:
                    self.fail = False
                    raise OSError("lost release acknowledgment")
        d = LostAck()
        with self.assertRaises(ResetError):
            self.backup(d)
        self.assertIsNone(d.lock)
        folder = next(self.backups.iterdir())
        finalize_backup(d, IDENTITY, folder)
        self.assertIsNone(d.lock)
        self.assertEqual(self.manifest(folder)["status"], "committed")

    def test_finalize_does_not_override_changes_after_reset(self):
        d = MemoryDevice()
        folder = self.backup(d)
        d.files[PREFS] = (encoded({"SBDeviceLockFailedAttempts": 2}), META)
        current = dict(d.files)
        with self.assertRaises(ResetError):
            finalize_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, current)

    def test_finalize_checks_device_identity(self):
        d = MemoryDevice()
        folder = self.backup(d)
        with self.assertRaises(ResetError):
            finalize_backup(d, replace(IDENTITY, udid="b" * 40), folder)
        self.assertIsNone(d.lock)

    def test_restore_cleanup_failure_can_be_finalized(self):
        class FailRelease(MemoryDevice):
            fail = False
            def release(self, token):
                if self.fail:
                    self.fail = False
                    raise OSError("interrupted restore cleanup")
                super().release(token)
        d = FailRelease()
        original = dict(d.files)
        folder = self.backup(d)
        d.fail = True
        with self.assertRaises(ResetError):
            restore_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, original)
        self.assertEqual(self.manifest(folder)["status"], "restored_lock_pending")
        finalize_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, original)
        self.assertIsNone(d.lock)

    def test_sync_failures_roll_back_at_each_commit_boundary(self):
        for fail_at in (1, 2, 3, 4):
            class FailSync(MemoryDevice):
                calls = 0
                def sync(self):
                    self.calls += 1
                    if self.calls == fail_at:
                        raise OSError("sync failure")
            d = FailSync()
            original = dict(d.files)
            with self.subTest(fail_at=fail_at), self.assertRaises(ResetError):
                self.backup(d)
            self.assertEqual(d.files, original)
            self.assertIsNone(d.lock)

    def test_manifest_cannot_redefine_permitted_reset(self):
        d = MemoryDevice()
        folder = self.backup(d)
        manifest = self.manifest(folder)
        manifest["files"][0]["after_sha256"] = digest(b"unrelated content")
        (folder / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ResetError, "permitted transformation"):
            load_backup(folder)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_backup_manifest_fifo_is_rejected_without_blocking(self):
        folder = self.backups / "fifo"
        folder.mkdir(parents=True)
        os.mkfifo(folder / "manifest.json")
        with self.assertRaises(ResetError):
            load_backup(folder)

    @unittest.skipIf(sys.platform == "win32", "POSIX symlink fixture")
    def test_symlink_manifest_rejected(self):
        d = MemoryDevice()
        folder = self.backup(d)
        target = folder / "original-manifest.json"
        (folder / "manifest.json").rename(target)
        (folder / "manifest.json").symlink_to(target)
        with self.assertRaises(ResetError):
            load_backup(folder)

    @unittest.skipIf(sys.platform == "win32", "POSIX symlink fixture")
    def test_symlink_backup_rejected(self):
        d = MemoryDevice()
        folder = self.backup(d)
        backup = folder / self.manifest(folder)["files"][0]["backup"]
        target = folder / "original-backup.plist"
        backup.rename(target)
        backup.symlink_to(target)
        with self.assertRaises(ResetError):
            load_backup(folder)

    def test_invalid_identity_types_are_reported_cleanly(self):
        for field, value in (("udid", None), ("model", []), ("version", 9.3), ("build", {})):
            with self.subTest(field=field), self.assertRaises(ResetError):
                replace(IDENTITY, **{field: value}).validate()

    def test_legacy_version_one_manifest_still_restores(self):
        d = MemoryDevice()
        original = dict(d.files)
        folder = self.backup(d)
        manifest = self.manifest(folder)
        manifest["tool_version"] = "1.0.0"
        (folder / "manifest.json").write_text(json.dumps(manifest))
        restore_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, original)
