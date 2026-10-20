import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ios_timeout import host, legacy_names


class BrandMigrationTests(unittest.TestCase):
    def test_new_installations_use_openup_and_existing_workspaces_stay_in_place(self):
        for platform in ("darwin", "linux", "win32"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                base = home / "Library/Application Support" if platform == "darwin" else home / "state"
                old_name = legacy_names.STATE_SLUG if platform == "linux" else legacy_names.APP_NAME
                new_name = "openup" if platform == "linux" else "Openup"
                old, new = base / old_name, base / new_name
                with patch.object(host.sys, "platform", platform), \
                     patch.object(Path, "home", return_value=home), \
                     patch.dict(os.environ, {"LOCALAPPDATA": str(base), "XDG_STATE_HOME": str(base)}):
                    self.assertEqual(host.state_directory(), new)
                    old.mkdir(parents=True)
                    backup = old / "original-backup"
                    backup.write_bytes(b"unchanged")
                    self.assertEqual(host.state_directory(), old)
                    self.assertEqual(backup.read_bytes(), b"unchanged")
                    self.assertFalse(new.exists())
                    new.mkdir()
                    self.assertEqual(host.state_directory(), new)
