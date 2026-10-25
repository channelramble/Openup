import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_desktop
from ios_timeout.core import ResetError
from ios_timeout import bundled_python2


class Python2PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        override = patch.object(build_desktop, "ROOT", self.root)
        override.start()
        self.addCleanup(override.stop)
        self.directory = self.root / "packaging/python2"
        self.directory.mkdir(parents=True)

    def archive(self, extra=None):
        contents = {"bin/python2": b"#!/bin/sh\n", "bin/python2.7": b"runtime",
                    "LICENSE.Python": b"license"}
        contents.update(extra or {})
        files = {}
        archive = self.directory / "python2-2.7.18-linux-x86_64.zip"
        with zipfile.ZipFile(archive, "w") as output:
            for name, data in contents.items():
                mode = 0o755 if name.startswith("bin/") else 0o644
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o100000 | mode) << 16
                output.writestr(info, data)
                files[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": mode}
        manifest = {"schema": 1, "version": "2.7.18", "targets": {"linux-x86_64": {
            "archive_size": archive.stat().st_size, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "files": files}}}
        (self.directory / "manifest.json").write_text(json.dumps(manifest))
        return archive

    def test_windows_includes_the_same_checked_linux_runtime_for_wsl(self):
        self.archive()
        destination = self.root / "Windows app/python2-runtime"
        destination.parent.mkdir()
        build_desktop.install_python2("windows-x86_64", destination)
        self.assertEqual((destination / "bin/python2.7").read_bytes(), b"runtime")
        self.assertEqual((destination / "LICENSE.Python").read_bytes(), b"license")

    def test_changed_archive_never_extracts(self):
        archive = self.archive()
        archive.write_bytes(archive.read_bytes() + b"changed")
        destination = self.root / "runtime"
        with self.assertRaisesRegex(ValueError, "checksum"):
            build_desktop.install_python2("linux-x86_64", destination)
        self.assertFalse(destination.exists())

    def test_outside_paths_are_rejected_even_in_a_hashed_archive(self):
        self.archive({"../outside": b"bad"})
        with self.assertRaises(ResetError):
            build_desktop.install_python2("linux-x86_64", self.root / "runtime")
        self.assertFalse((self.root / "outside").exists())


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "The legacy backend runs on macOS or Linux/WSL")
class NativePython2RuntimeTests(unittest.TestCase):
    def test_real_runtime_relocates_and_ignores_ambient_python(self):
        machine = platform.machine().lower()
        if machine not in ("arm64", "aarch64", "x86_64", "amd64"):
            self.skipTest("No desktop runtime for this host architecture")
        target = ("macos-" + ("arm64" if machine in ("arm64", "aarch64") else "x86_64")
                  if sys.platform == "darwin" else "linux-x86_64")
        if sys.platform == "linux" and machine in ("arm64", "aarch64"):
            self.skipTest("Linux desktop runtime targets x86_64")
        with tempfile.TemporaryDirectory(prefix="Python 2 app with spaces ") as folder:
            root = Path(folder)
            runtime = root / "python2-runtime"
            build_desktop.install_python2(target, runtime)
            hostile = root / "unrelated-python"
            hostile.mkdir()
            (hostile / "sitecustomize.py").write_text("raise RuntimeError('external Python was used')")
            original = {"PATH": "/usr/bin:/bin", "HOME": str(hostile), "PYTHONHOME": str(hostile),
                        "PYTHONPATH": str(hostile), "PYTHONUSERBASE": str(hostile), "BL_IOS_DESKTOP": "1"}
            with patch.object(bundled_python2, "app_root", return_value=root):
                env = bundled_python2.environment(original)
            command = "import bz2,ctypes,hashlib,struct,zlib; print 'bundled Python 2 works'"
            result = subprocess.check_output(["python2", "-c", command], env=env, cwd=hostile, text=True, timeout=15)
            self.assertEqual(result.strip(), "bundled Python 2 works")
            self.assertEqual(shutil.which("python2", path=env["PATH"]), str(runtime / "bin/python2"))
            if sys.platform == "linux":
                system = re.compile(r"^(?:ld-linux|linux-vdso|libc\.|libm\.|libdl\.|libpthread\.|librt\.|libutil\.)")
                for binary in runtime.rglob("*"):
                    if not binary.is_file():
                        continue
                    with binary.open("rb") as stream:
                        if stream.read(4) != b"\x7fELF":
                            continue
                    dependencies = subprocess.check_output(["ldd", str(binary)], env=env, text=True, timeout=15)
                    self.assertNotIn("not found", dependencies)
                    for name, origin in re.findall(r"^\s*(\S+) => (/.+?)\s+\(0x", dependencies, re.MULTILINE):
                        if not system.match(name):
                            self.assertTrue(Path(origin).resolve().is_relative_to(runtime), (name, origin))
