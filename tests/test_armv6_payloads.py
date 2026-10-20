import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from ios_timeout import armv6_payloads
from ios_timeout.core import ResetError
from ios_timeout.wsl import build_runner


class Armv6PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest, self.values = armv6_payloads.read_bundled_payloads()

    def archive(self, entries=None):
        entries = entries if entries is not None else [
            (name, self.values[name], stat.S_IFREG | 0o755)
            for name in armv6_payloads.TOOLS
        ]
        output = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # Deliberate duplicate-entry fixture.
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
                for name, data, mode in entries:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    package.writestr(info, data)
        return output.getvalue()

    def bundle(self, archive=None, manifest=None):
        folder = self.root / "resources"
        folder.mkdir(exist_ok=True)
        archive = self.archive() if archive is None else archive
        manifest = json.loads(json.dumps(self.manifest)) if manifest is None else manifest
        manifest["archive_sha256"] = hashlib.sha256(archive).hexdigest()
        (folder / "iphone3g-armv6.zip").write_bytes(archive)
        (folder / "iphone3g-armv6.json").write_text(json.dumps(manifest))
        return folder

    def assert_rejected_without_extraction(self, folder):
        reader = armv6_payloads.read_bundled_payloads
        with self.assertRaises(ResetError):
            reader(folder)
        destination = self.root / "extracted"
        with patch.object(armv6_payloads, "read_bundled_payloads",
                          side_effect=lambda: reader(folder)):
            with self.assertRaises(ResetError):
                armv6_payloads.extract_bundled_payloads(destination)
        self.assertFalse(destination.exists(), "Validation failure created partial payloads")

    def test_real_bundled_tools_verify_and_extract_without_executing(self):
        self.assertEqual(self.manifest["product"], "iPhone1,2")
        self.assertEqual(self.manifest["ramdisk_build"], "8C148")
        self.assertEqual(set(self.values), set(armv6_payloads.TOOLS))
        destination = self.root / "extracted"
        self.assertEqual(armv6_payloads.extract_bundled_payloads(destination), destination)
        self.assertEqual(set(path.name for path in destination.iterdir()), set(self.values))
        for name, expected in self.manifest["files"].items():
            path = destination / name
            self.assertEqual(path.read_bytes(), self.values[name])
            self.assertEqual(path.stat().st_size, expected["size"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected["sha256"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)

    def test_modified_archive_is_rejected_before_extraction(self):
        folder = self.bundle()
        archive = folder / "iphone3g-armv6.zip"
        data = bytearray(archive.read_bytes())
        data[-1] ^= 1
        archive.write_bytes(data)
        self.assert_rejected_without_extraction(folder)

    def test_invalid_manifest_or_provenance_is_rejected(self):
        for field, value in (
            ("schema", 2), ("product", "iPhone1,1"), ("architecture", "armv7"),
            ("ramdisk_build", "7E18"), ("files", {}),
        ):
            with self.subTest(field=field):
                manifest = json.loads(json.dumps(self.manifest))
                manifest[field] = value
                self.assert_rejected_without_extraction(self.bundle(manifest=manifest))
        folder = self.bundle()
        (folder / "iphone3g-armv6.json").write_text("{broken")
        self.assert_rejected_without_extraction(folder)

    def test_archive_hash_does_not_replace_individual_file_verification(self):
        entries = [
            (name, data + b"altered" if name == "restored_external" else data,
             stat.S_IFREG | 0o755)
            for name, data in self.values.items()
        ]
        manifest = json.loads(json.dumps(self.manifest))
        # Match the changed size, but deliberately retain the expected file hash.
        manifest["files"]["restored_external"]["size"] += len(b"altered")
        self.assert_rejected_without_extraction(self.bundle(self.archive(entries), manifest))

    def test_duplicate_traversal_and_extra_entries_are_rejected(self):
        entries = [(name, data, stat.S_IFREG | 0o755) for name, data in self.values.items()]
        for changed in (
            entries + [entries[0]],
            [("../" + entries[0][0], entries[0][1], entries[0][2])] + entries[1:],
            [("/" + entries[0][0], entries[0][1], entries[0][2])] + entries[1:],
            entries + [("unexpected", b"data", stat.S_IFREG | 0o644)],
        ):
            with self.subTest(names=[entry[0] for entry in changed]):
                self.assert_rejected_without_extraction(self.bundle(self.archive(changed)))

    def test_symlink_entry_is_rejected_even_when_content_hash_matches(self):
        entries = [
            (name, data, (stat.S_IFLNK if name == "restored_external" else stat.S_IFREG) | 0o755)
            for name, data in self.values.items()
        ]
        self.assert_rejected_without_extraction(self.bundle(self.archive(entries)))

    def test_oversized_declared_member_is_rejected_without_decompression(self):
        archive = bytearray(self.archive())
        manifest = json.loads(json.dumps(self.manifest))
        offset = 0
        for name in armv6_payloads.TOOLS:
            offset = archive.index(b"PK\x01\x02", offset)
            # Central-directory uncompressed size; no large allocation is needed.
            struct.pack_into("<I", archive, offset + 24, armv6_payloads.MAX_FILE + 1)
            manifest["files"][name]["size"] = armv6_payloads.MAX_FILE + 1
            offset += 4
        folder = self.bundle(bytes(archive), manifest)
        with patch.object(zipfile.ZipFile, "read",
                          side_effect=AssertionError("Oversized member reached decompression")):
            self.assert_rejected_without_extraction(folder)

    def test_matching_checksums_still_require_armv6_executables(self):
        values = dict(self.values)
        data = bytearray(values["restored_external"])
        struct.pack_into("<I", data, 8, 9)  # ARMv7 CPU subtype in a thin Mach-O header.
        values["restored_external"] = bytes(data)
        manifest = json.loads(json.dumps(self.manifest))
        manifest["files"]["restored_external"]["sha256"] = hashlib.sha256(data).hexdigest()
        entries = [(name, data, stat.S_IFREG | 0o755) for name, data in values.items()]
        self.assert_rejected_without_extraction(self.bundle(self.archive(entries), manifest))

    def test_existing_extraction_directory_is_not_overwritten(self):
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "bruteforce"
        sentinel.write_bytes(b"existing file")
        with self.assertRaises(FileExistsError):
            armv6_payloads.extract_bundled_payloads(destination)
        self.assertEqual(sentinel.read_bytes(), b"existing file")

    def test_wsl_zipapp_resources_verify_in_isolated_python_process(self):
        runner = self.root / "runner.pyz"
        build_runner(runner)
        with zipfile.ZipFile(runner) as archive:
            for name in (
                "iphone3g-armv6.json", "iphone3g-armv6.zip",
                "iphone3g-source.zip", "iphone3g-NOTICE.txt",
            ):
                self.assertIn("ios_timeout/data/" + name, archive.namelist())
        # Import directly from the actual zipapp. -I excludes checkout paths,
        # user site packages, and PYTHONPATH; no bootstrap/install/device code runs.
        script = """
import json, sys
from pathlib import Path
runner, destination = sys.argv[1:]
sys.path.insert(0, runner)
from ios_timeout import armv6_payloads
assert Path(armv6_payloads.__file__).as_posix().startswith(Path(runner).as_posix() + "/")
manifest, values = armv6_payloads.read_bundled_payloads()
armv6_payloads.extract_bundled_payloads(Path(destination))
assert all((Path(destination) / name).read_bytes() == data for name, data in values.items())
print(json.dumps({"tools": sorted(values), "build": manifest["ramdisk_build"]}))
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(runner), str(self.root / "from-zip")],
            cwd=self.root, capture_output=True, text=True, check=True, timeout=30,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {"tools": sorted(armv6_payloads.TOOLS), "build": "8C148"},
        )


if __name__ == "__main__":
    unittest.main()
