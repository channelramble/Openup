import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ios_timeout import bundled_python2
from ios_timeout.core import ResetError


class BundledPython2Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="app with spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "python2-runtime"
        override = patch.object(bundled_python2, "app_root", return_value=self.root)
        override.start()
        self.addCleanup(override.stop)

    def install(self):
        (self.runtime / "bin").mkdir(parents=True)
        for name in ("python2", "python2.7"):
            path = self.runtime / "bin" / name
            path.write_text("placeholder")
            path.chmod(0o755)

    def response(self, version=None, prefix=None):
        return subprocess.CompletedProcess([], 0, json.dumps([
            version or [2, 7, 18], str(prefix or self.runtime), 8]), "")

    def test_desktop_missing_runtime_never_uses_external_python(self):
        with patch.object(bundled_python2.subprocess, "run") as run, self.assertRaises(ResetError):
            bundled_python2.environment({"BL_IOS_DESKTOP": "1", "PATH": "/external/bin"})
        run.assert_not_called()

    def test_bundled_interpreter_wins_and_parent_environment_is_unchanged(self):
        self.install()
        original = {"PATH": "/external/bin", "PYTHONPATH": "/app-python3", "BL_IOS_DESKTOP": "1"}
        with patch.object(bundled_python2.subprocess, "run", return_value=self.response()) as run:
            result = bundled_python2.environment(original)
        self.assertEqual(result["PATH"].split(os.pathsep)[0], str(self.runtime / "bin"))
        self.assertEqual(result["BL_IOS_PYTHON2"], str(self.runtime / "bin/python2"))
        self.assertEqual(original["PATH"], "/external/bin")
        self.assertEqual(result["PYTHONPATH"], "/app-python3")
        self.assertEqual(run.call_args.args[0][0], str(self.runtime / "bin/python2"))
        self.assertLessEqual(run.call_args.kwargs["timeout"], 15)

    def test_wrong_version_or_external_prefix_stops_before_backend(self):
        self.install()
        for response in (self.response(version=[3, 13, 16]), self.response(prefix="/external/python")):
            with self.subTest(response=response.stdout), \
                 patch.object(bundled_python2.subprocess, "run", return_value=response), \
                 self.assertRaises(ResetError):
                bundled_python2.environment({"BL_IOS_DESKTOP": "1"})

    def test_failed_or_stalled_interpreter_reports_app_repair(self):
        self.install()
        for error in (OSError("bad architecture"), subprocess.TimeoutExpired([], 15),
                      subprocess.CalledProcessError(1, [])):
            with self.subTest(error=error), patch.object(bundled_python2.subprocess, "run", side_effect=error), \
                 self.assertRaisesRegex(ResetError, "complete app"):
                bundled_python2.environment({"BL_IOS_DESKTOP": "1"})

    def test_source_only_cli_preserves_existing_setup(self):
        original = {"PATH": "/existing/bin"}
        with patch.object(bundled_python2.subprocess, "run") as run:
            self.assertEqual(bundled_python2.environment(original), original)
        run.assert_not_called()

    def test_windows_forwards_desktop_requirement_into_wsl(self):
        from ios_timeout.wsl import desktop_wsl_environment
        with patch.dict(os.environ, {"BL_IOS_DESKTOP": "1", "WSLENV": "EXISTING/p:BL_IOS_DESKTOP/l"}, clear=True):
            result = desktop_wsl_environment()
        self.assertEqual(result["WSLENV"], "EXISTING/p:BL_IOS_DESKTOP/u")

    def test_directory_links_cannot_redirect_to_external_python(self):
        self.install()
        for path in (self.runtime, self.runtime / "bin"):
            external = self.root / "external-python"
            path.rename(external)
            try:
                path.symlink_to(external, target_is_directory=True)
            except OSError:
                external.rename(path)
                self.skipTest("Host cannot create directory symlinks")
            try:
                with self.subTest(path=path), patch.object(bundled_python2.subprocess, "run") as run, \
                     self.assertRaises(ResetError):
                    bundled_python2.environment({"BL_IOS_DESKTOP": "1"})
                run.assert_not_called()
            finally:
                path.unlink()
                external.rename(path)
