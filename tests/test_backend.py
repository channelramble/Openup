import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ios_timeout import cli
from ios_timeout.core import ResetError


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="itr path with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.kit = Path(self.temp.name) / "backend"
        self.args = cli.parser().parse_args(["prepare", "--kit", str(self.kit)])
        self.calls = []

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        if command[:2] == ["git", "clone"]:
            folder = Path(command[-1])
            folder.mkdir()
            (folder / "resources").mkdir()
            (folder / "restore.sh").write_text("#!/bin/bash\n")
        stdout = cli.KIT_COMMIT + "\n" if "rev-parse" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    def test_pinned_backend_download_and_argument_boundaries(self):
        with patch("ios_timeout.cli.subprocess.run", side_effect=self.fake_run), \
             patch("ios_timeout.cli.shutil.which", return_value="/usr/bin/git"), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()):
            cli.launch_backend(self.args)
        self.assertTrue((self.kit / "restore.sh").is_file())
        self.assertEqual(self.calls[1][-2:], ["--detach", cli.KIT_COMMIT])
        self.assertEqual(self.calls[-1], [
            "bash", str(self.kit.resolve() / "restore.sh"),
            "--sshrd", "--no-version-check", "--no-color",
        ])
        self.assertTrue(self.args.mount)

    def test_failed_download_cleans_partial_checkout(self):
        def failure(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            result.returncode = 1
            return result
        with patch("ios_timeout.cli.subprocess.run", side_effect=failure), \
             patch("ios_timeout.cli.shutil.which", return_value="/usr/bin/git"), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()), self.assertRaises(ResetError):
            cli.launch_backend(self.args)
        self.assertFalse(self.kit.exists())
        self.assertEqual(list(self.kit.parent.iterdir()), [])

    def test_existing_backend_is_not_replaced(self):
        self.kit.mkdir()
        (self.kit / "resources").mkdir()
        (self.kit / "restore.sh").write_text("#!/bin/bash\n# user checkout\n")
        with patch("ios_timeout.cli.subprocess.run", side_effect=self.fake_run), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()):
            cli.launch_backend(self.args)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.kit / "restore.sh").read_text().endswith("# user checkout\n"))

    def test_backend_failure_stops_prepare(self):
        self.kit.mkdir()
        (self.kit / "resources").mkdir()
        (self.kit / "restore.sh").write_text("#!/bin/bash\n")
        with patch("ios_timeout.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 1)), \
             patch("ios_timeout.cli.platform.system", return_value="Darwin"), \
             redirect_stdout(io.StringIO()), self.assertRaises(ResetError):
            cli.launch_backend(self.args)
