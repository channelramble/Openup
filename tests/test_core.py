import json
import os
import plistlib
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ios_timeout.core import (
    Identity, JOURNAL, LockBusy, Metadata, PREFS, ResetError, apply_plan, compatible,
    load_backup, make_plan, restore_backup,
)

IDENTITY = Identity("a" * 40, "iPhone5,2", "9.3.5", "13G36")
META = Metadata(stat.S_IFREG | 0o600, 501, 501)


def encoded(values=None, binary=True):
    if values is None:
        values = {
            "SBDeviceLockBlocked": True,
            "SBDeviceLockFailedAttempts": 11,
            "SBDeviceLockBlockTimeIntervalSinceReferenceDate": 63113904000.0,
            "SBDeviceLockBlockStateGeneration": 55,
            "SBDeviceLockCachedVCCaps": b"state",
            "SBDeviceWipeEnabled": True,
            "Wallpaper": b"preserve this",
            "Unrelated": {"a": [1, True, "x"]},
        }
    return plistlib.dumps(values, fmt=plistlib.FMT_BINARY if binary else plistlib.FMT_XML)


class MemoryDevice:
    def __init__(self, binary=True, journal=True):
        self.files = {PREFS: (encoded(binary=binary), META)}
        if journal:
            self.files[JOURNAL] = (encoded({"SBDeviceLockFailedAttempts": 11}), META)
        self.lock = None
        self.renames = 0
        self.fail_rename_at = None
        self.drop_connection_at = None
        self.disconnected = False

    def available(self):
        if self.disconnected:
            raise OSError("USB disconnected")

    def read(self, path):
        self.available()
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def exists(self, path):
        self.available()
        return path in self.files

    def stage(self, source, destination, contents, meta):
        self.available()
        self.files[destination] = (contents, meta)

    def rename(self, source, destination, replace=False):
        self.available()
        self.renames += 1
        if self.renames == self.drop_connection_at:
            self.disconnected = True
            raise OSError("USB disconnected")
        if self.renames == self.fail_rename_at:
            raise OSError("Injected rename failure")
        if destination in self.files and not replace:
            raise OSError("destination exists")
        self.files[destination] = self.files.pop(source)

    def remove(self, path):
        self.available()
        del self.files[path]

    def sync(self):
        self.available()

    def acquire(self, token, recover=False):
        self.available()
        if self.lock is not None and not (recover and self.lock == token):
            raise LockBusy("locked")
        self.lock = token

    def release(self, token):
        self.available()
        if self.lock is None:
            return
        if self.lock != token:
            raise ResetError("wrong lock")
        self.lock = None


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backups = Path(self.temp.name) / "backups"

    def test_binary_plist_preserves_preferences_and_security_policy(self):
        d = MemoryDevice()
        plan = make_plan(d, IDENTITY)
        actual = plistlib.loads(plan.changes[0].after)
        before = plistlib.loads(d.files[PREFS][0])
        self.assertTrue(plan.changes[0].after.startswith(b"bplist00"))
        self.assertIs(actual["SBDeviceLockBlocked"], False)
        self.assertEqual(actual["SBDeviceLockFailedAttempts"], 0)
        self.assertNotIn("SBDeviceLockBlockStateGeneration", actual)
        self.assertEqual(actual["Wallpaper"], before["Wallpaper"])
        self.assertEqual(actual["Unrelated"], before["Unrelated"])
        self.assertIs(actual["SBDeviceWipeEnabled"], True)

    def test_xml_format_is_preserved(self):
        self.assertTrue(make_plan(MemoryDevice(binary=False), IDENTITY).changes[0].after.startswith(b"<?xml"))

    def test_preview_does_not_mutate(self):
        d = MemoryDevice()
        originals = dict(d.files)
        make_plan(d, IDENTITY)
        self.assertEqual(d.files, originals)
        self.assertIsNone(d.lock)

    def test_missing_journal_is_supported(self):
        plan = make_plan(MemoryDevice(journal=False), IDENTITY)
        self.assertEqual([c.path for c in plan.changes], [PREFS])

    def test_clean_preferences_are_noop(self):
        d = MemoryDevice(journal=False)
        d.files[PREFS] = (encoded({"SBDeviceLockBlocked": False, "SBDeviceLockFailedAttempts": 0}), META)
        self.assertEqual(make_plan(d, IDENTITY).changes, [])
        self.assertIsNone(apply_plan(d, make_plan(d, IDENTITY), self.backups))
        self.assertFalse(self.backups.exists())

    def test_bool_and_integer_plist_types_are_corrected(self):
        d = MemoryDevice(journal=False)
        d.files[PREFS] = (encoded({"SBDeviceLockBlocked": 0, "SBDeviceLockFailedAttempts": False}), META)
        changes = make_plan(d, IDENTITY).changes
        self.assertEqual(len(changes), 1)
        self.assertIs(plistlib.loads(changes[0].after)["SBDeviceLockBlocked"], False)
        self.assertIs(type(plistlib.loads(changes[0].after)["SBDeviceLockFailedAttempts"]), int)

    def test_invalid_plist_is_rejected(self):
        d = MemoryDevice()
        d.files[PREFS] = (b"corrupt", META)
        with self.assertRaises(ResetError):
            make_plan(d, IDENTITY)

    def test_non_dictionary_plist_is_rejected(self):
        d = MemoryDevice()
        d.files[PREFS] = (plistlib.dumps(["bad"]), META)
        with self.assertRaises(ResetError):
            make_plan(d, IDENTITY)

    def test_unknown_schema_is_rejected(self):
        d = MemoryDevice()
        d.files[PREFS] = (encoded({"Wallpaper": "only"}), META)
        with self.assertRaises(ResetError):
            make_plan(d, IDENTITY)

    def test_pending_wipe_is_rejected(self):
        d = MemoryDevice()
        d.files[PREFS] = (encoded({"SBDeviceLockBlocked": True, "SBDeviceWipe": True}), META)
        with self.assertRaises(ResetError):
            make_plan(d, IDENTITY)

    def test_unsupported_hardware_and_versions(self):
        for model, version in (("iPhone6,1", "7.1.2"), ("iPhone3,1", "9.0"),
                               ("iPhone5,3", "6.1"), ("iPhone5,2", "11.0"),
                               ("iPhone5,2", "9.x"), ("unknown", "6.1")):
            with self.subTest(model=model, version=version), self.assertRaises(ResetError):
                compatible(model, version)

    def test_transaction_and_restore_byte_identical(self):
        d = MemoryDevice()
        originals = dict(d.files)
        folder = apply_plan(d, make_plan(d, IDENTITY), self.backups)
        self.assertIsNone(d.lock)
        self.assertNotIn(JOURNAL, d.files)
        self.assertEqual(d.files[PREFS][1], META)
        self.assertEqual(json.loads((folder / "manifest.json").read_text())["status"], "committed")
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for file in folder.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        restore_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, originals)
        self.assertIsNone(d.lock)
        # Repeating restoration after a lost acknowledgment is safe.
        restore_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, originals)

    def test_every_rename_failure_rolls_back(self):
        for failure in (1, 2, 3):
            with self.subTest(failure=failure):
                d = MemoryDevice()
                originals = dict(d.files)
                d.fail_rename_at = failure
                with self.assertRaises(ResetError):
                    apply_plan(d, make_plan(d, IDENTITY), self.backups)
                self.assertEqual(d.files, originals)
                self.assertIsNone(d.lock)

    def test_disconnect_recoverable_at_every_commit_boundary(self):
        for failure in (1, 2, 3):
            with self.subTest(failure=failure):
                d = MemoryDevice()
                originals = dict(d.files)
                d.drop_connection_at = failure
                root = self.backups / str(failure)
                with self.assertRaises(ResetError):
                    apply_plan(d, make_plan(d, IDENTITY), root)
                folder = next(root.iterdir())
                self.assertEqual(json.loads((folder / "manifest.json").read_text())["status"], "recovery_required")
                d.disconnected = False
                d.drop_connection_at = None
                restore_backup(d, IDENTITY, folder)
                self.assertEqual(d.files, originals)
                self.assertIsNone(d.lock)

    def test_changed_original_prevents_write(self):
        d = MemoryDevice()
        plan = make_plan(d, IDENTITY)
        newer = (encoded({"SBDeviceLockBlocked": True, "SBDeviceLockFailedAttempts": 12}), META)
        d.files[PREFS] = newer
        with self.assertRaises(ResetError):
            apply_plan(d, plan, self.backups)
        self.assertEqual(d.files[PREFS], newer)
        self.assertEqual(d.renames, 0)

    def test_wrong_phone_or_ios_rejected_on_restore(self):
        d = MemoryDevice()
        folder = apply_plan(d, make_plan(d, IDENTITY), self.backups)
        for identity in (replace(IDENTITY, udid="b" * 40), replace(IDENTITY, build="13G37")):
            with self.assertRaises(ResetError):
                restore_backup(d, identity, folder)

    def test_corrupt_backup_rejected_before_device_mutation(self):
        d = MemoryDevice()
        folder = apply_plan(d, make_plan(d, IDENTITY), self.backups)
        (folder / ("00-" + PREFS.rsplit("/", 1)[1])).write_bytes(b"corrupt")
        with self.assertRaises(ResetError):
            restore_backup(d, IDENTITY, folder)
        self.assertIsNone(d.lock)

    def test_path_traversal_manifest_rejected(self):
        d = MemoryDevice()
        folder = apply_plan(d, make_plan(d, IDENTITY), self.backups)
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest["files"][0]["backup"] = "../outside.plist"
        (folder / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ResetError):
            load_backup(folder)

    def test_changed_file_after_reset_is_not_clobbered(self):
        d = MemoryDevice()
        folder = apply_plan(d, make_plan(d, IDENTITY), self.backups)
        newer = (encoded({"SBDeviceLockFailedAttempts": 2}), META)
        d.files[PREFS] = newer
        before = dict(d.files)
        with self.assertRaises(ResetError):
            restore_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, before)

    def test_partial_staging_write_is_cleaned_after_rollback(self):
        class PartialDevice(MemoryDevice):
            def stage(self, source, destination, contents, meta):
                self.files[destination] = (contents[:19], meta)
                raise OSError("partial SFTP write")
        d = PartialDevice()
        before = dict(d.files)
        with self.assertRaises(ResetError):
            apply_plan(d, make_plan(d, IDENTITY), self.backups)
        self.assertEqual(d.files, before)
        self.assertIsNone(d.lock)

    def test_disconnect_during_staging_can_be_recovered(self):
        class DroppingDevice(MemoryDevice):
            def stage(self, source, destination, contents, meta):
                self.files[destination] = (contents[:19], meta)
                self.disconnected = True
                raise OSError("USB disconnected")
        d = DroppingDevice()
        before = dict(d.files)
        with self.assertRaises(ResetError):
            apply_plan(d, make_plan(d, IDENTITY), self.backups)
        folder = next(self.backups.iterdir())
        d.disconnected = False
        restore_backup(d, IDENTITY, folder)
        self.assertEqual(d.files, before)
        self.assertIsNone(d.lock)

    def test_existing_lock_is_respected(self):
        d = MemoryDevice()
        d.lock = "another-transaction"
        original = dict(d.files)
        with self.assertRaises(ResetError):
            apply_plan(d, make_plan(d, IDENTITY), self.backups)
        self.assertEqual(d.files, original)
        self.assertEqual(d.lock, "another-transaction")


if __name__ == "__main__":
    unittest.main()
