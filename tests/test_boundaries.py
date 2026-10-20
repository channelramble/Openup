import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from ios_timeout.cli import host_lock, parser
from ios_timeout.core import ResetError
from ios_timeout.device import USBDevice


class BoundariesTests(unittest.TestCase):
    def test_stalled_exec_acknowledgment_has_a_deadline(self):
        class StalledChannel:
            def __init__(self):
                self.closed = threading.Event()
            def settimeout(self, _timeout):
                pass
            def exec_command(self, _command):
                if not self.closed.wait(2):
                    raise AssertionError("SSH acknowledgment was not bounded")
                raise OSError("closed")
            def close(self):
                self.closed.set()
        channel = StalledChannel()
        device = USBDevice.__new__(USBDevice)
        device.transport = Mock()
        device.transport.open_session.return_value = channel
        with self.assertRaisesRegex(ResetError, "deadline"):
            device.command("stalled command", timeout=0.02)
        self.assertTrue(channel.closed.is_set())

    def test_cleanup_failures_do_not_mask_errors(self):
        device = USBDevice.__new__(USBDevice)
        connections = [Mock(), Mock(), Mock()]
        for connection in connections:
            connection.close.side_effect = OSError("close failed")
        device.sftp, device.transport, device.socket = connections
        device.close()
        device.close()
        for connection in connections:
            connection.close.assert_called_once()

    @unittest.skipIf(sys.platform == "win32", "POSIX symlink fixture; Windows reparse checks run separately")
    def test_state_directory_and_lock_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / "real"
            real.mkdir()
            link = Path(folder) / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ResetError), host_lock(link):
                self.fail("must not enter")
            target = Path(folder) / "target"
            target.write_text("untouched")
            (real / "session.lock").symlink_to(target)
            with self.assertRaises(OSError), host_lock(real):
                self.fail("must not enter")
            self.assertEqual(target.read_text(), "untouched")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_fifo_session_lock_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            os.mkfifo(Path(folder) / "session.lock")
            with self.assertRaises((ResetError, OSError)), host_lock(Path(folder)):
                self.fail("must not enter")

    def test_two_host_sessions_cannot_run_together(self):
        with tempfile.TemporaryDirectory() as folder, host_lock(Path(folder)):
            with self.assertRaises(ResetError), host_lock(Path(folder)):
                self.fail("must not enter")

    def test_default_runtime_paths_are_not_in_installation(self):
        options = parser().parse_args(["prepare"])
        self.assertIsNone(options.kit)
        self.assertIsNone(options.backups)
        self.assertNotIn("site-packages", str(options.state_dir))
