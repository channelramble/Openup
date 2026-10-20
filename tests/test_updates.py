import hashlib
import io
import json
import stat
import tempfile
import unittest
import zipfile
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ios_timeout.core import ResetError
from ios_timeout.updates import (
    activate_archive, active_record, archive_entries, asset_for,
    current_directory, run, select_release,
)


def release_zip(path, version="1.3.0", extra=None):
    entries = {
        "bootstrap.py": b"print('fixture, never executed')\n",
        "ios_timeout/__main__.py": b"# fixture\n",
        "ios_timeout/launcher.py": b"# fixture\n",
        "SOURCE.json": json.dumps({"version": version, "revision": "a" * 40, "dirty": False}).encode(),
    }
    entries.update(extra or {})
    entries["SHA256SUMS.txt"] = "".join(
        hashlib.sha256(data).hexdigest() + "  " + name + "\n"
        for name, data in sorted(entries.items())
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr("Bruteforce-Legacy-iOS/" + name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UpdateTests(unittest.TestCase):
    def test_release_selection_ignores_drafts_and_prereleases(self):
        releases = [{"tag_name": "v2.0.0", "draft": True},
                    {"tag_name": "v1.4.0", "prerelease": True},
                    {"tag_name": "v1.3.0"}, {"tag_name": "../unsafe"}]
        self.assertEqual(select_release(releases)["tag_name"], "v1.3.0")
        self.assertEqual(select_release(releases, True)["tag_name"], "v1.4.0")

    def test_missing_github_digest_prevents_install(self):
        with self.assertRaises(ResetError):
            asset_for({"tag_name": "v1.3.0", "assets": [
                {"name": "Bruteforce-Legacy-iOS-1.3.0.zip", "id": 1, "size": 10},
            ]})

    def test_install_verify_rollback_and_reactivation(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            archive = Path(folder) / "update.zip"
            digest = release_zip(archive)
            destination = activate_archive(archive, state, "1.3.0", digest)
            self.assertEqual(current_directory(state), destination)
            self.assertFalse((state / "backups").exists())
            args = Namespace(state_dir=state, rollback=True)
            with redirect_stdout(io.StringIO()):
                run(args)
            self.assertIsNone(current_directory(state))
            self.assertEqual(active_record(state)["previous"], "1.3.0")
            self.assertEqual(activate_archive(archive, state, "1.3.0", digest), destination)

    def test_download_digest_failure_preserves_active_version(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            archive = Path(folder) / "update.zip"
            digest = release_zip(archive)
            activate_archive(archive, state, "1.3.0", digest)
            before = (state / "updates/active.json").read_bytes()
            with self.assertRaises(ResetError):
                activate_archive(archive, state, "1.4.0", "0" * 64)
            self.assertEqual((state / "updates/active.json").read_bytes(), before)

    def test_traversal_is_rejected_even_with_matching_download_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "update.zip"
            digest = release_zip(archive, extra={"../outside": b"bad"})
            with self.assertRaises(ResetError):
                archive_entries(archive, "1.3.0", digest)
            self.assertFalse((Path(folder) / "outside").exists())

    def test_modified_installed_source_is_not_run(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            archive = Path(folder) / "update.zip"
            digest = release_zip(archive)
            installed = activate_archive(archive, state, "1.3.0", digest)
            (installed / "bootstrap.py").write_bytes(b"modified")
            with self.assertRaises(ResetError):
                current_directory(state)

    def test_failed_activation_can_be_retried_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            archive = Path(folder) / "update.zip"
            digest = release_zip(archive)
            from ios_timeout import updates
            original = updates.secure_write
            def fail_pointer(path, data):
                if Path(path).name == "active.json":
                    raise OSError("simulated activation failure")
                return original(path, data)
            with patch.object(updates, "secure_write", side_effect=fail_pointer), self.assertRaises(OSError):
                activate_archive(archive, state, "1.3.0", digest)
            self.assertIsNone(active_record(state)["current"])
            installed = activate_archive(archive, state, "1.3.0", digest)
            self.assertEqual(current_directory(state), installed)

    def test_wrong_release_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "update.zip"
            digest = release_zip(archive)
            with self.assertRaises(ResetError):
                archive_entries(archive, "1.4.0", digest)
