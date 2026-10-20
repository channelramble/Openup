"""Real SSH/SFTP over socketpair, with a simulated legacy filesystem.

This verifies the host transport and transaction, not an iPhone kernel, USB
hardware, pwnDFU, ramdisk compatibility, or the on-screen result.
"""

import errno
import io
import os
import plistlib
import shlex
import shutil
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import paramiko

from ios_timeout import cli
from ios_timeout.core import JOURNAL, PREFS, SYSTEM_VERSION, ResetError
from ios_timeout.device import USBDevice
from tests.test_core import encoded


class TestSFTP(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = root

    def local(self, path):
        candidate = self.root / path.lstrip("/")
        if ".." in Path(path).parts:
            raise PermissionError(path)
        return candidate

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).lstat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    stat = lstat

    def open(self, path, flags, attr):
        try:
            local = self.local(path)
            fd = os.open(str(local), flags, attr.st_mode or 0o600)
            if flags & os.O_RDWR:
                mode = "r+b"
            elif flags & os.O_WRONLY:
                mode = "wb"
            else:
                mode = "rb"
            stream = os.fdopen(fd, mode, buffering=0)
            handle = paramiko.SFTPHandle(flags)
            handle.filename = str(local)
            handle.readfile = stream
            handle.writefile = stream
            handle.chattr = lambda attributes: self.chattr(path, attributes)
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def mkdir(self, path, attr):
        try:
            self.local(path).mkdir(mode=attr.st_mode or 0o700)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rmdir(self, path):
        try:
            self.local(path).rmdir()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            self.local(path).unlink()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, source, destination):
        try:
            if self.local(destination).exists():
                return paramiko.SFTP_FAILURE
            self.local(source).rename(self.local(destination))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def posix_rename(self, source, destination):
        # Older ramdisks may lack the OpenSSH extension: exercise mv fallback.
        return paramiko.SFTP_OP_UNSUPPORTED

    def chattr(self, path, attr):
        try:
            paramiko.SFTPServer.set_file_attr(str(self.local(path)), attr)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class Server(paramiko.ServerInterface):
    def __init__(self, root, owner):
        self.root = root
        self.owner = owner

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == ("root", "alpine") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.owner.channels.append(channel)
        def work():
            status, output = 0, ""
            try:
                args = shlex.split(command.decode())
                if args[:2] == ["/usr/sbin/sysctl", "-n"]:
                    output = self.owner.model + "\n"
                elif args == ["/sbin/mount"]:
                    root = "/dev/md0" if self.owner.ramdisk else "/dev/disk0s1s1"
                    output = root + " on / (hfs, local)\n"
                    if self.owner.mounted:
                        output += "/dev/disk0s1s1 on /mnt1 (hfs, local)\n/dev/disk0s1s2 on /mnt2 (hfs, local)\n"
                elif command.decode().startswith("if test -x /bin/ps;"):
                    output = "/sbin/launchd\n/usr/sbin/sshd\n"
                    if self.owner.springboard:
                        output += "/System/Library/CoreServices/SpringBoard.app/SpringBoard\n"
                elif args[:1] == ["/sbin/mount_hfs"]:
                    self.owner.mounted = True
                elif command.decode() == "test -b /dev/disk0s1s1 && test -b /dev/disk0s1s2":
                    pass
                elif args[:2] == ["/bin/cp", "-p"]:
                    shutil.copy2(self.root / args[2].lstrip("/"), self.root / args[3].lstrip("/"))
                elif args[:2] == ["/bin/mv", "-f"]:
                    os.replace(self.root / args[2].lstrip("/"), self.root / args[3].lstrip("/"))
                elif args == ["/bin/sync"]:
                    pass
                else:
                    status, output = 127, "Unimplemented test command"
                if output:
                    channel.sendall(output.encode())
                channel.send_exit_status(status)
            finally:
                channel.shutdown_write()
        threading.Thread(target=work, daemon=True).start()
        return True


class SSHTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "phone"
        self.state = Path(self.temp.name) / "state"
        self.backups = Path(self.temp.name) / "backups"
        self.model = "iPhone5,2"
        self.ramdisk = True
        self.springboard = False
        self.mounted = True
        self.selected = {"udid": "e" * 40, "device_id": 9}
        for path, data in (
            (PREFS, encoded()),
            (JOURNAL, encoded({"SBDeviceLockFailedAttempts": 11})),
            (SYSTEM_VERSION, plistlib.dumps({"ProductVersion": "9.3.5", "ProductBuildVersion": "13G36"})),
        ):
            local = self.root / path.lstrip("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            local.chmod(0o600)
        self.servers = []
        self.channels = []
        self.addCleanup(self.close_servers)

    def close_servers(self):
        for server in self.servers:
            server.close()
        for channel in self.channels:
            try:
                channel.close()
            except (EOFError, OSError):
                pass

    def connect(self, *_args):
        client, server_socket = socket.socketpair()
        transport = paramiko.Transport(server_socket)
        transport.add_server_key(self.key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, TestSFTP, root=self.root)
        self.servers.append(transport)
        thread = threading.Thread(
            target=lambda: transport.start_server(server=Server(self.root, self)), daemon=True)
        thread.start()
        return client

    def device(self, **kwargs):
        with patch("ios_timeout.usbmux.tunnel", side_effect=self.connect):
            return USBDevice(self.selected, self.state, "alpine", **kwargs)

    def test_full_cli_preview_commit_and_restore_over_ssh(self):
        original = (self.root / PREFS.lstrip("/")).read_bytes()
        arguments = ["--state-dir", str(self.state)]
        output = io.StringIO()
        with patch("ios_timeout.usbmux.tunnel", side_effect=self.connect), \
             patch("ios_timeout.usbmux.list_devices", return_value=[self.selected]), \
             redirect_stdout(output):
            self.assertEqual(cli.main(["inspect"] + arguments), 0)
            self.assertEqual((self.root / PREFS.lstrip("/")).read_bytes(), original)
            self.assertFalse(self.backups.exists())
            self.assertEqual(cli.main(["reset", "--apply", "--backups", str(self.backups)] + arguments), 0)
            folder = next(self.backups.iterdir())
            data = plistlib.loads((self.root / PREFS.lstrip("/")).read_bytes())
            self.assertEqual(data["SBDeviceLockFailedAttempts"], 0)
            self.assertFalse((self.root / JOURNAL.lstrip("/")).exists())
            self.assertEqual(cli.main(["restore", str(folder), "--apply"] + arguments), 0)
        self.assertEqual((self.root / PREFS.lstrip("/")).read_bytes(), original)
        self.assertTrue((self.root / JOURNAL.lstrip("/")).exists())
        self.assertIn("read-back verified", output.getvalue())

    def test_modern_device_is_rejected_before_mount(self):
        self.model = "iPhone10,1"
        self.mounted = False
        with self.device() as device, self.assertRaises(ResetError):
            device.inspect(mount=True)
        self.assertFalse(self.mounted)

    def test_live_ios_is_rejected(self):
        self.ramdisk = False
        with self.device() as device, self.assertRaises(ResetError):
            device.inspect()

    def test_running_springboard_is_rejected(self):
        self.springboard = True
        with self.device() as device, self.assertRaises(ResetError):
            device.inspect()

    def test_mount_is_explicit(self):
        self.mounted = False
        with self.device() as device:
            with self.assertRaises(ResetError):
                device.inspect()
            self.assertEqual(device.inspect(mount=True).version, "9.3.5")

    def test_symlink_target_is_rejected(self):
        prefs = self.root / PREFS.lstrip("/")
        prefs.rename(prefs.with_suffix(".saved"))
        prefs.symlink_to(prefs.with_suffix(".saved"))
        with self.device() as device, self.assertRaises(ResetError):
            device.read(PREFS)

    def test_host_key_change_requires_explicit_option(self):
        with self.device():
            pass
        self.key = paramiko.RSAKey.generate(2048)
        with self.assertRaises(ResetError):
            self.device()
        with self.device(trust_new_key=True):
            pass


if __name__ == "__main__":
    unittest.main()
