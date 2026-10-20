import importlib.metadata
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import build_release
import check_dependencies
from ios_timeout import __version__


class DistributionTests(unittest.TestCase):
    def test_archive_is_reproducible_and_excludes_runtime_data(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = build_release.build(Path(folder))
            first = archive.read_bytes()
            self.assertEqual(build_release.build(Path(folder)).read_bytes(), first)
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
                self.assertTrue(all(name.startswith("Openup/") for name in names))
                for name in names:
                    self.assertNotIn("/.venv/", name)
                    self.assertNotIn("/backups/", name)
                    self.assertNotIn("/.git/", name)
                    self.assertNotIn("/.backends/", name)
                launcher = package.getinfo("Openup/Launch.command")
                self.assertEqual((launcher.external_attr >> 16) & 0o777, 0o755)
                source = json.loads(package.read("Openup/SOURCE.json"))
                self.assertEqual(source["version"], __version__)
                self.assertEqual(
                    package.read("Openup/requirements-lock.txt"),
                    package.read("Openup/ios_timeout/runtime-requirements.txt"),
                )

    def test_dependency_drift_and_missing_transitive_packages_are_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "requirements-lock.txt"
            lock.write_text("paramiko==5.1.0\ncryptography==50.0.2\n")
            with patch.object(importlib.metadata, "version", side_effect=[
                "5.1.0", importlib.metadata.PackageNotFoundError("cryptography"),
            ]):
                self.assertEqual(check_dependencies.check(lock), ["cryptography is missing"])
            with patch.object(importlib.metadata, "version", side_effect=["5.1.0", "49.0.0"]):
                self.assertEqual(
                    check_dependencies.check(lock),
                    ["cryptography: installed 49.0.0, required 50.0.2"],
                )

    def test_unpinned_requirement_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "requirements-lock.txt"
            lock.write_text("paramiko>=5\n")
            with self.assertRaises(ValueError):
                check_dependencies.check(lock)
